# Copyright (c) 2026 Chrys. All rights reserved.

"""Recognize recorded transport failures without replaying a call or its tools."""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

from pact_core.artifacts import write_json_atomic
from pact_core.schemas import TurnResult

# Only machine diagnostics and structured receipts reach this classifier. It
# deliberately never searches model prose, task text or a candidate tree.
_TRANSPORT_DIAGNOSTIC = re.compile(
    r"(?:"
    r"openAI API async (?:stream|invoke) error:\s*(?:httpx\.)?"
    r"(?:ReadError|APIConnectionError|APITimeoutError|ReadTimeout|ConnectError|RemoteProtocolError)\b|"
    r"LLM stream timeout:\s*stream frame timeout|"
    r"\b(?:ReadError|APIConnectionError|APITimeoutError|ReadTimeout|ConnectError|RemoteProtocolError|"
    r"ServerDisconnectedError|ConnectionResetError|ConnectionRefusedError|ConnectionAbortedError|"
    r"BrokenPipeError)\b|"
    r"connection\s+(?:reset|refused|aborted|closed)|"
    r"broken\s+pipe|\bECONNRESET\b|\bECONNREFUSED\b|\bEPIPE\b|"
    r"server disconnected|remote protocol error|"
    r"HTTP(?:/[\d.]+)?\s*(?:500|502|503|504)\b|"
    r"\b(?:500|502|503|504)\s+(?:internal server error|bad gateway|service unavailable|gateway time-?out)|"
    r"internal server error|bad gateway|service unavailable|gateway time-?out|"
    r"(?:status(?:_code)?|http_status)\s*[=:]\s*5\d\d\b|"
    r"provider[^\n]{0,80}\b5\d\d\b"
    r")",
    re.IGNORECASE,
)
_FAILED_STATUSES = frozenset({"error", "output_missing", "spawn_failed", "timeout"})


def transport_failure_evidence(
    result,
    *,
    artifact_dirs=(),
    native_dirs=(),
    stage: str = "benchmark-worker",
) -> dict | None:
    """Classify a failed role result from its adapter diagnostic or owned receipts."""
    if getattr(result, "status", None) not in _FAILED_STATUSES:
        return None
    diagnostic = str(getattr(result, "stderr_tail", "") or "")[:4000]
    match = _TRANSPORT_DIAGNOSTIC.search(diagnostic)
    if match is not None:
        return {
            "kind": "adapter_diagnostic",
            "status": getattr(result, "status", None),
            "detail": diagnostic[:1000],
            "matched": match.group(0)[:200],
        }
    for directory in artifact_dirs:
        if directory is None:
            continue
        path = Path(directory) / "transport-failure.json"
        try:
            raw = path.read_bytes()
            value = json.loads(raw)
        except (OSError, ValueError):
            continue
        if not isinstance(value, dict) or value.get("schema") != "long-horizon/transport-failure/v1":
            continue
        return {
            "kind": "transport_receipt",
            "status": getattr(result, "status", None),
            "detail": str(value.get("kind") or value.get("origin") or "transport-failure")[:200],
            "path": str(path),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
    for directory in native_dirs:
        if directory is None:
            continue
        evidence = _native_receipt_failure(Path(directory), stage=stage)
        if evidence is not None:
            return {**evidence, "status": getattr(result, "status", None)}
    return None


def _native_receipt_failure(directory: Path, *, stage: str) -> dict | None:
    """Reuse the existing native classifier, then accept the same receipts for 5xx/connection errors."""
    try:
        from .plugins.transport_recovery import stream_failure

        evidence = stream_failure(directory, stage=stage)
    except (OSError, ValueError, TypeError):
        evidence = None
    if evidence is not None:
        return {
            "kind": "native_stream",
            "detail": str(evidence.get("failure", ""))[:1000],
            "failed_session_id": evidence.get("failed_session_id"),
            "terminal_sha256": evidence.get("terminal_sha256"),
            "metadata_sha256": evidence.get("metadata_sha256"),
        }
    try:
        raw_terminal = (directory / "terminal-result.json").read_bytes()
        raw_metadata = (directory / "metadata.json").read_bytes()
        terminal, metadata = json.loads(raw_terminal), json.loads(raw_metadata)
        error = terminal.get("error") or {}
        message = error.get("message") if isinstance(error, dict) else None
        if not (
            isinstance(message, str)
            and _TRANSPORT_DIAGNOSTIC.search(message)
            and terminal.get("status") == "failed"
            and type(terminal.get("exit_code")) is int
            and terminal["exit_code"] == 1
            and error.get("code") == "RUNTIME_ERROR"
            and metadata.get("stage") == stage
            and metadata.get("failure_code") == "NATIVE_FAILED"
            and metadata.get("timed_out") is False
            and metadata.get("signal") is None
            and type(metadata.get("actual_exit_code")) is int
            and metadata["actual_exit_code"] == 1
            and not metadata.get("budget_stop_observed", False)
        ):
            return None
        return {
            "kind": "native_receipt",
            "detail": message[:1000],
            "failed_session_id": terminal.get("session_id"),
            "terminal_sha256": hashlib.sha256(raw_terminal).hexdigest(),
            "metadata_sha256": hashlib.sha256(raw_metadata).hexdigest(),
        }
    except (OSError, ValueError, AttributeError, TypeError):
        return None


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
