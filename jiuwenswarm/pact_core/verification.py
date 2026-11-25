"""Deterministic verification of one checkpoint (never an agent turn).

Two layers (PRD FR-4): built-in structural checks that always run, plus an
optional task command whose exit code is the base protocol — exit 0 means
``passed``, non-zero ``failed``, timeout ``timeout``, and no configured
command ``unavailable``. A JSON status line on stdout is an optional detail
enhancement, never a requirement.

This module must stay free of adapter, prompt, reviewer, and LLM imports
(AGENTS.md invariant 2); an architecture test locks that boundary.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

from .schemas import (
    FinalValidationResult,
    VerificationCheck,
    VerificationResult,
    VerificationStatus,
)

_TAIL_CHARS = 4000


def _git(workspace: Path, *args: str) -> subprocess.CompletedProcess[str]:
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("verification requires git on PATH")
    env = os.environ.copy()
    env["GIT_OPTIONAL_LOCKS"] = "0"
    result = subprocess.run(
        [git, *args],
        cwd=workspace,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=False,
        check=False,
    )
    return subprocess.CompletedProcess(
        result.args, result.returncode,
        result.stdout.decode('utf-8', errors='surrogateescape'),
        result.stderr.decode('utf-8', errors='surrogateescape'),
    )


def run_builtin_checks(
    *,
    workspace: Path,
    checkpoint_commit: str,
    baseline_commit: str,
    namespace_violations: list[str],
    required_artifacts: list[Path],
) -> list[VerificationCheck]:
    """Run the four always-on evidence/structural checks against one checkpoint.

    Content and formatting requirements belong to the explicit task command.
    """
    checks: list[VerificationCheck] = []

    readable = _git(workspace, "cat-file", "-e", f"{checkpoint_commit}^{{commit}}")
    checks.append(
        VerificationCheck(
            name="checkpoint_readable",
            passed=readable.returncode == 0,
            detail="" if readable.returncode == 0 else readable.stderr.strip(),
        )
    )

    patch = _git(
        workspace,
        "diff",
        "--binary",
        "--full-index",
        "--no-ext-diff",
        baseline_commit,
        checkpoint_commit,
    )
    checks.append(
        VerificationCheck(
            name="patch_generatable",
            passed=patch.returncode == 0,
            detail="" if patch.returncode == 0 else patch.stderr.strip(),
        )
    )

    checks.append(
        VerificationCheck(
            name="namespace_clean",
            passed=not namespace_violations,
            detail=", ".join(namespace_violations),
        )
    )

    missing = [str(path) for path in required_artifacts if not path.is_file()]
    checks.append(
        VerificationCheck(
            name="required_artifacts_present",
            passed=not missing,
            detail=", ".join(missing),
        )
    )
    return checks


def _optional_status_line_check(stdout: str, exit_code: int) -> VerificationCheck | None:
    for line in reversed(stdout.splitlines()):
        stripped = line.strip()
        if not (stripped.startswith("{") and stripped.endswith("}")):
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return VerificationCheck(
                name="command_status_line",
                passed=exit_code == 0,
                detail=stripped[:_TAIL_CHARS],
            )
    return None


def _remaining_command_timeout(timeout_seconds: float, deadline: float | None) -> float:
    """Re-read the original UTC deadline after structural checks and spawn I/O."""
    if deadline is None:
        return timeout_seconds
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(deadline):
        raise ValueError("deadline must be finite UTC epoch seconds")
    return max(0.0, min(timeout_seconds, deadline - time.time()))


def _process_start_ticks(pid: int) -> int | None:
    """Return the Linux start tick used to reject a reused fixed-group PID."""
    try:
        raw = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8")
        fields = raw[raw.rindex(")") + 2 :].split()
        return int(fields[19])
    except (OSError, ValueError, IndexError):
        return None


_CHECK_RESIDUAL_GRACE_SECONDS = 10.0
_CHECK_RESIDUAL_KILL_GRACE_SECONDS = 0.5
_CHECK_GROUP_POLL_SECONDS = 0.5

_ACTIVE_CHECK_LOCK = threading.Lock()
_ACTIVE_CHECK_PROCESSES: dict[int, dict[str, object]] = {}
_ACTIVE_CHECK_WATCHERS: dict[int, threading.Event] = {}


def _check_group_empty(pid: int) -> bool:
    """Return whether every process in the recorded check group has exited."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        return True
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False


def _signal_recorded_group(pid: int, record: dict[str, object], number: int) -> bool:
    """Signal a group only while its exact record is still registered."""
    with _ACTIVE_CHECK_LOCK:
        if _ACTIVE_CHECK_PROCESSES.get(pid) is not record:
            return False
        try:
            os.killpg(pid, number)
        except (ProcessLookupError, PermissionError):
            return False
        record["cleanup_started"] = True
    return True


def signal_active_check_group(pgid: int, number: int) -> bool:
    """Signal a recorded check group under the registry lock.

    Clearing a record and signalling its group are mutually exclusive, so a
    group whose record has been removed cannot be signalled here afterwards.
    """
    if isinstance(pgid, bool) or not isinstance(pgid, int) or pgid <= 1:
        return False
    with _ACTIVE_CHECK_LOCK:
        record = _ACTIVE_CHECK_PROCESSES.get(pgid)
        if record is None:
            return False
        try:
            os.killpg(pgid, number)
        except (ProcessLookupError, PermissionError):
            return False
        record["cancelled"] = True
    return True


def _watch_check_leader(
    pid: int,
    record: dict[str, object],
    stop: threading.Event,
    returned: threading.Event,
) -> None:
    """Track the group without killing a check that has not returned yet.

    While the check is still running, its own timeout or cancellation owns the
    process group; this watcher only probes whether the group is empty. Once
    ``_run_task_command`` has fixed the result it sets ``returned``, and only
    then does the residual grace start for background members left behind.
    """
    while not stop.is_set() and not returned.is_set():
        if _check_group_empty(pid):
            _unregister_check_process(pid, record)
            return
        stop.wait(_CHECK_GROUP_POLL_SECONDS)
    if stop.is_set():
        return
    # The command has returned and its result is fixed. Start residual
    # cleanup at that moment, not when the leader happened to exit.
    if _check_group_empty(pid):
        _unregister_check_process(pid, record)
        return
    grace_deadline = time.monotonic() + _CHECK_RESIDUAL_GRACE_SECONDS
    while not stop.is_set():
        if _check_group_empty(pid):
            _unregister_check_process(pid, record)
            return
        remaining = grace_deadline - time.monotonic()
        if remaining <= 0:
            break
        stop.wait(min(_CHECK_GROUP_POLL_SECONDS, remaining))
    if stop.is_set():
        return
    # A check must not leave background work behind after it returns. Bound
    # the residual group with the same TERM-then-KILL contract.
    _signal_recorded_group(pid, record, signal.SIGTERM)
    kill_deadline = time.monotonic() + _CHECK_RESIDUAL_KILL_GRACE_SECONDS
    while not stop.is_set() and time.monotonic() < kill_deadline:
        if _check_group_empty(pid):
            break
        stop.wait(0.05)
    if stop.is_set():
        return
    if not _check_group_empty(pid):
        _signal_recorded_group(pid, record, signal.SIGKILL)
        final_deadline = time.monotonic() + max(_CHECK_RESIDUAL_KILL_GRACE_SECONDS, 1.0)
        while not stop.is_set() and time.monotonic() < final_deadline:
            if _check_group_empty(pid):
                break
            stop.wait(0.05)
    # Never retain a record and watcher forever, even if an unkillable
    # member keeps the group alive.
    _unregister_check_process(pid, record)


def _register_check_process(
    process: subprocess.Popen,
) -> tuple[dict[str, object], threading.Event]:
    record = {
        "pid": process.pid,
        "pgid": process.pid,
        "started_at": time.time(),
        "start_ticks": _process_start_ticks(process.pid),
    }
    stop = threading.Event()
    returned = threading.Event()
    with _ACTIVE_CHECK_LOCK:
        _ACTIVE_CHECK_PROCESSES[process.pid] = record
        if isinstance(process.pid, int) and not isinstance(process.pid, bool):
            _ACTIVE_CHECK_WATCHERS[process.pid] = stop
    if not isinstance(process.pid, int) or isinstance(process.pid, bool):
        return record, returned
    threading.Thread(
        target=_watch_check_leader,
        args=(process.pid, record, stop, returned),
        name=f"check-leader-{process.pid}",
        daemon=True,
    ).start()
    return record, returned


def _unregister_check_process(pid: int, record: dict[str, object] | None = None) -> None:
    with _ACTIVE_CHECK_LOCK:
        if record is not None and _ACTIVE_CHECK_PROCESSES.get(pid) is not record:
            return
        _ACTIVE_CHECK_PROCESSES.pop(pid, None)
        stop = _ACTIVE_CHECK_WATCHERS.pop(pid, None)
    if stop is not None:
        stop.set()


def active_check_processes() -> tuple[dict[str, object], ...]:
    """Return the exact check groups started by this Core process."""
    with _ACTIVE_CHECK_LOCK:
        return tuple(dict(record) for record in _ACTIVE_CHECK_PROCESSES.values())


def _communicate_after_kill(process: subprocess.Popen) -> tuple[str, str]:
    """Reap a killed check without ever blocking forever on inherited pipes."""
    try:
        stdout, stderr = process.communicate(timeout=5.0)
        return stdout or "", stderr or ""
    except subprocess.TimeoutExpired:
        for stream in (process.stdout, process.stderr):
            try:
                stream.close()
            except (OSError, ValueError):
                pass
        try:
            process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            pass
        return "", "check_output_abandoned: process group did not close after SIGKILL"


def _run_task_command(
    command: str,
    *,
    workdir: Path,
    timeout_seconds: float,
    env_extra: dict[str, str],
    deadline: float | None = None,
) -> tuple[VerificationStatus, int | None, str, str, float]:
    env = os.environ.copy()
    env.update(env_extra)
    started = time.monotonic()
    if _remaining_command_timeout(timeout_seconds, deadline) <= 0:
        return "timeout", None, "", "budget_exhausted: deadline reached before verification command", 0.0
    process = subprocess.Popen(
        command,
        shell=True,
        cwd=workdir,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    record, returned = _register_check_process(process)
    result: tuple[VerificationStatus, int | None, str, str, float] | None = None
    try:
        try:
            remaining = _remaining_command_timeout(timeout_seconds, deadline)
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, remaining)
            stdout, stderr = process.communicate(timeout=remaining)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = _communicate_after_kill(process)
            duration = time.monotonic() - started
            result = ("timeout", None, stdout or "", stderr or "", duration)
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            _communicate_after_kill(process)
            raise
        else:
            duration = time.monotonic() - started
            status: VerificationStatus = "passed" if process.returncode == 0 else "failed"
            result = (status, process.returncode, stdout, stderr, duration)
    finally:
        # Fix the outcome before releasing the residual grace. A cancellation
        # that signalled this group while the command was still running must
        # not inherit the leader's success exit code.
        with _ACTIVE_CHECK_LOCK:
            interrupted = bool(record.get("cancelled") or record.get("cleanup_started"))
            record["result_determined"] = True
        returned.set()
        if result is not None and interrupted and result[0] != "timeout":
            _status, _exit_code, stdout, stderr, duration = result
            detail = "check_interrupted_by_cleanup: process group signalled before the check returned"
            result = (
                "timeout",
                None,
                stdout,
                stderr + ("\n" if stderr else "") + detail,
                duration,
            )
        # Keep the record while descendants remain in the group so cancellation
        # can still signal it. The watcher releases it once the group is empty.
        if _check_group_empty(process.pid):
            _unregister_check_process(process.pid, record)
    assert result is not None
    return result


def run_verification(
    *,
    workspace: Path,
    workdir: Path,
    checkpoint_commit: str,
    baseline_commit: str,
    namespace_violations: list[str],
    required_artifacts: list[Path],
    command: str | None,
    timeout_seconds: float,
    loop_dir: Path,
    round_number: int,
    patch_path: Path,
    deadline: float | None = None,
) -> VerificationResult:
    """Verify one checkpoint: built-in checks first, then the task command.

    ``workdir`` must be a disposable checkout that exactly matches
    ``checkpoint_commit``; the caller owns its creation and destruction.
    """
    if _remaining_command_timeout(timeout_seconds, deadline) <= 0:
        return VerificationResult(
            status="timeout",
            command=command or "",
            stderr_tail="budget_exhausted: deadline reached before public verification",
        )
    checks = run_builtin_checks(
        workspace=workspace,
        checkpoint_commit=checkpoint_commit,
        baseline_commit=baseline_commit,
        namespace_violations=namespace_violations,
        required_artifacts=required_artifacts,
    )
    if not all(check.passed for check in checks):
        return VerificationResult(status="failed", checks=checks, command=command or "")

    if command is None:
        return VerificationResult(status="unavailable", checks=checks)

    status, exit_code, stdout, stderr, duration = _run_task_command(
        command,
        workdir=workdir,
        timeout_seconds=timeout_seconds,
        deadline=deadline,
        env_extra={
            "PACT_LOOP_DIR": str(loop_dir),
            "PACT_ROUND": str(round_number),
            "PACT_PATCH_PATH": str(patch_path),
        },
    )
    if exit_code is not None:
        status_line = _optional_status_line_check(stdout, exit_code)
        if status_line is not None:
            checks = [*checks, status_line]
    return VerificationResult(
        status=status,
        checks=checks,
        command=command,
        exit_code=exit_code,
        duration_seconds=duration,
        stdout_tail=stdout[-_TAIL_CHARS:],
        stderr_tail=stderr[-_TAIL_CHARS:],
    )


def run_final_validation(
    *,
    command: str | None,
    workdir: Path,
    timeout_seconds: float,
    loop_dir: Path,
    baseline_commit: str,
    candidate_commit: str,
    patch_path: Path,
    deadline: float | None = None,
) -> FinalValidationResult:
    """Run one opaque terminal validator after agent convergence.

    The command runs at the candidate checkout for convenience, but any
    validator that compares revisions must use the explicit baseline and
    candidate environment variables rather than inferring meaning from HEAD.
    This result is controller-only evidence and must never enter a role prompt.
    """
    if command is None:
        return FinalValidationResult(status="not_configured")

    command_sha256 = hashlib.sha256(command.encode("utf-8")).hexdigest()
    try:
        status, exit_code, stdout, stderr, duration = _run_task_command(
            command,
            workdir=workdir,
            timeout_seconds=timeout_seconds,
            deadline=deadline,
            env_extra={
                "PACT_LOOP_DIR": str(loop_dir),
                "PACT_BASELINE_COMMIT": baseline_commit,
                "PACT_CANDIDATE_COMMIT": candidate_commit,
                "PACT_FINAL_PATCH_PATH": str(patch_path),
            },
        )
    except OSError as exc:
        return FinalValidationResult(
            status="unavailable",
            command_sha256=command_sha256,
            stderr_tail=str(exc)[-_TAIL_CHARS:],
        )
    return FinalValidationResult(
        status=status,
        command_sha256=command_sha256,
        exit_code=exit_code,
        duration_seconds=duration,
        stdout_tail=stdout[-_TAIL_CHARS:],
        stderr_tail=stderr[-_TAIL_CHARS:],
    )
