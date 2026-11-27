# Copyright (c) 2026 Chrys. All rights reserved.

"""Elapsed wall time of complete Core operations on sealed candidates."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import time
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

from jiuwenswarm.pact_core.artifacts import write_json_atomic
from jiuwenswarm.pact_core.runtime import WorkSnapshot

from .review_governance import DEFAULT_PUBLIC_CHECK_SECONDS, RECENT_SAMPLES


class VerificationTimeUnavailable(Exception):
    """The remaining check window is shorter than an observed entire verification."""

    def __init__(self, message, *, deadline, unchanged_revision=None):
        super().__init__(message)
        self.deadline = deadline
        self.unchanged_revision = unchanged_revision


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _scope(state: WorkSnapshot) -> dict:
    # Preserve execution-affecting method changes while leaving registration
    # provenance, per-candidate IDs and previous review receipts out of the key.
    methods = [
        {k: check.get(k) for k in ("name", "command", "requirement", "files", "fingerprint", "disposition", "version")}
        for check in state.checks
        if check.get("disposition") != "suspended"
    ]
    return {
        "contract": _digest(
            [
                state.contract,
                state.constraints,
                state.policy.payload(),
                state.acceptance_brief.get("text"),
                state.user_decisions,
                state.unresolved_assumptions,
            ]
        ),
        "methods": _digest(methods),
    }


class AcceptanceTimings:
    """Per-trial controller evidence. Core receipts remain authoritative."""

    def __init__(self, output: Path, identity: dict, current: Callable[[], WorkSnapshot]):
        self.output = output.resolve()
        self.identity = identity
        self.current = current
        self.root = self.output / "operation-timings" / _digest(identity)

    def measure(self, operation, method, workspace, work_id, expected_revision, *, deadline, **kwargs):
        attempt = uuid4().hex
        path = self.root / (attempt + ".json")
        previous = self.current()
        scope = _scope(previous)
        row = {
            "schema": "long-horizon/operation-timing/v1",
            "operation": operation,
            "attempt": attempt,
            "identity": self.identity,
            "workspace": str(workspace),
            "work_id": work_id,
            "input_revision": expected_revision,
            "scope": scope,
            "candidate": previous.candidate.checkpoint,
            "tree": previous.candidate.tree,
            "started_at": time.time(),
            "deadline": deadline,
            "status": "started",
        }
        if (
            operation == "verify"
            and deadline is not None
            and previous.work_id == work_id
            and previous.revision == expected_revision
        ):
            # Revalidate time immediately before starting any Core operation.
            # Failed or slow checks on the previous tree still inform the next
            # Worker budget, but a repair may remove their cause entirely.
            observed = [elapsed for _, elapsed, _ in self._samples("verify", previous, tree=previous.candidate.tree)]
            remaining = deadline - time.time()
            if remaining <= 0 or (observed and max(observed) >= remaining):
                self._write(
                    path,
                    {
                        **row,
                        "status": "insufficient_time",
                        "remaining_seconds": remaining,
                        "observed_seconds": max(observed, default=0.0),
                    },
                )
                raise VerificationTimeUnavailable(
                    "insufficient time for a complete public verification",
                    deadline=deadline,
                    unchanged_revision=expected_revision,
                )
        self._write(path, row)
        start = time.monotonic()
        try:
            state = method(work_id, expected_revision=expected_revision, deadline=deadline, **kwargs)
        except BaseException as error:
            row.update(
                status="error",
                error=type(error).__name__,
                elapsed_seconds=time.monotonic() - start,
                finished_at=time.time(),
            )
            self._write(path, row)
            raise
        elapsed = time.monotonic() - start
        ended = time.time()
        row.update(
            status="unbound",
            elapsed_seconds=elapsed,
            finished_at=ended,
            result_revision=state.revision,
            finished_after_deadline=deadline is not None and ended > deadline,
        )
        if (
            state.work_id == work_id
            and state.revision == expected_revision + 1
            and previous.revision == expected_revision
            and state.candidate == previous.candidate
        ):
            try:
                self._bind(row, state, kwargs.get("reviewer"))
            except (OSError, ValueError, KeyError, AttributeError, TypeError):
                row["status"] = "receipt_unavailable"
        self._write(path, row)
        # Core has already committed this revision. Give its result back to the
        # driver, which owns both the remaining review window and the hard cutoff.
        return state

    def _bind(self, row, state, reviewer):
        current = json.loads((state.artifacts / "work-state.json").read_bytes())
        if current["revision"] != state.revision:
            return
        if row["operation"] == "accept":
            results = getattr(reviewer, "results", ())
            row["reviewer_calls"] = len(results)
            row["completed_reviewer_calls"] = sum(r.status == "completed" for r in results)
            row["reviewer_stopped"] = getattr(reviewer, "stopped", None)
            row["purpose"] = getattr(reviewer, "purpose", None)
            ref = current["review_ref"]
        else:
            if not state.verification or state.verification.candidate != state.candidate:
                return
            ref = current["verification_ref"]
        receipt = Path(row["workspace"]) / ref
        if receipt.parent != state.artifacts / "evidence" or receipt.resolve() != receipt:
            return
        raw = receipt.read_bytes()
        row.update(receipt=receipt.name, receipt_sha256=hashlib.sha256(raw).hexdigest())
        row["status"] = self._receipt_status(row, json.loads(raw))
        if row["status"] == "accepted" and (
            not state.acceptance
            or state.acceptance.artifact != receipt
            or state.acceptance.candidate != state.candidate
        ):
            row["status"] = "unbound"

    def _receipt_status(self, row, core):
        workspace = Path(row["workspace"])
        evidence = workspace / ".pact/runtime/campaigns" / row["work_id"] / "evidence"
        candidate_path = workspace / core["candidate_ref"]
        if candidate_path.parent != evidence or candidate_path.resolve() != candidate_path:
            return "unbound"
        record = json.loads(candidate_path.read_bytes())
        candidate = record["candidate"]
        if (
            record["kind"] != "candidate"
            or record["campaign_id"] != row["work_id"]
            or core["kind"] != row["operation"]
            or core["campaign_id"] != row["work_id"]
            or candidate["source_workspace"] != row["workspace"]
            or candidate["checkpoint"] != row["candidate"]
            or candidate["tree"] != row["tree"]
        ):
            return "unbound"
        if core["kind"] == "accept":
            if (
                core["complete"] is True
                and not core["reasons"]
                and core["outcome"]["promotion_ready"] is True
                and core["outcome"]["candidate_checkpoint"] == row["candidate"]
            ):
                # A full acceptance can include a Core-owned verdict repair.
                # The sample covers the entire operation and every reviewer call.
                if (
                    row["purpose"] == "acceptance"
                    and row["reviewer_calls"] > 0
                    and row["completed_reviewer_calls"] == row["reviewer_calls"]
                    and not row["reviewer_stopped"]
                    and not row["finished_after_deadline"]
                ):
                    return "accepted"
                return "unqualified_acceptance"
            return "not_accepted"
        return "verified_" + core["verification"]["status"]

    def _write(self, path, row):
        try:
            if path.resolve() != path:
                raise OSError("redirected timing path")
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            write_json_atomic(path, row)
        except OSError:
            logging.getLogger(__name__).warning("Could not persist operation timing", exc_info=True)

    def recent(self, operation, state=None) -> list[float]:
        return [elapsed for _, elapsed, _ in self._samples(operation, state)]

    def worker_check_times(self) -> list[float]:
        """Failed or timed-out checks may have stopped early. They can only increase W."""
        samples = self._samples("verify")
        if not samples:
            return []
        passed = [elapsed for _, elapsed, status in samples if status == "verified_passed"]
        completed = sum(passed) / len(passed) if passed else DEFAULT_PUBLIC_CHECK_SECONDS
        lower_bound = max((elapsed for _, elapsed, status in samples if status != "verified_passed"), default=0.0)
        return [max(completed, lower_bound)]

    def _samples(self, operation, state=None, *, tree=None):
        """Recover comparable physical receipts. A timeout is a lower bound for W only."""
        if not self.root.exists() or self.root.resolve() != self.root:
            return []
        scope = _scope(state if state is not None else self.current())
        eligible = {"accepted"} if operation == "accept" else {"verified_passed", "verified_failed", "verified_timeout"}
        samples = {}
        for path in self.root.glob("*.json"):
            try:
                if path.is_symlink():
                    continue
                row = json.loads(path.read_bytes())
                if (
                    row["schema"] != "long-horizon/operation-timing/v1"
                    or row["identity"] != self.identity
                    or row["attempt"] != path.stem
                    or row["operation"] != operation
                    or row["status"] not in eligible
                    or (tree is not None and row["tree"] != tree)
                    or row["scope"] != scope
                    or row["result_revision"] != row["input_revision"] + 1
                ):
                    continue
                elapsed, ended = row["elapsed_seconds"], row["finished_at"]
                if not all(type(v) in (int, float) and math.isfinite(v) and v > 0 for v in (elapsed, ended)):
                    continue
                workspace = Path(row["workspace"])
                own_workspace = workspace == Path(self.identity["workspace"])
                external_mirror = (
                    self.identity["external"]
                    and workspace.parent == self.output.parent
                    and re.fullmatch(re.escape("." + self.output.name + "-core-") + "[0-9a-f]{20}", workspace.name)
                )
                if not (own_workspace or external_mirror):
                    continue
                work_id, name = row["work_id"], row["receipt"]
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", work_id) or Path(name).name != name:
                    continue
                ref = workspace / ".pact/runtime/campaigns" / work_id / "evidence" / name
                if ref.resolve() != ref:
                    continue
                raw = ref.read_bytes()
                if hashlib.sha256(raw).hexdigest() != row["receipt_sha256"]:
                    continue
                if self._receipt_status(row, json.loads(raw)) != row["status"]:
                    continue
                # Repeated confirmation of the same tree cannot manufacture a
                # representative acceptance history. Preserve its slowest cost.
                key = row["tree"] if operation == "accept" else ref
                old = samples.get(key, (0, 0, row["status"]))
                samples[key] = (max(old[0], ended), max(old[1], elapsed), row["status"])
            except (OSError, AttributeError, KeyError, TypeError, ValueError):
                continue
        return sorted(samples.values())[-RECENT_SAMPLES:]
