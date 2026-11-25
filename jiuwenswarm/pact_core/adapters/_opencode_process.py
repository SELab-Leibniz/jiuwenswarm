"""Bounded subprocess supervision for one OpenCode turn."""

from __future__ import annotations

import hashlib
import os
import selectors
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ._opencode_events import (
    json_text_output,
    normalize_event,
    permission_pending_after,
)
from ..schemas import EventParseStatus, TurnTerminationReason


@dataclass(frozen=True, slots=True)
class OpenCodeProcessSpec:
    args: list[str]
    workdir: Path
    prompt: str
    env: dict[str, str]
    timeout_seconds: float
    no_progress_timeout_seconds: float
    capture_limit_bytes: int
    poll_seconds: float


@dataclass(frozen=True, slots=True)
class OpenCodeSpawnFailure:
    detail: str
    duration_seconds: float


@dataclass(frozen=True, slots=True)
class OpenCodeProcessOutcome:
    process_started_at: str
    process_exited_at: str
    process_pid: int
    prompt_sent_at: str
    first_activity_at: str | None
    last_activity_at: str | None
    provider_first_activity_at: str | None
    permission_pending_at: str | None
    termination_reason: TurnTerminationReason | None
    process_group_terminated: bool
    stdout_bytes: int
    stdout_sha256: str
    stderr_bytes: int
    stderr_sha256: str
    stdout_text: str
    exit_code: int | None
    duration_seconds: float
    session_id: str | None
    normalized_events: tuple[dict[str, object], ...]
    event_parse_status: EventParseStatus


OpenCodeProcessResult = OpenCodeSpawnFailure | OpenCodeProcessOutcome


def supervise_opencode_process(
    spec: OpenCodeProcessSpec,
    *,
    started_monotonic: float,
    popen: Callable[..., subprocess.Popen[str]],
    monotonic: Callable[[], float],
    utc_now: Callable[[], str],
    parse_json_events: Callable[[str], list[dict[str, object]]],
    kill_process_group: Callable[[subprocess.Popen[str]], bool],
) -> OpenCodeProcessResult:
    """Run one process while bounding capture and classifying stalls."""
    try:
        proc = popen(
            spec.args,
            cwd=spec.workdir,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=spec.env,
            start_new_session=True,
        )
    except OSError as exc:
        return OpenCodeSpawnFailure(
            detail=str(exc),
            duration_seconds=monotonic() - started_monotonic,
        )

    process_started_at = utc_now()
    prompt_sent_at = utc_now()
    hard_deadline = started_monotonic + spec.timeout_seconds
    last_activity_monotonic = monotonic()
    first_activity_at: str | None = None
    last_activity_at: str | None = None
    provider_first_activity_at: str | None = None
    permission_pending_at: str | None = None
    stdout_buffer = bytearray()
    stderr_buffer = bytearray()
    stdout_count = 0
    stderr_count = 0
    stdout_hash = hashlib.sha256()
    stderr_hash = hashlib.sha256()
    stdout_json_buffer = b""
    stdout_text_parts: list[str] = []
    normalized_events: list[dict[str, object]] = []
    session_ids: set[str] = set()
    event_parse_issue = False
    termination_reason: TurnTerminationReason | None = None
    process_group_terminated = False
    capture_limit_exceeded = False
    exit_code: int | None = None

    def observe_chunk(channel: str, chunk: bytes, *, final: bool = False) -> None:
        nonlocal stdout_json_buffer
        nonlocal capture_limit_exceeded
        nonlocal event_parse_issue
        nonlocal stdout_count, stderr_count
        nonlocal first_activity_at, last_activity_at, last_activity_monotonic
        nonlocal provider_first_activity_at, permission_pending_at
        if chunk:
            now_text = utc_now()
            if first_activity_at is None:
                first_activity_at = now_text
            last_activity_at = now_text
            last_activity_monotonic = monotonic()
        retained = len(stdout_buffer) + len(stderr_buffer)
        remaining = max(0, spec.capture_limit_bytes - retained)
        retained_chunk = chunk[:remaining]
        if channel == "stdout":
            stdout_count += len(chunk)
            stdout_hash.update(chunk)
            stdout_buffer.extend(retained_chunk)
        else:
            stderr_count += len(chunk)
            stderr_hash.update(chunk)
            stderr_buffer.extend(retained_chunk)
        if stdout_count + stderr_count > spec.capture_limit_bytes:
            capture_limit_exceeded = True
        if channel != "stdout":
            return
        stdout_json_buffer += retained_chunk
        complete_json_bytes = b""
        if final:
            complete_json_bytes = stdout_json_buffer
            stdout_json_buffer = b""
        else:
            last_newline = max(
                stdout_json_buffer.rfind(b"\n"), stdout_json_buffer.rfind(b"\r")
            )
            if last_newline >= 0:
                complete_json_bytes = stdout_json_buffer[: last_newline + 1]
                stdout_json_buffer = stdout_json_buffer[last_newline + 1 :]
        complete_json_text = complete_json_bytes.decode("utf-8", errors="replace")
        events = parse_json_events(complete_json_text)
        candidate_lines = [
            line for line in complete_json_text.splitlines() if line.strip()
        ]
        if len(events) != len(candidate_lines):
            event_parse_issue = True
        for event in events:
            normalized = normalize_event(event, seq=len(normalized_events))
            if normalized is None:
                event_parse_issue = True
                continue
            normalized_events.append(normalized)
            session_id = normalized.get("session_id")
            if isinstance(session_id, str):
                session_ids.add(session_id)
        text_output = json_text_output(events)
        if text_output:
            stdout_text_parts.append(text_output)
        if provider_first_activity_at is None and any(
            event.get("type") == "step_start" for event in events
        ):
            provider_first_activity_at = utc_now()
        pending = permission_pending_after(
            events, pending=permission_pending_at is not None
        )
        if pending:
            if permission_pending_at is None:
                permission_pending_at = utc_now()
        else:
            permission_pending_at = None

    def observe_snapshot(
        next_stdout: str | bytes | None,
        next_stderr: str | bytes | None,
        *,
        previous_lengths: list[int],
        final: bool = False,
    ) -> None:
        observed_stdout = _coerce_bytes(next_stdout)
        observed_stderr = _coerce_bytes(next_stderr)
        stdout_start, stderr_start = previous_lengths
        new_stdout = (
            observed_stdout[stdout_start:]
            if len(observed_stdout) >= stdout_start
            else observed_stdout
        )
        new_stderr = (
            observed_stderr[stderr_start:]
            if len(observed_stderr) >= stderr_start
            else observed_stderr
        )
        previous_lengths[:] = [len(observed_stdout), len(observed_stderr)]
        observe_chunk("stdout", new_stdout, final=final)
        observe_chunk("stderr", new_stderr)

    def deadline_reason(now: float) -> TurnTerminationReason | None:
        if hard_deadline - now <= 0:
            return "role_timeout"
        if last_activity_monotonic + spec.no_progress_timeout_seconds - now <= 0:
            if permission_pending_at is not None:
                return "permission_pending_timeout"
            if provider_first_activity_at is not None:
                return "provider_idle_timeout"
            return "no_progress_timeout"
        return None

    def poll_timeout(now: float) -> float:
        return min(
            spec.poll_seconds,
            hard_deadline - now,
            last_activity_monotonic + spec.no_progress_timeout_seconds - now,
        )

    try:
        stdout_pipe = getattr(proc, "stdout", None)
        stderr_pipe = getattr(proc, "stderr", None)
        stdin_pipe = getattr(proc, "stdin", None)
        use_bounded_pipes = all(
            pipe is not None and callable(getattr(pipe, "fileno", None))
            for pipe in (stdout_pipe, stderr_pipe, stdin_pipe)
        )
        if use_bounded_pipes:
            assert stdin_pipe is not None
            assert stdout_pipe is not None
            assert stderr_pipe is not None
            prompt_bytes = spec.prompt.encode()
            prompt_offset = 0
            for pipe in (stdin_pipe, stdout_pipe, stderr_pipe):
                os.set_blocking(pipe.fileno(), False)
            with selectors.DefaultSelector() as selector:
                selector.register(stdin_pipe, selectors.EVENT_WRITE, "stdin")
                selector.register(stdout_pipe, selectors.EVENT_READ, "stdout")
                selector.register(stderr_pipe, selectors.EVENT_READ, "stderr")
                while True:
                    now = monotonic()
                    termination_reason = deadline_reason(now)
                    if termination_reason is not None:
                        break
                    if not selector.get_map():
                        try:
                            exit_code = proc.wait(timeout=poll_timeout(now))
                        except subprocess.TimeoutExpired:
                            continue
                        break
                    for key, _ in selector.select(timeout=poll_timeout(now)):
                        if key.data == "stdin":
                            try:
                                written = os.write(
                                    key.fd,
                                    prompt_bytes[prompt_offset : prompt_offset + 65536],
                                )
                            except BrokenPipeError:
                                written = 0
                                prompt_offset = len(prompt_bytes)
                            else:
                                prompt_offset += written
                            if prompt_offset >= len(prompt_bytes):
                                selector.unregister(key.fileobj)
                                stdin_pipe.close()
                            continue
                        retained = len(stdout_buffer) + len(stderr_buffer)
                        read_size = min(
                            65536,
                            max(1, spec.capture_limit_bytes - retained + 1),
                        )
                        chunk = os.read(key.fd, read_size)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        observe_chunk(str(key.data), chunk)
                        if capture_limit_exceeded:
                            termination_reason = "output_limit_exceeded"
                            break
                    if termination_reason is not None:
                        break
                observe_chunk("stdout", b"", final=True)
            if termination_reason is None:
                if proc.poll() is None:
                    try:
                        exit_code = proc.wait(timeout=poll_timeout(monotonic()))
                    except subprocess.TimeoutExpired:
                        termination_reason = deadline_reason(monotonic())
                        if termination_reason is None:
                            termination_reason = "no_progress_timeout"
                        process_group_terminated = kill_process_group(proc)
                        exit_code = None
            else:
                process_group_terminated = kill_process_group(proc)
                exit_code = None
        else:
            # Contract tests use scripted Popen doubles without real pipe file
            # descriptors. Production processes always use the selector path.
            previous_lengths = [0, 0]
            first_communicate = True
            while True:
                now = monotonic()
                termination_reason = deadline_reason(now)
                if termination_reason is not None:
                    break
                input_text = spec.prompt if first_communicate else None
                first_communicate = False
                try:
                    completed_stdout, completed_stderr = proc.communicate(
                        input=input_text,
                        timeout=poll_timeout(now),
                    )
                except subprocess.TimeoutExpired as exc:
                    observe_snapshot(
                        exc.output,
                        exc.stderr,
                        previous_lengths=previous_lengths,
                    )
                    if capture_limit_exceeded:
                        termination_reason = "output_limit_exceeded"
                        break
                    continue
                observe_snapshot(
                    completed_stdout,
                    completed_stderr,
                    previous_lengths=previous_lengths,
                    final=True,
                )
                if capture_limit_exceeded:
                    termination_reason = "output_limit_exceeded"
                break
            if termination_reason is not None:
                process_group_terminated = kill_process_group(proc)
                exit_code = None
            else:
                exit_code = proc.returncode
    except BaseException:
        # Cancellation must tear down the process before the controller can
        # remove the disposable worktree underneath it.
        kill_process_group(proc)
        raise

    if len(session_ids) > 1:
        event_parse_issue = True
    session_id = next(iter(session_ids)) if len(session_ids) == 1 else None
    event_parse_status: EventParseStatus
    if normalized_events:
        event_parse_status = (
            "partial"
            if event_parse_issue or termination_reason is not None
            else "complete"
        )
    elif stdout_count > 0:
        event_parse_status = "partial"
    else:
        event_parse_status = "unavailable"

    return OpenCodeProcessOutcome(
        process_started_at=process_started_at,
        process_exited_at=utc_now(),
        process_pid=proc.pid,
        prompt_sent_at=prompt_sent_at,
        first_activity_at=first_activity_at,
        last_activity_at=last_activity_at,
        provider_first_activity_at=provider_first_activity_at,
        permission_pending_at=permission_pending_at,
        termination_reason=termination_reason,
        process_group_terminated=process_group_terminated,
        stdout_bytes=stdout_count,
        stdout_sha256=stdout_hash.hexdigest(),
        stderr_bytes=stderr_count,
        stderr_sha256=stderr_hash.hexdigest(),
        stdout_text="\n".join(stdout_text_parts),
        exit_code=exit_code,
        duration_seconds=monotonic() - started_monotonic,
        session_id=session_id,
        normalized_events=tuple(normalized_events),
        event_parse_status=event_parse_status,
    )


def _coerce_bytes(value: str | bytes | None) -> bytes:
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    return value.encode()
