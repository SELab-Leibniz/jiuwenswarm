"""Loop artifact layout, schema-versioned JSON writes, and the event log.

Everything a loop persists lives under ``<workspace>/.pact/loops/<loop_id>/``
(PRD FR-8). JSON artifacts embed a ``schema`` field; writes are atomic so a
crash never leaves a truncated artifact behind.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .schemas import EVENT_SCHEMA

_RESERVED_EVENT_KEYS = frozenset({"schema", "seq", "ts", "event"})


class EventLogCorruptionError(RuntimeError):
    """Raised when the events.jsonl file fails full-line integrity validation.

    Reader contract: any consumer of ``events.jsonl`` must treat this as a
    hard stop. Consume the intact prefix up to the first violation and
    surface the corruption to the caller; never skip a bad line and keep
    reading as if the log were clean.
    """


def atomic_write_text(path: Path, content: str) -> None:
    """Write text via a same-directory temp file and atomic rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", errors="surrogateescape", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    """Write formatted JSON atomically; payload must carry a ``schema`` field."""
    if "schema" not in payload:
        raise ValueError(f"JSON artifact payload is missing a schema field: {path.name}")
    atomic_write_text(path, json.dumps(payload, indent=2, ensure_ascii=True, default=str) + "\n")


def read_json(path: Path) -> dict[str, object]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"JSON artifact is not an object: {path}")
    return data


def _validate_event_log_lines(events_path: Path) -> int:
    """Validate every line of an existing events.jsonl; return the line count.

    Checks the full file, not just the tail: a middle line with a corrupted
    ``seq`` (e.g. 0, 99, 2) would pass a tail-only check but must still be
    caught here. Any violation raises ``EventLogCorruptionError``.
    """
    try:
        text = events_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise EventLogCorruptionError(
            f"events log is not valid UTF-8: {events_path}"
        ) from exc
    if text == "":
        return 0
    if not text.endswith("\n"):
        raise EventLogCorruptionError(
            f"events log does not end with a newline (truncated write?): {events_path}"
        )
    lines = text.splitlines()
    for index, line in enumerate(lines):
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise EventLogCorruptionError(
                f"events log line {index} is not valid JSON: {events_path}"
            ) from exc
        if not isinstance(record, dict):
            raise EventLogCorruptionError(
                f"events log line {index} is not a JSON object: {events_path}"
            )
        if record.get("schema") != EVENT_SCHEMA:
            raise EventLogCorruptionError(
                f"events log line {index} has an unexpected schema: {events_path}"
            )
        seq = record.get("seq")
        if isinstance(seq, bool) or not isinstance(seq, int):
            raise EventLogCorruptionError(
                f"events log line {index} has a non-integer seq: {events_path}"
            )
        if seq != index:
            raise EventLogCorruptionError(
                f"events log line {index} has seq {seq}, expected {index} (gap or reorder): "
                f"{events_path}"
            )
    return len(lines)


def append_event(events_path: Path, event_type: str, data: dict[str, object]) -> None:
    """Append one schema-tagged, sequenced event line to events.jsonl.

    Rejects any ``data`` key that collides with a control field (``schema``,
    ``seq``, ``ts``, ``event``). Before appending, validates every existing
    line in the file (not just the tail) and raises
    ``EventLogCorruptionError`` on the first violation, fail-closed. See
    ``EventLogCorruptionError`` for the reader contract this establishes for
    future consumers.
    """
    reserved_used = _RESERVED_EVENT_KEYS.intersection(data)
    if reserved_used:
        raise ValueError(
            f"event data may not override reserved key(s): {sorted(reserved_used)}"
        )

    events_path.parent.mkdir(parents=True, exist_ok=True)
    seq = _validate_event_log_lines(events_path) if events_path.exists() else 0

    record = {
        "schema": EVENT_SCHEMA,
        "seq": seq,
        "ts": datetime.now(UTC).isoformat(),
        "event": event_type,
        **data,
    }
    with events_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=True, default=str) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def build_loop_id() -> str:
    """Create a compact, sortable-ish loop id."""
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{uuid.uuid4().hex[:8]}"


@dataclass(frozen=True, slots=True)
class LoopPaths:
    """Filesystem paths for loop-level PACT artifacts."""

    workspace: Path
    loop_dir: Path
    plan: Path
    goal_tracker: Path
    initialization: Path
    state: Path
    manifest: Path
    events: Path
    final_patch: Path
    final_validation: Path
    final_validation_log: Path
    promotion: Path


@dataclass(frozen=True, slots=True)
class RoundPaths:
    """Filesystem paths for artifacts owned by one PACT round."""

    worker_prompt: Path
    worker_result: Path
    worker_adapter_artifacts: Path
    summary: Path
    checkpoint: Path
    delta_patch: Path
    patch: Path
    verification: Path
    verification_log: Path
    reviewer_prompt: Path
    reviewer_result: Path
    reviewer_adapter_artifacts: Path
    reviewer_repair_prompt: Path
    reviewer_repair_result: Path
    reviewer_repair_adapter_artifacts: Path
    review: Path
    review_decision: Path
    reviewer_mutation: Path
    gate: Path


def build_loop_paths(workspace: Path, loop_id: str) -> LoopPaths:
    """Build loop-level artifact paths under the workspace PACT root."""
    loop_dir = workspace / ".pact" / "loops" / loop_id
    return LoopPaths(
        workspace=workspace,
        loop_dir=loop_dir,
        plan=loop_dir / "plan.md",
        goal_tracker=loop_dir / "goal-tracker.md",
        initialization=loop_dir / "initialization.json",
        state=loop_dir / "state.json",
        manifest=loop_dir / "manifest.json",
        events=loop_dir / "events.jsonl",
        final_patch=loop_dir / "final.patch",
        final_validation=loop_dir / "final-validation.json",
        final_validation_log=loop_dir / "final-validation.log",
        promotion=loop_dir / "promotion.json",
    )


def round_paths(loop_dir: Path, round_number: int) -> RoundPaths:
    """Build round-level artifact paths using two-digit round numbers."""
    prefix = f"round-{round_number:02d}"
    return RoundPaths(
        worker_prompt=loop_dir / f"{prefix}-worker-prompt.md",
        worker_result=loop_dir / f"{prefix}-worker-result.json",
        worker_adapter_artifacts=loop_dir / f"{prefix}-worker-adapter",
        summary=loop_dir / f"{prefix}-summary.md",
        checkpoint=loop_dir / f"{prefix}-checkpoint.json",
        delta_patch=loop_dir / f"{prefix}-delta.patch",
        patch=loop_dir / f"{prefix}.patch",
        verification=loop_dir / f"{prefix}-verification.json",
        verification_log=loop_dir / f"{prefix}-verification.log",
        reviewer_prompt=loop_dir / f"{prefix}-reviewer-prompt.md",
        reviewer_result=loop_dir / f"{prefix}-reviewer-result.json",
        reviewer_adapter_artifacts=loop_dir / f"{prefix}-reviewer-adapter",
        reviewer_repair_prompt=loop_dir / f"{prefix}-reviewer-repair-prompt.md",
        reviewer_repair_result=loop_dir / f"{prefix}-reviewer-repair-result.json",
        reviewer_repair_adapter_artifacts=loop_dir / f"{prefix}-reviewer-repair-adapter",
        review=loop_dir / f"{prefix}-review.md",
        review_decision=loop_dir / f"{prefix}-review-decision.json",
        reviewer_mutation=loop_dir / f"{prefix}-reviewer-mutation.patch",
        gate=loop_dir / f"{prefix}-gate.json",
    )
