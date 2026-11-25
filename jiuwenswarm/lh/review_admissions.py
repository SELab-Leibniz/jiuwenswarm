# Copyright (c) 2026 Chrys. All rights reserved.

"""Durable, purpose-split admission limits shared by Reviewer scheduling paths.

Diagnostic/repair reviews and acceptance reviews have independent quotas. A
diagnostic review can never consume an acceptance attempt, so a long trial that
spends its diagnosis allowance still retains a final decision reserve. The same
observable review input (candidate tree, contract revision, checks and user
decisions) may not be reviewed more than ``same_input_limit`` times for the same
purpose. Counts are committed before the Reviewer runs, so an interrupted,
failed or forced review does not silently reset them.

The ledger is bound to the Core trial identity and uses a file lock plus a
re-read on every admission, so two controller instances cannot overwrite each
other's counts or restart the allowance under a different output directory.
"""

from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from jiuwenswarm.pact_core.artifacts import write_json_atomic
from jiuwenswarm.pact_core.runtime import RuntimeStateError

SCHEMA_V1 = "long-horizon/review-admissions/v1"
SCHEMA = "long-horizon/review-admissions/v2"
DIAGNOSIS = "diagnosis"
ACCEPTANCE = "acceptance"
# Normal scheduling no longer stops on a low count. These are extreme fuses
# that only bound a pathological loop; time and gap progress decide ordinary
# stops. The same-purpose/same-input limit remains the anti-retry guard.
DIAGNOSIS_LIMIT = 64
ACCEPTANCE_LIMIT = 32
SAME_INPUT_LIMIT = 2
MIN_DIAGNOSIS_LIMIT = 64
MAX_DIAGNOSIS_LIMIT = 64
MIN_ACCEPTANCE_LIMIT = 32
MAX_ACCEPTANCE_LIMIT = 32
_MAX_REFUSALS = 32
_LOCK_POLL_SECONDS = 0.01

FUSE_REASON = "review_fuse_reached"

MAX_WORKER_REPAIR_ROUNDS = 3
WORKER_REPAIR_SCHEMA = "long-horizon/review-quota-worker-repair/v1"
_DEFAULT_WORKER_REPAIR_SECONDS = 300.0
_MIN_WORKER_REPAIR_SECONDS = 60.0
_MAX_WORKER_REPAIR_SECONDS = 1800.0

# Kept for callers that imported the old single-quota default. It now names the
# diagnosis quota; acceptance has its own reserved default.
TRIAL_LIMIT = DIAGNOSIS_LIMIT


def default_review_limits(budget_seconds: float | None) -> tuple[int, int]:
    """Return the extreme fuses, independent of the trial length.

    A low count is not a useful normal stopping condition: the observed
    ``review_limit_reached`` trials still had 72-279 minutes left. Ordinary
    stopping is decided by time admission and persistent gap progress; these
    fuses only bound a pathological loop and are reported separately as
    ``review_fuse_reached``.
    """
    return DIAGNOSIS_LIMIT, ACCEPTANCE_LIMIT


def acceptance_quota_spent(payload: dict[str, Any]) -> bool:
    """Return whether the acceptance fuse is spent (legacy name)."""
    try:
        count = payload["purposes"][ACCEPTANCE]["count"]
        limit = payload["limits"][ACCEPTANCE]
    except (KeyError, TypeError):
        return False
    return type(count) is int and type(limit) is int and count >= limit > 0


def acceptance_fuse_reached(payload: dict[str, Any]) -> bool:
    """Return whether the extreme acceptance fuse has been reached."""
    return acceptance_quota_spent(payload)


def worker_repair_round_seconds(snapshot: dict[str, Any]) -> float:
    """Estimate one Worker repair round from completed implementation durations.

    A reviewer-less repair round still needs a Worker turn plus a public check.
    Use the slowest of the last three completed implementation calls, bounded so
    an early fast fixture or one pathological timeout cannot make the decision
    meaningless. With no usable history, keep a conservative five-minute slot.
    """
    calls = snapshot.get("calls")
    durations: list[float] = []
    if isinstance(calls, list):
        for call in calls:
            if not isinstance(call, dict):
                continue
            if call.get("role") != "worker" or call.get("purpose") != "implementation":
                continue
            if call.get("status") != "completed":
                continue
            value = call.get("observed_duration_seconds")
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or value < 0
            ):
                continue
            durations.append(float(value))
    if not durations:
        return _DEFAULT_WORKER_REPAIR_SECONDS
    recent = durations[-3:]
    return min(_MAX_WORKER_REPAIR_SECONDS, max(_MIN_WORKER_REPAIR_SECONDS, max(recent)))


def worker_repair_allowed(
    *,
    remaining_seconds: float,
    finish_reserve_seconds: float,
    worker_round_seconds: float,
    rounds_completed: int = 0,
    max_rounds: int = MAX_WORKER_REPAIR_ROUNDS,
) -> bool:
    """Keep the finish reserve plus one complete Worker/check round in hand."""
    values = (remaining_seconds, finish_reserve_seconds, worker_round_seconds)
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or value < 0
        for value in values
    ):
        return False
    if type(rounds_completed) is not int or type(max_rounds) is not int:
        return False
    if rounds_completed < 0 or max_rounds < 1 or rounds_completed >= max_rounds:
        return False
    return float(remaining_seconds) > float(finish_reserve_seconds) + float(worker_round_seconds)


def _category(purpose: str) -> str:
    """Acceptance is its own quota; every diagnostic/repair purpose shares one."""
    return ACCEPTANCE if purpose == "acceptance" else DIAGNOSIS


class ReviewAdmissions:
    def __init__(
        self,
        path: Path,
        *,
        trial_limit: int | None = None,
        diagnosis_limit: int | None = None,
        diagnostic_limit: int | None = None,
        acceptance_limit: int = ACCEPTANCE_LIMIT,
        same_input_limit: int = SAME_INPUT_LIMIT,
        trial_id: str | None = None,
        work_id: str | None = None,
        lock_timeout_seconds: float = 10.0,
    ) -> None:
        if diagnostic_limit is not None:
            if diagnosis_limit is not None and diagnosis_limit != diagnostic_limit:
                raise ValueError("diagnosis_limit and diagnostic_limit disagree")
            diagnosis_limit = diagnostic_limit
        if diagnosis_limit is None:
            diagnosis_limit = trial_limit if trial_limit is not None else DIAGNOSIS_LIMIT
        elif trial_limit is not None and trial_limit != diagnosis_limit:
            raise ValueError("trial_limit is the legacy diagnosis_limit and cannot disagree")
        if diagnosis_limit < 1 or acceptance_limit < 1 or same_input_limit < 1:
            raise ValueError("review limits must be positive")
        if (
            isinstance(lock_timeout_seconds, bool)
            or not isinstance(lock_timeout_seconds, (int, float))
            or not math.isfinite(lock_timeout_seconds)
            or lock_timeout_seconds <= 0
        ):
            raise ValueError("review admission lock timeout must be positive and finite")
        self.path = path
        self.diagnosis_limit = diagnosis_limit
        self.acceptance_limit = acceptance_limit
        # Compatibility alias: the old single review cap now names diagnosis.
        self.trial_limit = diagnosis_limit
        self.same_input_limit = same_input_limit
        self.lock_timeout_seconds = float(lock_timeout_seconds)
        self._identity = (
            {"trial_id": trial_id, "work_id": work_id} if trial_id is not None or work_id is not None else None
        )
        self._lock = threading.Lock()
        self._value = self._load()

    def _defaults(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "identity": self._identity,
            "trial": 0,
            "purposes": {DIAGNOSIS: {"count": 0}, ACCEPTANCE: {"count": 0}},
            "keys": {},
            "refusals": [],
            "admissions": [],
        }

    @staticmethod
    def _migrate_row(row: dict[str, Any]) -> bool:
        """Give legacy single-purpose rows an explicit per-purpose count."""
        purposes = row.get("purposes")
        if isinstance(purposes, dict):
            for category, purpose_row in purposes.items():
                if category not in (DIAGNOSIS, ACCEPTANCE):
                    raise RuntimeStateError("review admission purpose count is invalid")
                if (
                    not isinstance(purpose_row, dict)
                    or type(purpose_row.get("count")) is not int
                    or purpose_row["count"] < 0
                ):
                    raise RuntimeStateError("review admission purpose count is invalid")
            return False
        count = row["count"]
        last_purpose = row.get("last_purpose")
        # Old ledgers did not record a category. An explicit acceptance is kept
        # as acceptance; all other historical attempts become diagnosis counts.
        category = ACCEPTANCE if last_purpose == "acceptance" else DIAGNOSIS
        row["purposes"] = {
            category: {
                "count": count,
                "last_purpose": last_purpose or category,
                "last_full": bool(row.get("last_full")),
            }
        }
        return True

    def _purpose_counts(self) -> dict[str, int]:
        return self._purpose_counts_in(self._value)

    @staticmethod
    def _purpose_counts_in(value: dict[str, Any]) -> dict[str, int]:
        counts = {DIAGNOSIS: 0, ACCEPTANCE: 0}
        for row in value["keys"].values():
            for category, purpose_row in row["purposes"].items():
                counts[category] += purpose_row["count"]
        return counts

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._defaults()
        try:
            if self.path.is_symlink() or not self.path.is_file() or self.path.stat().st_size > 4 * 1024 * 1024:
                raise ValueError("review admission file is not canonical")
            value = json.loads(self.path.read_bytes())
        except (OSError, ValueError) as error:
            raise RuntimeStateError("review admission evidence is unreadable") from error
        if (
            not isinstance(value, dict)
            or value.get("schema") not in (SCHEMA, SCHEMA_V1)
            or type(value.get("trial")) is not int
            or value["trial"] < 0
            or not isinstance(value.get("keys"), dict)
            or not isinstance(value.get("refusals"), list)
        ):
            raise RuntimeStateError("review admission evidence has an unsupported schema")
        identity = value.get("identity")
        if identity is not None and not isinstance(identity, dict):
            raise RuntimeStateError("review admission identity is invalid")
        if self._identity is not None and identity is not None and identity != self._identity:
            raise RuntimeStateError("review admission ledger belongs to a different trial")
        if identity is None:
            value["identity"] = self._identity
        admissions = value.get("admissions", [])
        if not isinstance(admissions, list) or any(not isinstance(row, dict) for row in admissions):
            raise RuntimeStateError("review admission history is invalid")
        value["admissions"] = admissions[-128:]
        for key, row in value["keys"].items():
            if (
                not isinstance(key, str)
                or not isinstance(row, dict)
                or type(row.get("count")) is not int
                or row["count"] < 0
            ):
                raise RuntimeStateError("review admission counts are invalid")
            self._migrate_row(row)
        value["schema"] = SCHEMA
        value["purposes"] = {
            category: {"count": count} for category, count in self._purpose_counts_in(value).items()
        }
        return value

    @contextmanager
    def _file_lock(self) -> Iterator[None]:
        try:
            import fcntl
        except ImportError as error:
            raise RuntimeStateError("review admission locking requires POSIX") from error
        parent = self.path.parent
        ledger_path = Path(os.path.abspath(self.path))
        # A symlink anywhere above the ledger can redirect both the ledger and
        # its lock outside the trusted trial directory. Check every ancestor
        # from the filesystem root to the lock file, not only the direct parent.
        for ancestor in reversed(ledger_path.parents):
            if ancestor.is_symlink():
                raise RuntimeStateError("review admission ancestor or parent directory is a symlink")
        if ledger_path.is_symlink():
            raise RuntimeStateError("review admission file is a symlink")
        parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_name(self.path.name + ".lock")
        lock_path = Path(os.path.abspath(lock_path))
        if lock_path.is_symlink():
            raise RuntimeStateError("review admission lock file is a symlink")
        try:
            descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        except OSError as error:
            if error.errno == errno.ELOOP:
                raise RuntimeStateError("review admission lock file is a symlink") from error
            raise
        with os.fdopen(descriptor, "a+b") as lock:
            deadline = time.monotonic() + self.lock_timeout_seconds
            while True:
                try:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise RuntimeStateError("review admission lock wait timed out") from None
                    time.sleep(_LOCK_POLL_SECONDS)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _reload(self) -> None:
        self._value = self._load()
        if self._identity is not None and self._value.get("identity") is None:
            self._value["identity"] = self._identity

    @staticmethod
    def key(state: Any) -> tuple[str, dict[str, Any]]:
        material = {
            "tree": getattr(state.candidate, "tree", None),
            "contract_revision": getattr(state, "contract_revision", None),
            "checks": list(getattr(state, "checks", ()) or ()),
            "user_decisions": list(getattr(state, "user_decisions", ()) or ()),
        }
        encoded = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str).encode()
        return hashlib.sha256(encoded).hexdigest(), material

    def _limit(self, category: str) -> int:
        return self.acceptance_limit if category == ACCEPTANCE else self.diagnosis_limit

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._value["purposes"] = {
            category: {"count": count} for category, count in self._purpose_counts().items()
        }
        write_json_atomic(self.path, json.loads(json.dumps(self._value, default=str, sort_keys=True)))

    def admit(
        self,
        state: Any,
        *,
        purpose: str,
        full: bool = False,
        force: bool = False,
        check: Callable[[], str | None] | None = None,
    ) -> dict[str, Any]:
        """Reserve one purpose-specific Reviewer attempt; never reset counts."""
        key, material = self.key(state)
        category = _category(purpose)
        with self._lock, self._file_lock():
            self._reload()
            counts = self._purpose_counts()
            # A budget stop may expire while this admission waits for the lock.
            # Re-evaluate it under the lock before charging the durable ledger.
            stopped = check() if check is not None else None
            if stopped:
                return {
                    "admitted": False,
                    "reason": stopped,
                    "stop": stopped,
                    "purpose": purpose,
                    "category": category,
                    "input_sha256": key,
                    "trial": self._value["trial"],
                    "same_input": self._value["keys"]
                    .get(key, {})
                    .get("purposes", {})
                    .get(category, {})
                    .get("count", 0),
                    "trial_limit": self.diagnosis_limit,
                    "diagnosis": counts[DIAGNOSIS],
                    "diagnosis_limit": self.diagnosis_limit,
                    "acceptance": counts[ACCEPTANCE],
                    "acceptance_limit": self.acceptance_limit,
                    "acceptance_remaining": max(0, self.acceptance_limit - counts[ACCEPTANCE]),
                    "purpose_count": counts[category],
                    "purpose_limit": self._limit(category),
                    "purpose_remaining": max(0, self._limit(category) - counts[category]),
                    "same_input_limit": self.same_input_limit,
                }
            trial = self._value["trial"]
            row = self._value["keys"].get(key, {"count": 0, **material})
            self._migrate_row(row)
            purpose_row = row["purposes"].get(category, {"count": 0})
            purpose_count = counts[category]
            purpose_limit = self._limit(category)
            admission_id: str | None = None
            stop: str | None = None
            if purpose_count >= purpose_limit:
                reason = f"{category}_fuse"
                admitted = False
                stop = FUSE_REASON
            elif purpose_row["count"] >= self.same_input_limit:
                reason = "same_input_limit"
                admitted = False
                stop = "review_limit_reached"
            else:
                admitted = True
                reason = None
                admission_id = uuid.uuid4().hex
                self._value["trial"] = trial + 1
                row["count"] = row["count"] + 1
                purpose_row.update(count=purpose_row["count"] + 1, last_purpose=purpose, last_full=bool(full))
                row["purposes"][category] = purpose_row
                self._value["keys"][key] = row
                admissions = list(self._value.get("admissions", []))
                admissions.append(
                    {
                        "admission_id": admission_id,
                        "purpose": purpose,
                        "category": category,
                        "full": bool(full),
                        "force": bool(force),
                        "input_sha256": key,
                        "tree": material["tree"],
                        "admitted_at": time.time(),
                    }
                )
                self._value["admissions"] = admissions[-128:]
                purpose_count += 1
                counts[category] = purpose_count
            if not admitted:
                refusal = {
                    "input_sha256": key,
                    "purpose": purpose,
                    "category": category,
                    "full": bool(full),
                    "force": bool(force),
                    "reason": reason,
                    "stop": stop,
                    "tree": material["tree"],
                    "contract_revision": material["contract_revision"],
                }
                self._value["refusals"] = [*self._value["refusals"][-(_MAX_REFUSALS - 1) :], refusal]
            self._write()
            return {
                "admitted": admitted,
                "reason": reason,
                "stop": stop,
                "admission_id": admission_id,
                "purpose": purpose,
                "category": category,
                "input_sha256": key,
                "trial": self._value["trial"],
                "same_input": purpose_row["count"],
                "trial_limit": self.diagnosis_limit,
                "diagnosis": counts[DIAGNOSIS],
                "diagnosis_limit": self.diagnosis_limit,
                "acceptance": counts[ACCEPTANCE],
                "acceptance_limit": self.acceptance_limit,
                "acceptance_remaining": max(0, self.acceptance_limit - counts[ACCEPTANCE]),
                "purpose_count": purpose_count,
                "purpose_limit": purpose_limit,
                "purpose_remaining": max(0, purpose_limit - purpose_count),
                "same_input_limit": self.same_input_limit,
            }

    def payload(self) -> dict[str, Any]:
        with self._lock, self._file_lock():
            self._reload()
            value = json.loads(json.dumps(self._value, default=str, sort_keys=True))
            value["limits"] = {
                DIAGNOSIS: self.diagnosis_limit,
                ACCEPTANCE: self.acceptance_limit,
                "same_input": self.same_input_limit,
            }
            return value
