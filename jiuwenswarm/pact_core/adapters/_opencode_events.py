"""Bounded parsing of OpenCode's JSON-lines process stream."""

from __future__ import annotations

import json
from pathlib import Path

from ..artifacts import atomic_write_text
from ..schemas import AGENT_EVENT_SCHEMA


def json_event_lines(text: str) -> list[dict[str, object]]:
    """Return only complete JSON object lines; ignore transport noise."""
    events: list[dict[str, object]] = []
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def json_text_output(events: list[dict[str, object]]) -> str:
    """Extract final-text candidates without retaining arbitrary tool payloads."""
    texts: list[str] = []
    for event in events:
        if event.get("type") != "text":
            continue
        part = event.get("part")
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            text = part["text"].strip()
            if text:
                texts.append(text)
    return "\n".join(texts)


def permission_pending_after(
    events: list[dict[str, object]], *, pending: bool
) -> bool:
    """Fold permission events into the current pending/not-pending state."""
    for event in events:
        if event.get("type") == "permission.asked":
            pending = True
        elif event.get("type") == "permission.replied":
            pending = False
    return pending


def normalize_event(
    event: dict[str, object], *, seq: int
) -> dict[str, object] | None:
    """Project one raw event onto the approved correlation-only metadata."""
    event_type = _string(event.get("type"))
    if event_type is None:
        return None
    part = _mapping(event.get("part"))
    state = _mapping(part.get("state"))
    tool_name = _string(part.get("tool")) or _string(event.get("tool"))
    tool_call_id = (
        _string(part.get("callID"))
        or _string(event.get("callID"))
        or _string(event.get("call_id"))
    )
    session_id = (
        _string(event.get("sessionID"))
        or _string(part.get("sessionID"))
        or _string(event.get("session_id"))
    )
    timestamp = event.get("timestamp")
    if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float, str)):
        timestamp = None
    status = _string(state.get("status"))
    skill_name: str | None = None
    if tool_name == "skill":
        tool_input = _mapping(state.get("input"))
        skill_name = _string(tool_input.get("name"))

    return {
        "schema": AGENT_EVENT_SCHEMA,
        "seq": seq,
        "timestamp": timestamp,
        "type": event_type,
        "session_id": session_id,
        "tool_call_id": tool_call_id,
        "tool_name": tool_name,
        "status": status,
        "skill_name": skill_name,
    }


def write_normalized_events(path: Path, events: list[dict[str, object]]) -> None:
    """Atomically persist one JSON object per line, including the empty case."""
    content = "".join(
        json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
        for event in events
    )
    atomic_write_text(path, content)


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
