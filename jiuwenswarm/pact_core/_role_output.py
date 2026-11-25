"""Lossless, whole-output JSON decoding for untrusted role proposals only."""

from __future__ import annotations

import json
import re


class RoleOutputError(ValueError):
    """A response has no unambiguous complete JSON object."""


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise RoleOutputError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _constant(value: str) -> object:
    raise RoleOutputError(f"non-JSON numeric constant: {value}")


def decode_role_object(raw: str) -> dict[str, object]:
    """Accept an object or one complete JSON fence; never extract or repair JSON."""
    text = raw.strip().removeprefix("\ufeff").strip()
    fenced = re.fullmatch(r"```(?:json)?[ \t]*\r?\n(.*?)\r?\n```", text,
                          flags=re.IGNORECASE | re.DOTALL)
    if fenced is not None:
        text = fenced.group(1).strip()
    try:
        payload = json.loads(text, object_pairs_hook=_object, parse_constant=_constant)
    except json.JSONDecodeError as exc:
        raise RoleOutputError(f"invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}") from exc
    except RoleOutputError:
        raise
    except (ValueError, RecursionError) as exc:
        raise RoleOutputError("JSON exceeds the decoder's numeric or nesting limits") from exc
    if not isinstance(payload, dict):
        raise RoleOutputError("decision must be a JSON object")
    pending: list[tuple[object, int]] = [(payload, 0)]
    while pending:
        value, depth = pending.pop()
        if depth > 64:
            raise RoleOutputError("JSON exceeds the decoder's nesting limits (64)")
        if isinstance(value, dict):
            pending.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, list):
            pending.extend((item, depth + 1) for item in value)
    try:
        json.dumps(payload, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError as exc:
        raise RoleOutputError("JSON strings must be representable as UTF-8") from exc
    return payload
