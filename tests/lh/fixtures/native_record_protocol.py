# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Incremental envelope checks only; Agent/policy semantics stay in Runtime."""

from __future__ import annotations

import hashlib
import json
import math
import tempfile
from typing import Any

SCHEMA_VERSION = "0.1"

CHUNK_TYPE = "record_chunk"
CHUNK_VERSION = 1
CHUNK_FRAME_BYTES = 512 * 1024
CHUNK_THRESHOLD_BYTES = 8 * 1024 * 1024
_CHUNK_SPOOL_BYTES = 8 * 1024 * 1024
_CHUNK_WORST_BYTES_PER_CHAR = 6


def _chunk_overhead(request_id: str, schema_version: str) -> int:
    probe = {
        "type": CHUNK_TYPE,
        "chunk_version": CHUNK_VERSION,
        "schema_version": schema_version,
        "request_id": request_id,
        "stream_id": "0" * 32,
        "chunk_index": 0,
        "data": "",
        "final": True,
        "sha256": "0" * 64,
        "record_bytes": 0,
    }
    return len(
        json.dumps(probe, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )


def _iter_text_chunk_frames(
    pieces,
    *,
    request_id: str,
    schema_version: str,
    frame_bytes: int = CHUNK_FRAME_BYTES,
    stream_id: str | None = None,
):
    """Yield bounded frames over an iterable of JSON text pieces."""
    import hashlib
    import uuid

    if frame_bytes < 2048:
        raise ValueError("chunk frame limit is too small for protocol framing")
    overhead = _chunk_overhead(request_id, schema_version)
    max_chars = max(1, (frame_bytes - overhead - 64) // _CHUNK_WORST_BYTES_PER_CHAR)
    stream = stream_id or uuid.uuid4().hex
    digest = hashlib.sha256()
    total = 0
    index = 0
    buffer: list[str] = []
    buffered = 0

    def emit(final: bool):
        nonlocal index, buffer, buffered
        data = "".join(buffer)
        frame = {
            "type": CHUNK_TYPE,
            "chunk_version": CHUNK_VERSION,
            "schema_version": schema_version,
            "request_id": request_id,
            "stream_id": stream,
            "chunk_index": index,
            "data": data,
            "final": final,
        }
        if final:
            frame["sha256"] = digest.hexdigest()
            frame["record_bytes"] = total
        line = json.dumps(frame, ensure_ascii=False, separators=(",", ":")) + "\n"
        if len(line.encode("utf-8")) > frame_bytes:
            raise ValueError("chunk frame limit is too small for one fragment")
        index += 1
        buffer = []
        buffered = 0
        return line

    for piece in pieces:
        if not isinstance(piece, str):
            raise TypeError("chunk source pieces must be strings")
        start = 0
        while start < len(piece):
            take = min(max_chars - buffered, len(piece) - start)
            fragment = piece[start:start + take]
            encoded = fragment.encode("utf-8")
            buffer.append(fragment)
            buffered += len(fragment)
            digest.update(encoded)
            total += len(encoded)
            start += take
            if buffered >= max_chars:
                yield emit(False)
    yield emit(True)
MAX_INPUT_BYTES = 100 * 1024 * 1024  # 100 MiB fast-path window


class ProtocolError(RuntimeError):
    """Malformed, incomplete, mismatched, or unsupported CLI output."""


def encode(record: dict[str, Any]) -> bytes:
    """Encode one logical record without a size-only refusal.

    Records that fit the fast-path window stay a single unchanged JSON line.
    Larger records become bounded chunk frames; readers reassemble the exact
    text and verify its digest before strict decoding.
    """
    text = json.dumps(
        record, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    )
    encoded = (text + "\n").encode("utf-8")
    if len(encoded) <= MAX_INPUT_BYTES:
        return encoded
    request_id = str(record.get("request_id", ""))
    schema_version = str(record.get("schema_version", SCHEMA_VERSION))
    return "".join(
        _iter_text_chunk_frames(
            [text], request_id=request_id, schema_version=schema_version
        )
    ).encode("utf-8")


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError("duplicate output key")
        result[key] = value
    return result


def _constant(_: str) -> None:
    raise ProtocolError("non-finite output number")


def _float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ProtocolError("non-finite output number")
    return result


class Records:
    """Bounded state: no event history is retained by the SDK."""

    def __init__(
        self, request_id: str, *, query: bool = False, operation: str | None = None
    ) -> None:
        self.request_id = request_id
        self.query = query
        self.operation = operation
        self.sequence = 0
        self.session_id: str | None = None
        self.result: dict[str, Any] | None = None
        self._chunks: dict[str, dict[str, Any]] = {}

    def accept(self, line: bytes) -> dict[str, Any]:
        if self.result is not None:
            raise ProtocolError("output after terminal result")
        if not line.endswith(b"\n"):
            raise ProtocolError("truncated JSONL record")
        try:
            record = json.loads(
                line.decode("utf-8"),
                object_pairs_hook=_pairs,
                parse_constant=_constant,
                parse_float=_float,
            )
        except (ValueError, RecursionError) as error:
            raise ProtocolError("invalid UTF-8 JSON record") from error
        if not isinstance(record, dict):
            raise ProtocolError("output record must be an object")
        if record.get("type") == CHUNK_TYPE:
            return self._accept_chunk(record)
        return self._accept_record(record)

    def _accept_chunk(self, frame: dict[str, Any]) -> dict[str, Any]:
        """Spool one bounded frame; return the logical record on ``final``."""
        if (
            frame.get("chunk_version") != CHUNK_VERSION
            or frame.get("schema_version") != SCHEMA_VERSION
            or frame.get("request_id") != self.request_id
        ):
            raise ProtocolError("output chunk version/request identity mismatch")
        stream_id = frame.get("stream_id")
        index = frame.get("chunk_index")
        data = frame.get("data")
        final = frame.get("final")
        if not isinstance(stream_id, str) or not stream_id:
            raise ProtocolError("output chunk has no stream identity")
        if type(index) is not int or index < 0:
            raise ProtocolError("output chunk index is invalid")
        if not isinstance(data, str):
            raise ProtocolError("output chunk data is not text")
        if not isinstance(final, bool):
            raise ProtocolError("output chunk final flag is invalid")
        state = self._chunks.get(stream_id)
        if state is None:
            if index != 0:
                raise ProtocolError("output chunk stream does not start at zero")
            state = {
                "next": 0,
                "spool": tempfile.SpooledTemporaryFile(max_size=_CHUNK_SPOOL_BYTES),
                "digest": hashlib.sha256(),
                "bytes": 0,
            }
            self._chunks[stream_id] = state
        if index != state["next"]:
            state["spool"].close()
            self._chunks.pop(stream_id, None)
            raise ProtocolError("non-contiguous output chunk sequence")
        raw = data.encode("utf-8")
        state["spool"].write(raw)
        state["digest"].update(raw)
        state["bytes"] += len(raw)
        state["next"] += 1
        if not final:
            return frame
        expected = frame.get("sha256")
        expected_bytes = frame.get("record_bytes")
        actual = state["digest"].hexdigest()
        if expected != actual or expected_bytes != state["bytes"]:
            state["spool"].close()
            self._chunks.pop(stream_id, None)
            raise ProtocolError("output chunk digest/length mismatch")
        state["spool"].seek(0)
        text = state["spool"].read().decode("utf-8")
        state["spool"].close()
        self._chunks.pop(stream_id, None)
        try:
            record = json.loads(
                text,
                object_pairs_hook=_pairs,
                parse_constant=_constant,
                parse_float=_float,
            )
        except (ValueError, RecursionError) as error:
            raise ProtocolError("invalid UTF-8 JSON record") from error
        if not isinstance(record, dict):
            raise ProtocolError("output record must be an object")
        return self._accept_record(record)

    def _accept_record(self, record: dict[str, Any]) -> dict[str, Any]:
        if (
            record.get("schema_version") != SCHEMA_VERSION
            or record.get("request_id") != self.request_id
        ):
            raise ProtocolError("output version/request identity mismatch")
        sequence = record.get("sequence")
        if type(sequence) is not int or sequence != self.sequence:
            raise ProtocolError("non-contiguous output sequence")
        self.sequence += 1
        session_id = record.get("session_id")
        if session_id is not None:
            if not isinstance(session_id, str) or not session_id.strip():
                raise ProtocolError("invalid Session identity")
            if self.session_id is not None and self.session_id != session_id:
                raise ProtocolError("Session identity changed")
            self.session_id = session_id
        kind = record.get("type")
        if kind == "event" and not self.query:
            if (
                not isinstance(record.get("event_type"), str)
                or not record["event_type"]
            ):
                raise ProtocolError("invalid event type")
            return record
        expected = "query_result" if self.query else "result"
        if kind != expected or session_id != self.session_id:
            raise ProtocolError("invalid terminal record")
        if self.query:
            if session_id is not None or self.sequence != 1:
                raise ProtocolError("query must not emit Session events")
            if record.get("operation") not in (None, self.operation):
                raise ProtocolError("query operation mismatch")
        self._validate_terminal(record)
        self.result = record
        return record

    def _validate_terminal(self, record: dict[str, Any]) -> None:
        status = record.get("status")
        code = record.get("exit_code")
        if type(code) is not int or code not in (0, 1, 2, 124, 130):
            raise ProtocolError("invalid exit code")
        expected = {
            0: "completed",
            1: "failed",
            2: "failed",
            124: "timed_out",
            130: "cancelled",
        }[code]
        if status != expected:
            raise ProtocolError("inconsistent result status/exit code")
        error = record.get("error")
        if code != 0:
            if not isinstance(error, dict) or not isinstance(error.get("code"), str):
                raise ProtocolError("failed result requires structured error")
        elif error is not None:
            raise ProtocolError("successful result contains error")
        elif self.query:
            if record.get("operation") != self.operation or not isinstance(
                record.get("data"), dict
            ):
                raise ProtocolError("successful query requires data and operation")
        elif self.session_id is None:
            raise ProtocolError("successful run requires Session identity")

    def finish(self, exit_code: int) -> dict[str, Any]:
        if self.result is None:
            raise ProtocolError("process exited without a terminal result")
        if self.result["exit_code"] != exit_code:
            raise ProtocolError("terminal result does not match process exit code")
        return self.result
