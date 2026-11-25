"""OpenCode output recovery and normalization to the adapter contract."""

from __future__ import annotations

from pathlib import Path

from ._opencode_process import OpenCodeProcessOutcome
from ..schemas import (
    TurnResult,
    TurnStatus,
    TurnTelemetry,
    TurnTerminationReason,
)


_TAIL_LIMIT = 4000


def recover_and_clear_output_file(path: Path) -> tuple[str, bool]:
    """Read the adapter transport file once and clear it for the next turn."""
    if not path.exists():
        return "", False
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        text = ""
    finally:
        path.unlink(missing_ok=True)
    return text, True


def normalize_process_outcome(
    outcome: OpenCodeProcessOutcome,
    *,
    output_path: Path,
    no_progress_timeout_seconds: float,
    native_store_path: Path | None,
    events_path: Path | None,
) -> TurnResult:
    """Apply file-first recovery and existing failure precedence rules."""
    file_text, file_existed = recover_and_clear_output_file(output_path)

    if outcome.termination_reason is not None:
        return _result(
            outcome,
            status="timeout",
            final_text="",
            output_source="none",
            exit_code=None,
            termination_reason=outcome.termination_reason,
            no_progress_timeout_seconds=no_progress_timeout_seconds,
            native_store_path=native_store_path,
            events_path=events_path,
        )

    if outcome.exit_code != 0:
        # Non-zero exit remains authoritative even when a partial transport
        # file exists; the recovered text is not credible completion evidence.
        return _result(
            outcome,
            status="output_missing",
            final_text="",
            output_source="none",
            exit_code=outcome.exit_code,
            termination_reason="process_exit_nonzero",
            no_progress_timeout_seconds=no_progress_timeout_seconds,
            native_store_path=native_store_path,
            events_path=events_path,
        )

    if file_existed and file_text.strip():
        return _result(
            outcome,
            status="completed",
            final_text=file_text,
            output_source="file",
            exit_code=outcome.exit_code,
            termination_reason="completed",
            no_progress_timeout_seconds=no_progress_timeout_seconds,
            native_store_path=native_store_path,
            events_path=events_path,
        )

    if outcome.stdout_text.strip():
        return _result(
            outcome,
            status="completed",
            final_text=outcome.stdout_text,
            output_source="stdout_json",
            exit_code=outcome.exit_code,
            termination_reason="completed",
            no_progress_timeout_seconds=no_progress_timeout_seconds,
            native_store_path=native_store_path,
            events_path=events_path,
        )

    return _result(
        outcome,
        status="output_missing",
        final_text="",
        output_source="none",
        exit_code=outcome.exit_code,
        termination_reason="output_missing",
        no_progress_timeout_seconds=no_progress_timeout_seconds,
        native_store_path=native_store_path,
        events_path=events_path,
    )


def _result(
    outcome: OpenCodeProcessOutcome,
    *,
    status: TurnStatus,
    final_text: str,
    output_source: str,
    exit_code: int | None,
    termination_reason: TurnTerminationReason,
    no_progress_timeout_seconds: float,
    native_store_path: Path | None,
    events_path: Path | None,
) -> TurnResult:
    return TurnResult(
        status=status,
        final_text=final_text,
        output_source=output_source,
        exit_code=exit_code,
        stdout_tail=_tail(outcome.stdout_text),
        stderr_tail="",
        duration_seconds=outcome.duration_seconds,
        events_path=events_path,
        telemetry=_telemetry(
            outcome,
            termination_reason=termination_reason,
            no_progress_timeout_seconds=no_progress_timeout_seconds,
        ),
        session_id=outcome.session_id,
        native_store_path=native_store_path,
        event_parse_status=outcome.event_parse_status,
        event_count=len(outcome.normalized_events),
    )


def _telemetry(
    outcome: OpenCodeProcessOutcome,
    *,
    termination_reason: TurnTerminationReason,
    no_progress_timeout_seconds: float,
) -> TurnTelemetry:
    return TurnTelemetry(
        process_started_at=outcome.process_started_at,
        process_exited_at=outcome.process_exited_at,
        process_pid=outcome.process_pid,
        prompt_sent_at=outcome.prompt_sent_at,
        first_activity_at=outcome.first_activity_at,
        last_activity_at=outcome.last_activity_at,
        provider_first_activity_at=outcome.provider_first_activity_at,
        permission_pending_at=outcome.permission_pending_at,
        termination_reason=termination_reason,
        no_progress_timeout_seconds=no_progress_timeout_seconds,
        process_group_terminated=outcome.process_group_terminated,
        stdout_bytes=outcome.stdout_bytes,
        stdout_sha256=outcome.stdout_sha256,
        stderr_bytes=outcome.stderr_bytes,
        stderr_sha256=outcome.stderr_sha256,
    )


def _tail(text: str, limit: int = _TAIL_LIMIT) -> str:
    return text[-limit:] if text else ""
