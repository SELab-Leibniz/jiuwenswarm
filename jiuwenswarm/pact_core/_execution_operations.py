"""Operation lifecycle persistence shared by Execution Kernel stages."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .artifacts import LoopPaths
from .schemas import OperationName


@dataclass(frozen=True, slots=True)
class ExecutionIO:
    """The three artifact writes whose order is part of execution behavior."""

    write_json: Callable[[Path, dict[str, object]], None]
    write_text: Callable[[Path, str], None]
    append_event: Callable[[Path, str, dict[str, object]], None]


def record_operation_exit(
    io: ExecutionIO,
    paths: LoopPaths,
    state: dict[str, object],
    operation: OperationName,
    role: str | None,
    status: str,
    started_at: datetime,
) -> None:
    finished_at = datetime.now(UTC)
    state.update(
        {
            "operation_status": status,
            "operation_finished_at": finished_at.isoformat(),
            "updated_at": finished_at.isoformat(),
        }
    )
    io.write_json(paths.state, state)
    io.append_event(
        paths.events,
        "operation_finished",
        {
            "operation": operation,
            "role": role,
            "round": state.get("current_round"),
            "status": status,
            "duration_seconds": (finished_at - started_at).total_seconds(),
        },
    )


@contextmanager
def operation(
    io: ExecutionIO,
    paths: LoopPaths,
    state: dict[str, object],
    *,
    operation: OperationName,
    role: str | None = None,
) -> Iterator[None]:
    """Persist one side-effect boundary and never mask its first exception."""
    started_at = datetime.now(UTC)
    state.update(
        {
            "operation": operation,
            "operation_role": role,
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
            {"operation": operation, "role": role, "round": state.get("current_round")},
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

    try:
        yield
    except BaseException as exc:
        try:
            record_operation_exit(
                io, paths, state, operation, role, "failed", started_at
            )
        except Exception as telemetry_exc:
            exc.add_note(f"operation telemetry write failed: {telemetry_exc!r}")
        raise
    else:
        record_operation_exit(
            io, paths, state, operation, role, "succeeded", started_at
        )
