"""Decode one complete structured object without rewriting model output.

Chrys 6fcfb332 kernel/_types.py::_parse_structured_response_value accepts JSON
fences and prose around an object. This migration preserves those transport
forms while rejecting ambiguous candidates, duplicate keys, non-finite values,
and truncation. It never repairs payload bytes or promotes a nested fragment.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any

MAX_STRUCTURED_CHARS = 8 * 1024 * 1024
_FENCE = re.compile(r"(?m)^[ \t]*```(?:json)?[ \t]*\r?\n(?P<body>[\s\S]*?)\r?\n[ \t]*```[ \t]*(?=\r?$)")


@dataclass(frozen=True)
class DecodedObject:
    value: dict[str, Any]
    format: str
    object_text: str
    start: int
    end: int


def _strict_json(text: str) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        output = {}
        for key, value in items:
            if key in output:
                raise ValueError(f"duplicate JSON key: {key}")
            output[key] = value
        return output

    def constant(value: str) -> None:
        raise ValueError(f"non-finite JSON value: {value}")

    try:
        parsed = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except RecursionError as exc:
        raise ValueError("structured JSON exceeds parser nesting limit") from exc
    pending = [parsed]
    while pending:
        value = pending.pop()
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("structured JSON contains a non-finite number")
        if isinstance(value, dict):
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    return parsed


def _object_spans(text: str) -> list[tuple[int, int]]:
    """Find outer objects, refusing incomplete or mismatched containers."""
    spans: list[tuple[int, int]] = []
    start: int | None = None
    stack: list[str] = []
    in_string = False
    escaped = False
    for index, character in enumerate(text):
        if start is None:
            if character == "{":
                start = index
                stack = [character]
            elif character in "[]}":
                # Do not silently unwrap an array or accept a malformed suffix.
                raise ValueError("structured object has an ambiguous outer container")
            continue
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "{[":
            stack.append(character)
        elif character in "}]":
            if not stack or (stack[-1], character) not in {("{", "}"), ("[", "]")}:
                raise ValueError("structured object has mismatched containers")
            stack.pop()
            if not stack:
                spans.append((start, index + 1))
                start = None
    if start is not None:
        raise ValueError("structured object is incomplete")
    return spans


def _decoded(text: str, start: int, end: int, output_format: str) -> DecodedObject:
    raw = text[start:end]
    value = _strict_json(raw)
    if not isinstance(value, dict):
        raise ValueError("structured response must contain a JSON object")
    return DecodedObject(value=value, format=output_format, object_text=raw, start=start, end=end)


def decode_structured_object(text: str) -> DecodedObject:
    """Return a unique complete object and exact source offsets; raise ValueError otherwise."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("structured response is empty")
    if len(text) > MAX_STRUCTURED_CHARS:
        raise ValueError("structured response exceeds the size limit")
    stripped = text.strip()
    start = len(text) - len(text.lstrip())
    end = len(text.rstrip())
    try:
        value = _strict_json(stripped)
    except json.JSONDecodeError:
        pass
    else:
        if not isinstance(value, dict):
            raise ValueError("structured response must contain a JSON object")
        return DecodedObject(value=value, format="json", object_text=stripped, start=start, end=end)

    # A malformed outer JSON value must not expose a syntactically-valid inner
    # object to the prose fallback.
    if stripped.startswith(("[", '"')):
        raise ValueError("structured response has a malformed outer JSON value")

    fences = list(_FENCE.finditer(text))
    if fences:
        if len(fences) != 1:
            raise ValueError("structured response contains multiple fenced candidates")
        fence = fences[0]
        before, after = text[:fence.start()], text[fence.end():]
        if "```" in before or "```" in after:
            raise ValueError("structured response contains ambiguous code fences")
        if _object_spans(before) or _object_spans(after):
            raise ValueError("structured response contains multiple object candidates")
        body = fence.group("body")
        body_start = fence.start("body") + len(body) - len(body.lstrip())
        body_end = fence.start("body") + len(body.rstrip())
        output_format = "json_fence" if not before.strip() and not after.strip() else "prose_json_fence"
        return _decoded(text, body_start, body_end, output_format)
    if "```" in text:
        raise ValueError("structured response has an incomplete or unsupported fence")

    spans = _object_spans(text)
    if len(spans) != 1:
        raise ValueError("structured response must contain exactly one complete object")
    start, end = spans[0]
    return _decoded(text, start, end, "prose_json")


def parse_unique_json_object(text: str) -> dict[str, Any]:
    return decode_structured_object(text).value
