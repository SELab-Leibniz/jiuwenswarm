# Copyright (c) 2026 Chrys. All rights reserved.

"""Host-owned check observations; a stderr pointer carries no classification authority."""

from __future__ import annotations

import json
import math
import os
import re
import shlex
import stat
import time
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from uuid import uuid4

SCHEMA = "harbor-public-check/outcome/v1"
POINTER = "LH_PUBLIC_CHECK_RECEIPT_V1 "
ID = re.compile(r"[0-9a-f]{32}")
CHECKPOINT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
KINDS = frozenset(
    {"public_test_passed", "public_test_failed", "infrastructure_error", "agent_deadline_exhausted",
     "public_check_timeout", "unknown"}
)
EXECUTION = frozenset({"not_started", "started", "completed", "unknown"})
RPC_CLIENT = "/opt/lh-main/src/long_horizon/harbor_unified/public_check_rpc_client.py"
RPC_SOCKET = "/lh-public-check-rpc/s"


def is_rpc_public_check_command(command: str | None) -> bool:
    """Recognize only the existing six-argv frozen frontend; execute nothing."""
    if not isinstance(command, str):
        return False
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    return (
        len(words) == 6
        and Path(words[0]).is_absolute()
        and words[1:] == ["-I", "-B", RPC_CLIENT, "--socket", RPC_SOCKET]
    )


def receipts_for_public_check_command(command: str | None, *, deadline_unix: float) -> TrustedCheckReceipts | None:
    """Bind existing runtime metadata once; None is not proof a check ran."""
    if not is_rpc_public_check_command(command):
        return None
    return TrustedCheckReceipts.bind(Path(RPC_SOCKET).parent / "outcomes", deadline_unix=deadline_unix)


@dataclass(frozen=True)
class PublicCheckOutcome:
    error_kind: str
    execution_state: str
    request_id: str
    candidate_snapshot_id: str | None
    stage: str
    return_code: int
    test_return_code: int | None
    remaining_seconds: float | None
    post_validation_complete: bool
    cleanup_complete: bool
    tasks_max: int | None = None
    pids_peak: int | None = None
    oom: int | None = None
    oom_kill: int | None = None
    oom_delta: int | None = None
    oom_kill_delta: int | None = None

    @property
    def stop_unresolved(self) -> bool:
        # A bounded check timeout is an explicit diagnostic result, not a
        # reason to strand the invocation. Incomplete cleanup still holds it.
        if self.error_kind == "public_check_timeout" and self.cleanup_complete is True:
            return False
        return self.error_kind not in {"public_test_passed", "public_test_failed"}

    @property
    def continue_implementation(self) -> bool:
        return (
            self.error_kind in {"public_test_failed", "public_check_timeout"}
            and self.cleanup_complete is True
            and (self.error_kind == "public_check_timeout" or self.execution_state == "completed")
            and (self.error_kind == "public_check_timeout" or self.post_validation_complete is True)
        )


@dataclass
class CheckTrace:
    request_id: str
    deadline_unix: float | None = None
    candidate_snapshot_id: str | None = None
    stage: str = "request"
    execution_state: str = "not_started"
    test_return_code: int | None = None
    post_validation_complete: bool = False
    cleanup_complete: bool = False
    uncertainty_notice: bool = False
    recovered_cleanup_error: dict | None = None
    timeout_cause: str | None = None
    tasks_max: int | None = None
    pids_peak: int | None = None
    oom: int | None = None
    oom_kill: int | None = None
    oom_delta: int | None = None
    oom_kill_delta: int | None = None
    cleanup_incomplete_reason: str | None = None

    @classmethod
    def new(cls) -> CheckTrace:
        return cls(uuid4().hex)

    def outcome(self, *, failed: bool, deadline_error: bool = False) -> PublicCheckOutcome:
        remaining = self.deadline_unix - time.time() if self.deadline_unix is not None else None
        complete = self.execution_state == "completed" and self.post_validation_complete and self.cleanup_complete
        kind = (
            "infrastructure_error"
            if failed
            else ("public_test_passed" if self.test_return_code == 0 else "public_test_failed")
        )
        if deadline_error and self.timeout_cause == "public_check":
            kind = "public_check_timeout"
        elif deadline_error and self.timeout_cause == "agent_deadline":
            kind = "agent_deadline_exhausted"
        elif deadline_error and remaining is not None and remaining <= 0:
            kind = "agent_deadline_exhausted"
        elif deadline_error:
            # A typed timeout that fires while the original agent clock still
            # has budget can only be the bounded per-check clock.
            kind = "public_check_timeout"
        if not failed and (not complete or self.candidate_snapshot_id is None or self.uncertainty_notice):
            kind = "unknown"
        return PublicCheckOutcome(
            kind,
            self.execution_state,
            self.request_id,
            self.candidate_snapshot_id,
            self.stage,
            125 if failed or kind == "unknown" else self.test_return_code,
            self.test_return_code,
            remaining,
            self.post_validation_complete,
            self.cleanup_complete,
            self.tasks_max,
            self.pids_peak,
            self.oom,
            self.oom_kill,
            self.oom_delta,
            self.oom_kill_delta,
        )


def uncertainty_notice(stdout: str | None) -> bool:
    """An explicit public-recipe uncertainty is a veto, never passing authority."""
    if not isinstance(stdout, str):
        return False
    for line in reversed(stdout[-16384:].splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if type(value) is dict and (
            value.get("status") == "unknown"
            and value.get("kind") == "check_infrastructure_unknown"
            or value.get("kind") == "global_budget_exhausted"
        ):
            return True
    return False


def _read_bounded(path: Path) -> bytes:
    if path.is_symlink() or path.resolve(strict=True) != path or not path.is_file() or path.stat().st_size > 16384:
        raise ValueError("noncanonical_check_receipt")
    with path.open("rb") as stream:
        raw = stream.read(16385)
    if len(raw) > 16384:
        raise ValueError("oversized_check_receipt")
    return raw


def candidate_checkpoint(host: Path, base: Path, alias_base: str) -> str | None:
    """Observe only detached HEAD within the already validated fixed candidate bind."""
    try:
        git = host / ".git"
        if git.is_symlink():
            return None
        if git.is_file():
            raw = _read_bounded(git).decode("ascii").strip()
            if not raw.startswith("gitdir: "):
                return None
            target = PurePosixPath(raw[8:])
            alias = PurePosixPath(alias_base)
            if target.is_absolute():
                if not target.is_relative_to(alias) or ".." in target.parts:
                    return None
                git = base / str(target.relative_to(alias))
            else:
                if ".." in target.parts:
                    return None
                git = host / str(target)
        if not git.is_relative_to(base) or git.resolve(strict=True) != git:
            return None
        head = _read_bounded(git / "HEAD").decode("ascii").strip()
        return "git:" + head if CHECKPOINT.fullmatch(head) else None
    except (OSError, UnicodeError, ValueError):
        return None


class OutcomeWriter:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(mode=0o711)
        # Only this newly-created metadata directory changes mode. Its existing
        # RPC parent remains private; the Main bind is read-only. A task UID may
        # differ from the host writer and needs traversal, not directory listing.
        root.chmod(0o711)

    def write(self, trace: CheckTrace, outcome: PublicCheckOutcome, error: dict | None) -> dict:
        payload = {
            "schema": SCHEMA,
            "producer": "host_public_check_rpc",
            "deadline_unix": trace.deadline_unix,
            "outcome": asdict(outcome),
            "error": error,
            "untrusted_result_veto": trace.uncertainty_notice,
            "recovered_cleanup_error": trace.recovered_cleanup_error,
        }
        path = self.root / (outcome.request_id + ".json")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o444)
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o444)
            json.dump(payload, stream, allow_nan=False, sort_keys=True)
            stream.write("\n")
        snapshot = outcome.candidate_snapshot_id
        if snapshot is not None and CHECKPOINT.fullmatch(snapshot.removeprefix("git:")):
            # A small host-owned current pointer prevents arbitrary stderr IDs
            # or older requests being accepted as this candidate's latest check.
            latest = self.root / (snapshot.removeprefix("git:") + ".latest")
            temporary = self.root / (outcome.request_id + ".latest.tmp")
            with temporary.open("x") as stream:
                json.dump({"request_id": outcome.request_id}, stream)
            temporary.chmod(0o444)
            os.replace(temporary, latest)
        return {"request_id": outcome.request_id}


def _readonly_mount(root: Path) -> bool:
    try:
        return bool(os.statvfs(root).f_flag & os.ST_RDONLY)
    except (OSError, AttributeError):
        return False


@dataclass(frozen=True)
class TrustedCheckReceipts:
    root: Path
    deadline_unix: float

    @classmethod
    def bind(cls, root: Path, *, deadline_unix: float) -> TrustedCheckReceipts | None:
        try:
            if (
                not root.is_absolute()
                or root.is_symlink()
                or root.resolve(strict=True) != root
                or not root.is_dir()
                or not _readonly_mount(root)
                or type(deadline_unix) not in (float, int)
                or not math.isfinite(deadline_unix)
            ):
                return None
        except OSError:
            return None
        return cls(root, deadline_unix)

    def read(self, request_id: str, checkpoint: str, exit_code: int) -> PublicCheckOutcome | None:
        if not ID.fullmatch(request_id) or not CHECKPOINT.fullmatch(checkpoint):
            return None
        try:
            latest = json.loads(_read_bounded(self.root / (checkpoint + ".latest")))
            if latest != {"request_id": request_id}:
                return None
            path = self.root / (request_id + ".json")
            if stat.S_IMODE(path.stat().st_mode) & 0o222:
                return None
            payload = json.loads(_read_bounded(path))
            if (
                payload["schema"] != SCHEMA
                or payload["producer"] != "host_public_check_rpc"
                or payload["deadline_unix"] != self.deadline_unix
            ):
                return None
            value = PublicCheckOutcome(**payload["outcome"])
            if (
                value.request_id != request_id
                or value.candidate_snapshot_id != "git:" + checkpoint
                or value.return_code != exit_code
                or type(value.return_code) is not int
                or value.error_kind not in KINDS
                or value.execution_state not in EXECUTION
            ):
                return None
            if value.error_kind in {"public_test_passed", "public_test_failed"} and not (
                value.execution_state == "completed"
                and value.post_validation_complete is True
                and value.cleanup_complete is True
                and value.test_return_code == exit_code
                and (value.error_kind == "public_test_passed") == (exit_code == 0)
            ):
                return None
            if value.stop_unresolved and value.return_code != 125:
                return None
            return value
        except (OSError, ValueError, TypeError, KeyError):
            return None


def decode_public_check_outcome(
    check_results, *, receipts: TrustedCheckReceipts | None, expected_candidate_checkpoint: str, verification=None
) -> PublicCheckOutcome | None:
    """Core check-results tuples carry an ID pointer; only a bound host file supplies data."""
    if receipts is None:
        return None
    if not check_results and verification is not None:
        # Real Core has no derived rows for the supplied command. Consume its
        # typed command result; do not append a manufactured derived check.
        if verification.candidate.checkpoint != expected_candidate_checkpoint:
            return None
        tail = verification.stderr_tail
        if type(verification.exit_code) is not int or not isinstance(tail, str):
            return None
        lines = tail.splitlines()
        if not lines or not lines[-1].startswith(POINTER):
            return None
        return receipts.read(lines[-1][len(POINTER) :], expected_candidate_checkpoint, verification.exit_code)
    found = []
    for result in check_results:
        if type(result) is not dict or type(result.get("exit_code")) is not int:
            continue
        tail = result.get("stderr_tail")
        if not isinstance(tail, str):
            continue
        lines = tail.splitlines()
        if not lines or not lines[-1].startswith(POINTER):
            continue
        request_id = lines[-1][len(POINTER) :]
        value = receipts.read(request_id, expected_candidate_checkpoint, result["exit_code"])
        if value is not None:
            found.append(value)
    return found[0] if len(found) == 1 else None
