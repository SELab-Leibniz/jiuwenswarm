# Copyright (c) 2026 Chrys. All rights reserved.

"""Recognize a recorded SSE ReadError without replaying a call or its tools."""

from __future__ import annotations

import re
import time
from pathlib import Path

from pact_core.artifacts import write_json_atomic
from pact_core.schemas import TurnResult


def partial_stream_failure(exc: Exception, request):
    # Do not classify arbitrary connection, auth, limit or model errors.
    detail = str(exc)
    if not re.search(r"\[181001\].*async stream error:\s*(?:httpx\.)?ReadError\b", detail, re.S):
        return None
    if request.artifact_dir is not None:
        write_json_atomic(
            Path(request.artifact_dir) / "transport-failure.json",
            {
                "schema": "long-horizon/transport-failure/v1",
                "at": time.time(),
                "kind": "sse_read_error",
                "exception_type": type(exc).__name__,
                "cause_types": _cause_types(exc),
                "http_status": None,
                "provider_request_id": None,
                "replayed": False,
                "retries": 0,
                "deadline": request.deadline,
                "next_action": "Checkpoint partial draft, publicly check it; only a normally scheduled later stage may continue.",
            },
        )
    return TurnResult(
        status="error",
        final_text="",
        output_source="transport_failure",
        exit_code=1,
        stderr_tail="sse_read_error; partial draft retained; no call or tool replay",
    )


def _cause_types(exc):
    values, seen = [], set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        values.append(type(exc).__name__)
        exc = exc.__cause__ or exc.__context__
    return values


def record_native_stream_failure(output_dir, *, stage, deadline):
    """Observe the real native terminal receipt when its adapter hides exceptions."""
    import hashlib
    import json

    root = Path(output_dir)
    terminal = root / "terminal-result.json"
    try:
        raw = terminal.read_bytes()
        value = json.loads(raw)
    except (FileNotFoundError, json.JSONDecodeError):
        return False
    error = value.get("error") or {}
    if not re.search(r"\[181001\].*async stream error:\s*(?:httpx\.)?ReadError\b", str(error.get("message", "")), re.S):
        return False
    write_json_atomic(
        root / "transport-failure.json",
        {
            "schema": "long-horizon/transport-failure/v1",
            "at": time.time(),
            "kind": "sse_read_error",
            "origin": "native_terminal_result",
            "stage": stage,
            "native_status": value.get("status"),
            "native_error_code": error.get("code"),
            "local_request_id": value.get("request_id"),
            "local_session_id": value.get("session_id"),
            "terminal_sha256": hashlib.sha256(raw).hexdigest(),
            "http_status": None,
            "provider_request_id": None,
            "socket_errno": None,
            "replayed": False,
            "retries": 0,
            "deadline": deadline,
            "next_action": "Keep native failure; no call/tool replay. Publicly validate any saved partial candidate.",
        },
    )
    return True
