"""Opaque final validation and terminal promotion/export finalization."""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime

from ._execution_operations import ExecutionIO
from ._execution_round import remaining_timeout
from .artifacts import LoopPaths
from .checkpoint import CheckpointStore
from .goal_tracker import sha256_text
from .schemas import (
    FINAL_VALIDATION_SCHEMA,
    PROMOTION_SCHEMA,
    FinalValidationStatus,
    FinalValidationResult,
    InitializationCheckpoint,
    LoopStatus,
    OperationName,
    PromotionStatus,
)
from .verification import run_final_validation


def run_final_stage(
    io: ExecutionIO,
    *,
    store: CheckpointStore,
    paths: LoopPaths,
    initialization: InitializationCheckpoint,
    checkpoint: str,
    command: str | None,
    timeout_seconds: float,
    deadline: float | None = None,
) -> FinalValidationStatus:
    """Run and persist terminal validation without exposing it to a role."""
    store.export_patch(checkpoint, paths.final_patch)
    if remaining_timeout(timeout_seconds, deadline) == 0:
        result = FinalValidationResult(status="timeout",
            command_sha256=sha256_text(command) if command else "",
            stderr_tail="budget_exhausted: deadline reached before final validation")
    elif command is None:
        result = run_final_validation(
            command=None,
            workdir=initialization.worker_worktree,
            timeout_seconds=remaining_timeout(timeout_seconds, deadline),
            deadline=deadline,
            loop_dir=paths.loop_dir,
            baseline_commit=initialization.baseline_commit,
            candidate_commit=checkpoint,
            patch_path=paths.final_patch,
        )
    else:
        with store.final_validation_worktree(checkpoint=checkpoint) as final_dir:
            timeout = remaining_timeout(timeout_seconds, deadline)
            result = FinalValidationResult(status="timeout", command_sha256=sha256_text(command),
                stderr_tail="budget_exhausted: deadline reached before final validation") if timeout == 0 else run_final_validation(
                command=command,
                workdir=final_dir,
                timeout_seconds=timeout,
                deadline=deadline,
                loop_dir=paths.loop_dir,
                baseline_commit=initialization.baseline_commit,
                candidate_commit=checkpoint,
                patch_path=paths.final_patch,
            )
    io.write_json(
        paths.final_validation,
        {
            "schema": FINAL_VALIDATION_SCHEMA,
            "baseline_commit": initialization.baseline_commit,
            "candidate_commit": checkpoint,
            "patch_sha256": sha256_text(paths.final_patch.read_bytes().decode("utf-8", errors="surrogateescape")),
            **asdict(result),
        },
    )
    io.write_text(
        paths.final_validation_log,
        f"--- stdout (tail) ---\n{result.stdout_tail}\n"
        f"--- stderr (tail) ---\n{result.stderr_tail}\n",
    )
    io.append_event(
        paths.events,
        "final_validation",
        {"status": result.status, "candidate": checkpoint},
    )
    return result.status


def finalize(
    io: ExecutionIO,
    store: CheckpointStore,
    paths: LoopPaths,
    state: dict[str, object],
    *,
    status: LoopStatus,
    stop_reason: str,
    rounds: int,
    checkpoint: str,
    promote: bool,
    promotion_deferred: bool = False,
) -> tuple[PromotionStatus, str]:
    """Persist terminal evidence, promote/export, and always clean worktrees."""
    promotion_status: PromotionStatus = "not_attempted"
    promotion_reason = ""
    first_exc: BaseException | None = None
    operation_status: str | None = None
    started_at: datetime | None = None

    try:
        if promote:
            operation: OperationName = "promotion"
            started_at = datetime.now(UTC)
            state.update(
                {
                    "operation": operation,
                    "operation_role": None,
                    "operation_status": "running",
                    "operation_started_at": started_at.isoformat(),
                    "operation_finished_at": None,
                    "updated_at": started_at.isoformat(),
                }
            )
            try:
                io.write_json(paths.state, state)
                io.append_event(
                    paths.events,
                    "operation_started",
                    {
                        "operation": operation,
                        "role": None,
                        "round": state.get("current_round"),
                    },
                )
            except BaseException as exc:
                state.update(
                    {
                        "operation_status": "failed",
                        "operation_finished_at": datetime.now(UTC).isoformat(),
                    }
                )
                try:
                    io.write_json(paths.state, state)
                except Exception as telemetry_exc:
                    exc.add_note(f"operation telemetry write failed: {telemetry_exc!r}")
                raise

            side_effect_exc: BaseException | None = None
            try:
                promotion = store.promote(checkpoint, paths.final_patch)
                promotion_status = promotion.status
                promotion_reason = promotion.reason
            except BaseException as exc:
                side_effect_exc = exc
                promotion_status = "failed"
                promotion_reason = f"{operation} raised: {exc!r}"

            operation_failed = side_effect_exc is not None or promotion_status == "failed"
            operation_status = "failed" if operation_failed else "succeeded"
            first_exc = side_effect_exc
        else:
            try:
                store.export_patch(checkpoint, paths.final_patch)
                promotion_reason = (
                    "promotion deferred; candidate ready for authorization"
                    if promotion_deferred
                    else "loop did not complete; patch exported for manual review"
                )
            except BaseException as exc:
                first_exc = exc
                promotion_status = "failed"
                promotion_reason = f"export_patch raised: {exc!r}"

        try:
            io.write_json(
                paths.promotion,
                {
                    "schema": PROMOTION_SCHEMA,
                    "status": promotion_status,
                    "checkpoint": checkpoint,
                    "patch_path": str(paths.final_patch),
                    "reason": promotion_reason,
                },
            )
        except BaseException as exc:
            first_exc = _first_exception(
                first_exc, exc, f"promotion.json write also failed: {exc!r}"
            )

        finished_at = datetime.now(UTC)
        state.update(
            {
                "status": status,
                "stop_reason": stop_reason,
                "current_round": rounds,
                "current_checkpoint": checkpoint,
                "promotion_status": promotion_status,
                "promotion_reason": promotion_reason,
                "updated_at": finished_at.isoformat(),
            }
        )
        if promote:
            state.update(
                {
                    "operation_status": operation_status,
                    "operation_finished_at": finished_at.isoformat(),
                }
            )
        try:
            io.write_json(paths.state, state)
        except BaseException as exc:
            first_exc = _first_exception(
                first_exc, exc, f"terminal state write also failed: {exc!r}"
            )

        if promote:
            assert started_at is not None
            try:
                io.append_event(
                    paths.events,
                    "operation_finished",
                    {
                        "operation": operation,
                        "role": None,
                        "round": state.get("current_round"),
                        "status": operation_status,
                        "duration_seconds": (finished_at - started_at).total_seconds(),
                    },
                )
            except BaseException as exc:
                first_exc = _first_exception(
                    first_exc, exc, f"operation finish event also failed: {exc!r}"
                )
        try:
            io.append_event(
                paths.events,
                "loop_end",
                {
                    "status": status,
                    "stop_reason": stop_reason,
                    "promotion": promotion_status,
                },
            )
        except BaseException as exc:
            first_exc = _first_exception(
                first_exc, exc, f"loop-end event also failed: {exc!r}"
            )
    finally:
        store.cleanup()

    if first_exc is not None:
        raise first_exc
    return promotion_status, promotion_reason


def finalize_infrastructure_failure(
    io: ExecutionIO,
    store: CheckpointStore,
    paths: LoopPaths,
    state: dict[str, object],
    *,
    exc: BaseException,
    rounds: int,
    checkpoint: str,
) -> tuple[LoopStatus, str]:
    """Normalize one infrastructure exception and preserve failure evidence."""
    status: LoopStatus = "failed"
    stop_reason = f"infra_error: {type(exc).__name__}: {exc}"
    try:
        finalize(
            io,
            store,
            paths,
            state,
            status=status,
            stop_reason=stop_reason,
            rounds=rounds,
            checkpoint=checkpoint,
            promote=False,
        )
    except BaseException as finalize_exc:
        exc.add_note(f"failure finalization also failed: {finalize_exc!r}")
    return status, stop_reason


def _first_exception(
    first_exc: BaseException | None, exc: BaseException, note: str
) -> BaseException:
    if first_exc is None:
        return exc
    first_exc.add_note(note)
    return first_exc
