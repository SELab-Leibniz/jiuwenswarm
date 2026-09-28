"""Native Jiuwen machine-process transport for long-horizon stages.

This module does not implement an agent, replay a stage, or claim provider
inference continuation. Runtime owns tools, context management and sessions.
The caller owns schema validation and bounded correction policy; explicit
continuations retain the first call's deadline and native state directory.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import yaml

from jiuwenswarm.channels.process_cli.protocol.model import (
    OneShotEvent, OneShotRunInput, OneShotRunResult,
)

BASE_COMMIT = "49c9f5b4c66d193e4168bfa9c82c2df025c773f0"
# At this native pin glob/list_files do not consistently enforce FileGuard's
# external path boundary. ReadFile and Grep were verified against that boundary.
READ_ONLY_TOOLS = ("read_file", "grep")
_CREDENTIAL_KEY = re.compile(
    r"(?:^|_)(?:api_key|apikey|secret|secret_key|password|passwd|access_token|"
    r"refresh_token|auth_token|authorization|credential|credentials|private_key)(?:$|_)|(?:^|_)token$", re.I,
)
_ENV_REFERENCE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*(?::-)?\}\Z")
_BUDGET_CODES = frozenset({
    "BUDGET_EXHAUSTED", "OUTPUT_BUDGET_EXHAUSTED", "MAX_OUTPUT_TOKENS",
    "MAX_TOKENS_EXCEEDED", "TOKEN_LIMIT_EXCEEDED", "OUTPUT_TOKEN_LIMIT",
})


def _credential_key(value: str) -> bool:
    return _CREDENTIAL_KEY.search(value.replace("-", "_")) is not None


@dataclass
class WorkerResult:
    final_text: str
    events: list[dict]
    session_id: str
    usage: dict
    status: str
    exit_code: int


@dataclass
class _SessionLease:
    identity: dict
    state_dir: Path
    origin_output_dir: Path
    previous_output_dir: Path
    previous_request_id: str
    previous_artifacts: dict[str, str]
    config_sha256: str
    config_environment_sha256: str
    deadline_monotonic: float
    deadline_unix: float
    budget_seconds: float
    continuation_index: int = 0
    active: bool = False
    usable: bool = True
    immutable_receipts: dict[Path, dict[str, str]] = field(default_factory=dict)


_RECEIPT_FILES = (
    "metadata.json", "request.json", "events.jsonl", "terminal-result.json",
    "final-response.json", "usage.json", "native-config.before.yaml", "native-config.after.yaml",
)


def _object_sha256(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _environment_sha256(config_text: str, environment: dict[str, str]) -> str:
    references = set(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)", config_text))
    return _object_sha256({key: environment.get(key) for key in sorted(references)})


def _state_environment(state: Path) -> dict[str, str]:
    return {
        "JIUWENSWARM_DATA_DIR": str(state), "JIUWENSWARM_CONFIG_DIR": str(state / "config"),
        "JIUWENSWARM_TASKS_DIR": str(state / "tasks"),
        "JIUWENSWARM_TASK_REGISTRY_DIR": str(state / "task-registry"),
        "JIUWENSWARM_STARTUP_DIAGNOSTICS_DIR": str(state / "startup"),
        "AGENTOS_GATEWAY_LOG_DIR": str(state / "logs"),
        "OPENJIUWEN_CONTEXT_DEBUG_DIR": str(state / "context-debug"),
        "PYTHONUNBUFFERED": "1",
    }


class WorkerError(RuntimeError):
    """Safe typed failure; detailed evidence remains in ``output_dir``."""

    def __init__(self, message: str, *, code: str, output_dir: Path,
                 result: WorkerResult | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.output_dir = output_dir
        self.result = result


class WorkerConfigurationError(WorkerError):
    pass


class WorkerProtocolError(WorkerError):
    pass


class WorkerExecutionError(WorkerError):
    pass


class WorkerTimeoutError(WorkerExecutionError):
    pass


class WorkerBudgetExhaustedError(WorkerExecutionError):
    pass


def _decode_object(text: str) -> dict:
    def pairs(items: list[tuple[str, Any]]) -> dict:
        result: dict = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def constant(_value: str) -> None:
        raise ValueError("non-finite JSON number")

    result = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    if not isinstance(result, dict):
        raise ValueError("JSON object required")
    return result


def normalize_tool_events(events: list[dict]) -> list[dict]:
    """Expose only actually observed native tool records, with their provenance.

    Unknown success remains ``None``. The native stream rail emits ``success``
    when it can establish a structured result's outcome. An absent error field
    or a successful *overall* run does not establish successful tool execution.
    """
    normalized: list[dict] = []
    for event in events:
        kind = event.get("event_type")
        if event.get("type") != "event" or kind not in {"chat.tool_call", "chat.tool_result"}:
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        tag = "tool_call" if kind == "chat.tool_call" else "tool_result"
        info = payload.get(tag, payload)
        if not isinstance(info, dict):
            continue
        call_id = info.get("tool_call_id") or info.get("id")
        name = info.get("tool_name") or info.get("name")
        if not isinstance(call_id, str) or not call_id or not isinstance(name, str) or not name:
            continue
        record = {
            "type": tag, "call_id": call_id, "name": name,
            "native_sequence": event.get("sequence"),
            "provenance": {key: event.get(key) for key in
                           ("schema_version", "request_id", "session_id", "event_type", "sequence")},
        }
        if tag == "tool_call":
            args = info.get("arguments", info.get("args", {}))
            if isinstance(args, str):
                try:
                    args = _decode_object(args)
                except (ValueError, RecursionError):
                    continue
            if not isinstance(args, dict):
                continue
            record["arguments"] = args
        else:
            failure: bool | None = None
            # Contradictory success and failure flags are always a failure.
            if info.get("success") is False or info.get("is_error") is True or info.get("isError") is True:
                failure = True
            elif str(info.get("status", "")).lower() in {"error", "failed", "failure", "cancelled", "denied"}:
                failure = True
            elif info.get("error"):
                failure = True
            elif info.get("success") is True or info.get("is_error") is False or info.get("isError") is False:
                failure = False
            elif str(info.get("status", "")).lower() in {"success", "completed"}:
                failure = False
            record.update({
                "result": info.get("raw_output", info.get("result", info.get("content"))),
                "rendered_result": info.get("rendered_result"),
                "is_error": failure,
                "success": None if failure is None else not failure,
            })
        normalized.append(record)
    return normalized


class _Redactor:
    def __init__(self, environment: dict[str, str]) -> None:
        self.secrets = sorted({value for key, value in environment.items()
                               if _credential_key(key) and len(value) >= 4}, key=len, reverse=True)

    def text(self, value: str) -> str:
        for secret in self.secrets:
            value = value.replace(secret, "[REDACTED]")
        value = re.sub(r"(?i)(bearer\s+)[^\s\"'<>]+", r"\1[REDACTED]", value)
        value = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "[REDACTED]", value)
        value = re.sub(
            r'''(?i)((?:api[_-]?key|secret|password|authorization|access_token)\s*["']?\s*[:=]\s*["']?)([^\s,}"']+)''',
            r"\1[REDACTED]", value,
        )
        return value

    def value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.value(item) for item in value]
        if isinstance(value, dict):
            return {key: ("[REDACTED]" if _credential_key(str(key)) and item
                          else self.value(item)) for key, item in value.items()}
        return value


def _json_write(path: Path, value: Any, redactor: _Redactor) -> None:
    path.write_text(json.dumps(redactor.value(value), ensure_ascii=False, indent=2,
                               allow_nan=False) + "\n", encoding="utf-8")


def _credential_free(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if _credential_key(str(key)) and item not in (None, ""):
                if not isinstance(item, str) or _ENV_REFERENCE.fullmatch(item) is None:
                    return False
            if not _credential_free(item):
                return False
    elif isinstance(value, list):
        return all(_credential_free(item) for item in value)
    elif isinstance(value, str):
        # Credentials embedded in URLs are not environment-only configuration.
        if re.search(r"https?://[^/\s:@]+:[^/\s@]+@", value):
            return False
        if re.search(r"[?&](?:api[_-]?key|token|password|secret)=(?!\$\{)[^&\s]+", value, re.I):
            return False
    return True


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_identity(source_commit: str, binary: Path) -> dict:
    root = Path(__file__).resolve().parents[2]
    identity = {
        "base_commit": BASE_COMMIT, "implementation_source_commit": source_commit,
        "adapter_sha256": _sha256(Path(__file__)),
        "native_entry_sha256": _sha256(Path(__file__).with_name("native_entry.py")),
        "native_tool_free_sha256": _sha256(Path(__file__).with_name("native_tool_free.py")),
        "contextgraph_reader_sha256": _sha256(Path(__file__).with_name("contextgraph.py")),
        "contextgraph_mcp_sha256": _sha256(Path(__file__).with_name("contextgraph_mcp.py")),
        "native_code_spec_sha256": _sha256(root / "jiuwenswarm/agents/harness/code/spec.py"),
        "native_code_interface_sha256": _sha256(root / "jiuwenswarm/server/runtime/agent_adapter/interface_code.py"),
        "binary_path": str(binary), "binary_sha256": _sha256(binary),
        "checkout_commit": None, "checkout_dirty": None,
        "declared_source_commit": os.environ.get("JIUWENSWARM_AGENT_COMMIT"),
        "identity_scope": "declared upstream base plus separately recorded fork and executable",
    }
    try:
        commit = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                                capture_output=True, check=True, timeout=5)
        dirty = subprocess.run(["git", "-C", str(root), "status", "--porcelain"],
                               capture_output=True, check=True, timeout=5)
        identity.update(checkout_commit=commit.stdout.decode().strip(), checkout_dirty=bool(dirty.stdout))
    except (OSError, subprocess.SubprocessError):
        pass
    protocol = root / "jiuwenswarm/channels/process_cli/protocol/model.py"
    identity["native_protocol_sha256"] = _sha256(protocol)
    return identity


def _readonly_permissions(config: dict) -> dict:
    """Survive native template merge, which restores omitted tool defaults.

    Code mode at the pinned commit rejects a custom Agent tool allowlist.
    Keep the product Agent intact and enforce restrictions in its native
    permission rail. Explicit deny entries also become native deny_tools.
    """
    shipped = Path(__file__).resolve().parents[1] / "resources/config.yaml"
    builtin = yaml.safe_load(shipped.read_text(encoding="utf-8"))
    names = set((builtin.get("permissions") or {}).get("tools") or {})
    names.update((config.get("permissions") or {}).get("tools") or {})
    names.update({"task_tool", "subagent_spawn", "subagent_wait", "subagent_list",
                  "subagent_send_input", "subagent_close", "subagent_resume", "skill_tool",
                  "tool_search", "tool_call", "enter_plan_mode", "exit_plan_mode", "switch_mode",
                  "glob", "list_files"})
    tools = {name: "deny" for name in sorted(names)}
    tools.update({name: "allow" for name in READ_ONLY_TOOLS})
    return {
        "enabled": True, "schema": "tiered_policy", "permission_mode": "strict",
        "package_builtin_rules": True, "mode": "manual", "defaults": {"*": "deny"},
        "tools": tools, "rules": [], "approval_overrides": [],
        "file_guard": {"enabled": True, "workspace": {"read": "allow", "write": "deny", "exec": "deny"},
                       "defaults": {"read": "deny", "write": "deny", "exec": "deny"}, "paths": []},
    }


def _section(config: dict, name: str) -> dict:
    value = config.get(name)
    if value is None:
        value = {}
        config[name] = value
    if not isinstance(value, dict):
        raise ValueError("invalid native configuration section")
    return value


def _partial_summary(records: list[dict]) -> tuple[str, dict, str]:
    text_parts: list[str] = []
    usage: dict = {}
    has_summary = False
    session = ""
    for record in records:
        if isinstance(record.get("session_id"), str):
            session = record["session_id"]
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        kind = record.get("event_type")
        if kind == "chat.delta":
            for key in ("delta", "content", "text", "message", "answer"):
                if isinstance(payload.get(key), str):
                    text_parts.append(payload[key])
                    break
        elif kind == "chat.usage_summary" and isinstance(payload.get("usage"), dict):
            usage = dict(payload["usage"])
            has_summary = True
        elif kind == "chat.usage_metadata" and not has_summary:
            metadata = payload.get("metadata")
            if isinstance(metadata, dict) and isinstance(metadata.get("usage_metadata"), dict):
                for key, value in metadata["usage_metadata"].items():
                    if (key in {"input_tokens", "output_tokens", "total_tokens", "cache_tokens",
                                "input_cost", "output_cost", "total_cost"}
                            and isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0):
                        usage[key] = usage.get(key, 0) + value
    return "".join(text_parts), usage, session


def select_native_final_response(records: list[dict], terminal: dict) -> tuple[str, dict]:
    """Select a native final message, without searching its text for JSON.

    At the pinned revision, ``RunSummary.output`` concatenates every delta in
    the run, including commentary before tool calls. It is therefore a run
    transcript, not necessarily the final assistant message. Native
    ``chat.final.final_mode`` is the authoritative message-boundary contract
    (``jiuwenswarm.common.chat_final``): patch_segment replaces the current
    tool-separated segment, replace_turn replaces the turn, and append adds
    a final segment. Keep that complete final content verbatim.

    Older/ambiguous streams retain their terminal aggregate unchanged; no
    substring, brace matching, Markdown stripping or JSON-aware selection is
    performed. Empty finals can be synthetic stream-end controls, and later
    assistant/tool activity invalidates selection of an earlier message.
    """
    aggregate = terminal.get("output") or ""
    provenance = {"source": "native_terminal_aggregate", "native_sequence": terminal.get("sequence")}
    if terminal.get("status") != "completed":
        return aggregate, provenance
    finals = [(index, record) for index, record in enumerate(records)
              if record.get("type") == "event" and record.get("event_type") == "chat.final"]
    if not finals:
        return aggregate, provenance
    index, final = finals[-1]
    payload = final.get("payload")
    if not isinstance(payload, dict):
        return aggregate, provenance
    mode = payload.get("final_mode")
    content = payload.get("content")
    if (mode not in {"patch_segment", "replace_turn", "append"}
            or not isinstance(content, str) or not content.strip() or payload.get("output_suppressed")):
        return aggregate, provenance
    for following in records[index + 1:]:
        kind = following.get("event_type")
        after_payload = following.get("payload") or {}
        if kind in {"chat.tool_call", "chat.tool_update", "chat.tool_result", "chat.output_phase", "chat.input_received"}:
            return aggregate, provenance
        if kind in {"chat.delta", "chat.reasoning"} and any(
                isinstance(after_payload.get(key), str) and after_payload[key]
                for key in ("content", "delta", "text", "message", "answer")):
            return aggregate, provenance
    return content, {
        "source": "native_chat_final", "native_sequence": final.get("sequence"),
        "final_mode": mode, "output_phase_id": payload.get("output_phase_id"),
        "execution_id": payload.get("execution_id"),
    }


def _budget_signal(records: list[dict]) -> bool:
    """Recognize explicit machine budget evidence, never words in model prose."""
    for record in records:
        if record.get("type") == "result":
            error = record.get("error")
            if isinstance(error, dict) and error.get("code") in _BUDGET_CODES:
                return True
        elif record.get("event_type") in {"chat.usage_metadata", "chat.usage_summary", "chat.error", "runtime.error"}:
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            sources = [payload]
            for source in sources:
                if source.get("code") in _BUDGET_CODES or source.get("finish_reason") in {"length", "max_tokens"}:
                    return True
                # Native llm_usage is wrapped as payload.metadata.usage_metadata.
                # Walk only protocol metadata keys, never arbitrary tool/model text.
                for key in ("metadata", "usage", "usage_metadata", "error", "details"):
                    if isinstance(source.get(key), dict):
                        sources.append(source[key])
    return False


def _redact_native_state(state: Path, redactor: _Redactor) -> int:
    """Scrub diagnostic logs, never the live native history used for resume."""
    count = 0
    for root, directories, files in os.walk(state, followlinks=False):
        directories[:] = [name for name in directories if not (Path(root) / name).is_symlink()]
        for name in files:
            path = Path(root) / name
            relative = path.relative_to(state)
            log_roots = ("openjiuwen-logs", "logs", "startup", "context-debug")
            is_log = relative.parts[0] in log_roots or relative.parts[:2] == ("agent", ".logs")
            if not is_log:
                continue
            if path.is_symlink() or path.suffix.lower() not in {".json", ".jsonl", ".log", ".txt", ".yaml", ".yml"}:
                continue
            try:
                original = path.read_text(encoding="utf-8")
                scrubbed = redactor.text(original)
                if scrubbed != original:
                    path.write_text(scrubbed, encoding="utf-8")
                    count += 1
            except (OSError, UnicodeError):
                continue
    return count


async def _terminate_group(process: asyncio.subprocess.Process, *, grace: float = 1.0) -> None:
    """Terminate our own group, including children retaining stdout pipes."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        await process.wait()
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=grace)
    except asyncio.TimeoutError:
        pass
    # The leader can exit before a child. Kill the remaining group regardless.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    await process.wait()


class JiuwenWorker:
    def __init__(self, binary: Path, config_template: Path, source_commit: str = BASE_COMMIT,
                 default_timeout_seconds: float = 21600.0) -> None:
        self.binary = Path(binary).expanduser().resolve()
        self.config_template = Path(config_template).expanduser().resolve()
        self.source_commit = source_commit
        self.default_timeout_seconds = default_timeout_seconds
        # Deliberately process-local: no implicit cross-workflow restoration.
        self._sessions: dict[str, _SessionLease] = {}
        self._state_directories: set[Path] = set()

    def _admission_identity(self, *, stage: str, workspace: Path, read_only: bool,
                            schema: dict | None, tool_policy: str, environment: dict[str, str]) -> dict:
        try:
            template = self.config_template.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            template = ""
        return {
            "stage": stage, "workspace": str(workspace), "read_only": read_only, "tool_policy": tool_policy,
            "schema_sha256": _object_sha256(schema), "source": _source_identity(self.source_commit, self.binary),
            "config_template_path": str(self.config_template), "config_template_sha256": _sha256(self.config_template),
            # Values (including provider credentials) never enter a receipt.
            "referenced_environment_sha256": _environment_sha256(template, environment),
        }

    @staticmethod
    def _receipt_hashes(output_dir: Path) -> dict[str, str | None]:
        return {name: _sha256(output_dir / name) for name in _RECEIPT_FILES}

    async def run(self, *, stage: str, prompt: str, workspace: Path, output_dir: Path,
                  read_only: bool = False, schema: dict | None = None,
                  timeout_seconds: float | None = None, session_id: str | None = None,
                  tool_policy: str = "configured") -> WorkerResult:
        """Run or explicitly correct one successful native role session.

        Every result contains only this request's events and usage. Callers
        own bounded correction policy and must aggregate actual request chains.
        A continuation keeps the initial deadline and persisted native state;
        it never recreates a missing session or repeats a failed invocation.
        """
        output_dir = Path(output_dir).resolve()
        workspace = Path(workspace).resolve()
        if tool_policy not in ("configured", "none"):
            raise WorkerConfigurationError("Native tool policy must be configured or none.",
                                           code="INVALID_TOOL_POLICY", output_dir=output_dir)
        timeout = self.default_timeout_seconds if timeout_seconds is None else timeout_seconds
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or timeout <= 0):
            raise WorkerConfigurationError("Stage timeout must be finite and positive.",
                                           code="INVALID_TIMEOUT", output_dir=output_dir)
        admitted_monotonic, admitted_unix = time.monotonic(), time.time()
        environment = os.environ.copy()
        identity = self._admission_identity(stage=stage, workspace=workspace, read_only=read_only,
                                             schema=schema, tool_policy=tool_policy, environment=environment)
        lease = None
        if session_id is not None:
            lease = self._sessions.get(session_id)
            reason = None
            if lease is None:
                reason = "UNKNOWN_SESSION"
            elif lease.active:
                reason = "SESSION_BUSY"
            elif not lease.usable:
                reason = "SESSION_NOT_RESUMABLE"
            elif lease.identity != identity:
                reason = "CONTINUATION_IDENTITY_MISMATCH"
            elif any(self._receipt_hashes(path) != hashes for path, hashes in lease.immutable_receipts.items()):
                reason = "CONTINUATION_RECEIPT_CHANGED"
            elif _sha256(lease.state_dir / "config/config.yaml") != lease.config_sha256:
                reason = "CONTINUATION_CONFIG_CHANGED"
            elif _environment_sha256((lease.state_dir / "config/config.yaml").read_text(encoding="utf-8"),
                                      {**environment, **_state_environment(lease.state_dir)}) != lease.config_environment_sha256:
                reason = "CONTINUATION_ENVIRONMENT_CHANGED"
            if reason:
                raise WorkerConfigurationError("Native session continuation was not admitted.",
                                               code=reason, output_dir=output_dir)
            assert lease is not None
            if admitted_monotonic >= lease.deadline_monotonic:
                lease.usable = False
                raise WorkerTimeoutError("The original native session deadline has expired.",
                                         code="TIMEOUT", output_dir=output_dir)
            lease.active = True
        deadline_monotonic = lease.deadline_monotonic if lease else admitted_monotonic + timeout
        deadline_unix = lease.deadline_unix if lease else admitted_unix + timeout
        call_deadline_monotonic = min(deadline_monotonic, admitted_monotonic + timeout)
        try:
            result = await self._run_call(
                stage=stage, prompt=prompt, workspace=workspace, output_dir=output_dir,
                read_only=read_only, schema=schema, timeout_seconds=timeout, session_id=session_id,
                lease=lease, identity=identity, environment=environment, admitted_unix=admitted_unix,
                admitted_monotonic=admitted_monotonic, deadline_unix=deadline_unix,
                deadline_monotonic=deadline_monotonic, call_deadline_monotonic=call_deadline_monotonic,
            )
            metadata = _decode_object((output_dir / "metadata.json").read_text(encoding="utf-8"))
            receipt_hashes = self._receipt_hashes(output_dir)
            if any(value is None for value in receipt_hashes.values()):
                raise WorkerProtocolError("Native call is missing an immutable receipt artifact.",
                                          code="INCOMPLETE_RECEIPT", output_dir=output_dir, result=result)
            if result.session_id in self._sessions and self._sessions[result.session_id] is not lease:
                result.status = "failed"
                metadata.update(status="failed", failure_code="SESSION_COLLISION")
                _json_write(output_dir / "metadata.json", metadata, _Redactor(environment))
                raise WorkerProtocolError("Native session identity collided with another role.",
                                          code="SESSION_COLLISION", output_dir=output_dir, result=result)
            if lease is None:
                lease = _SessionLease(identity=identity, state_dir=output_dir / "state",
                    origin_output_dir=output_dir, previous_output_dir=output_dir,
                    previous_request_id=metadata["request_id"], previous_artifacts=receipt_hashes,
                    config_sha256=metadata["archived_config_sha256"], deadline_monotonic=deadline_monotonic,
                    config_environment_sha256=metadata["archived_config_environment_sha256"],
                    deadline_unix=deadline_unix, budget_seconds=timeout)
                self._sessions[result.session_id] = lease
            else:
                lease.previous_output_dir = output_dir
                lease.previous_request_id = metadata["request_id"]
                lease.previous_artifacts = receipt_hashes
                lease.config_sha256 = metadata["archived_config_sha256"]
                lease.config_environment_sha256 = metadata["archived_config_environment_sha256"]
                lease.continuation_index += 1
            lease.immutable_receipts[output_dir] = receipt_hashes
            return result
        except BaseException:
            if lease is not None:
                lease.usable = False
            raise
        finally:
            if lease is not None:
                lease.active = False

    async def _run_call(self, *, stage: str, prompt: str, workspace: Path, output_dir: Path,
                        read_only: bool, schema: dict | None, timeout_seconds: float,
                        session_id: str | None, lease: _SessionLease | None, identity: dict,
                        environment: dict[str, str], admitted_unix: float, admitted_monotonic: float,
                        deadline_unix: float, deadline_monotonic: float,
                        call_deadline_monotonic: float) -> WorkerResult:
        timeout = call_deadline_monotonic - time.monotonic()
        if not workspace.is_dir() or output_dir == workspace or workspace in output_dir.parents:
            raise WorkerConfigurationError("Stage artifacts must be outside the existing project directory.",
                                           code="INVALID_WORKSPACE", output_dir=output_dir)
        if any(output_dir == state or state in output_dir.parents for state in self._state_directories):
            raise WorkerConfigurationError("Call artifacts cannot be stored inside mutable native state.",
                                           code="OUTPUT_INSIDE_STATE", output_dir=output_dir)
        # Existing nonempty directories are never reused or silently overwritten.
        output_dir.mkdir(parents=True, exist_ok=True)
        if any(output_dir.iterdir()):
            raise WorkerConfigurationError("Stage output directory must be empty.",
                                           code="OUTPUT_EXISTS", output_dir=output_dir)
        try:
            (output_dir / ".worker-claim").touch(exist_ok=False)
        except FileExistsError:
            raise WorkerConfigurationError("Stage output was claimed concurrently.",
                                           code="OUTPUT_EXISTS", output_dir=output_dir) from None
        redactor = _Redactor(environment)
        request_id = uuid.uuid4().hex
        state = lease.state_dir if lease else output_dir / "state"
        self._state_directories.add(state)
        config_dir = state / "config"
        if lease is None:
            config_dir.mkdir(parents=True)
            state.chmod(0o700)
        metadata = {
            "stage": stage, "source": identity["source"],
            "request_id": request_id, "workspace": str(workspace), "state_dir": str(state),
            "read_only": read_only, "timeout_seconds": timeout, "schema_requested": schema is not None,
            "tool_policy": identity["tool_policy"],
            "status": "preparing", "actual_exit_code": None, "signal": None, "timed_out": False,
            "automatic_stage_retries": 0, "session_continuation": lease is not None,
            "requested_session_id": session_id, "requested_timeout_seconds": timeout_seconds,
            "schema_sha256": identity["schema_sha256"], "continuation_identity_sha256": _object_sha256(identity),
            "referenced_environment_sha256": identity["referenced_environment_sha256"],
            "session_budget_seconds": lease.budget_seconds if lease else timeout_seconds,
            "session_deadline_unix": deadline_unix, "session_deadline_monotonic": deadline_monotonic,
            "call_deadline_monotonic": call_deadline_monotonic,
            "call_admitted_monotonic": admitted_monotonic,
            "session_lineage": {"origin_output_dir": str(lease.origin_output_dir if lease else output_dir),
                "previous_output_dir": str(lease.previous_output_dir) if lease else None,
                "previous_request_id": lease.previous_request_id if lease else None,
                "previous_receipt_sha256": lease.previous_artifacts["metadata.json"] if lease else None,
                "previous_artifact_sha256": dict(lease.previous_artifacts) if lease else {},
                "continuation_index": lease.continuation_index + 1 if lease else 0},
            "started_at_unix": admitted_unix, "usage": {},
            "artifacts_redacted": True, "native_state_is_mutable": True,
            "native_state_redaction": "diagnostic logs only; private live history is not rewritten",
            "config_snapshots": {"before": "native-config.before.yaml", "after": "native-config.after.yaml"},
        }
        _json_write(output_dir / "metadata.json", metadata, redactor)
        config_path = config_dir / "config.yaml"
        try:
            if lease is None:
                if _sha256(self.config_template) != identity["config_template_sha256"]:
                    raise ValueError("template changed during admission")
                config = yaml.safe_load(self.config_template.read_text(encoding="utf-8"))
                if not isinstance(config, dict) or not _credential_free(config):
                    raise ValueError("invalid config")
                _section(_section(config, "modes"), "code")["tool_policy"] = identity["tool_policy"]
                if read_only or identity["tool_policy"] == "none":
                    # Frozen-source investigation and tool-free synthesis do
                    # not mount external tools. Mutable native solver roles
                    # retain the explicitly configured read-only graph MCP.
                    _section(config, "mcp")["servers"] = []
                if read_only:
                    config["permissions"] = _readonly_permissions(config)
                    _section(config, "hooks")["disable_all_hooks"] = True
                # Preserve native model/context-engine behavior. Only relocate
                # artifacts and apply the explicit phase permission policy.
                _section(config, "trajectory_ui")["db_path"] = str(state / ".trace/sessions")
                _section(config, "agent_observability")["traces_dir"] = str(state / ".trace")
                _section(_section(config, "react"), "context_engine_config")["context_debug_dir"] = str(state / "context-debug")
                config_path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")
                config_path.chmod(0o600)
            # A continuation uses native's previous effective config unchanged.
            # The shared state can evolve; these per-request snapshots cannot.
            (output_dir / "native-config.before.yaml").write_bytes(config_path.read_bytes())
        except (OSError, ValueError, TypeError, AttributeError, yaml.YAMLError):
            metadata.update(status="configuration_failed", failure_code="INVALID_CONFIG")
            _json_write(output_dir / "metadata.json", metadata, redactor)
            raise WorkerConfigurationError("Native configuration could not be isolated for this stage.",
                                           code="INVALID_CONFIG", output_dir=output_dir) from None
        metadata["config_template_sha256"] = identity["config_template_sha256"]
        metadata["requested_config_sha256"] = _sha256(output_dir / "native-config.before.yaml")
        environment.update(_state_environment(state))
        metadata["requested_config_environment_sha256"] = _environment_sha256(config_path.read_text(encoding="utf-8"), environment)
        if schema is not None:
            prompt += "\n\nReturn ONLY one JSON value satisfying this JSON Schema, without Markdown fences:\n" + json.dumps(schema, ensure_ascii=False, allow_nan=False)
        timeout_admitted_monotonic, timeout_admitted_unix = time.monotonic(), time.time()
        timeout = call_deadline_monotonic - timeout_admitted_monotonic
        metadata.update(timeout_seconds=timeout, timeout_admitted_monotonic=timeout_admitted_monotonic,
                        timeout_admitted_at_unix=timeout_admitted_unix)
        if timeout <= 0:
            metadata.update(status="timed_out", failure_code="TIMEOUT", timed_out=True)
            _json_write(output_dir / "metadata.json", metadata, redactor)
            raise WorkerTimeoutError("The admitted native call deadline expired during preparation.",
                                     code="TIMEOUT", output_dir=output_dir)
        request: dict = {
            "schema_version": "0.1", "type": "run", "request_id": request_id,
            "input": prompt, "mode": "agent.code.normal", "timeout_seconds": timeout,
            "workspace": {"cwd": str(workspace), "project_dir": str(workspace), "trusted_dirs": [str(workspace)]},
        }
        if session_id is not None:
            request["session_id"] = session_id
        try:
            OneShotRunInput.from_dict(request)
        except (ValueError, TypeError):
            raise WorkerConfigurationError("Request does not satisfy the pinned native schema.",
                                           code="INVALID_REQUEST", output_dir=output_dir) from None
        _json_write(output_dir / "request.json", request, redactor)
        if schema is not None:
            _json_write(output_dir / "response-schema.json", schema, redactor)
        # stdin uses the original request, while persisted artifacts redact secrets.
        request_bytes = json.dumps(request, ensure_ascii=False, allow_nan=False).encode("utf-8")
        metadata.update(status="running", command=[str(self.binary), "--run-json", "-"])
        _json_write(output_dir / "metadata.json", metadata, redactor)
        records: list[dict] = []
        terminal: dict | None = None
        protocol_error: str | None = None
        process: asyncio.subprocess.Process | None = None
        interrupted = False
        budget_observed = False
        budget_stop_task: asyncio.Task | None = None
        policy_stop_task: asyncio.Task | None = None
        policy_proofs: list[int] = []
        native_session: str | None = session_id
        started = time.monotonic()

        async def read_stdout() -> None:
            nonlocal terminal, protocol_error, budget_observed, budget_stop_task, policy_stop_task, native_session
            assert process is not None and process.stdout is not None
            with (output_dir / "events.jsonl").open("w", encoding="utf-8") as event_log, \
                    (output_dir / "stdout.jsonl").open("w", encoding="utf-8") as raw_log:
                try:
                    async for line in process.stdout:
                        raw_log.write(redactor.text(line.decode("utf-8", errors="replace")))
                        raw_log.flush()
                        try:
                            record = _decode_object(line.decode("utf-8"))
                            if terminal is not None:
                                raise ValueError("record after terminal")
                            if record.get("sequence") != len(records) or record.get("request_id") != request_id:
                                raise ValueError("invalid correlation or sequence")
                            if native_session and record.get("session_id") != native_session:
                                raise ValueError("native session identity changed")
                            if record.get("type") == "result":
                                OneShotRunResult.from_dict(record)
                                terminal = record
                            else:
                                OneShotEvent.from_dict(record)
                            if record.get("session_id"):
                                native_session = record["session_id"]
                            records.append(record)
                            event_log.write(json.dumps(redactor.value(record), ensure_ascii=False, allow_nan=False) + "\n")
                            event_log.flush()
                            if identity["tool_policy"] == "none":
                                kind, payload = record.get("event_type"), record.get("payload")
                                if kind == "chat.tool_policy":
                                    if (isinstance(payload, dict) and payload.get("policy") == "none"
                                            and payload.get("registered_tools") == [] and payload.get("verified") is True
                                            and payload.get("profile") == "code.no-tools.v1"):
                                        policy_proofs.append(record["sequence"])
                                    else:
                                        protocol_error = "INVALID_TOOL_POLICY_PROOF"
                                elif kind in {"chat.tool_call", "chat.tool_result"}:
                                    protocol_error = "TOOL_POLICY_VIOLATION"
                                if protocol_error in {"INVALID_TOOL_POLICY_PROOF", "TOOL_POLICY_VIOLATION"} and policy_stop_task is None:
                                    policy_stop_task = asyncio.create_task(_terminate_group(process, grace=0.2))
                            if not budget_observed and _budget_signal([record]):
                                budget_observed = True
                                metadata["budget_stop_observed_at_unix"] = time.time()
                                metadata["budget_stop_native_sequence"] = record["sequence"]
                                # Stop as soon as the adapter observes the native
                                # signal, before treating truncated output as usable.
                                # This cannot undo actions predating its emission.
                                if record.get("type") == "event":
                                    budget_stop_task = asyncio.create_task(_terminate_group(process, grace=0.2))
                        except (UnicodeError, ValueError, TypeError, RecursionError):
                            protocol_error = protocol_error or "INVALID_JSONL"
                except (ValueError, asyncio.LimitOverrunError):
                    protocol_error = "OUTPUT_RECORD_TOO_LARGE"

        async def read_stderr() -> None:
            assert process is not None and process.stderr is not None
            with (output_dir / "stderr.log").open("w", encoding="utf-8") as stream:
                # Read complete lines to avoid secrets split across arbitrary chunks.
                try:
                    async for line in process.stderr:
                        stream.write(redactor.text(line.decode("utf-8", errors="replace")))
                        stream.flush()
                except (ValueError, asyncio.LimitOverrunError):
                    stream.write("[oversized diagnostic record omitted]\n")

        async def communicate() -> None:
            assert process is not None and process.stdin is not None
            process.stdin.write(request_bytes)
            try:
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            process.stdin.close()
            await asyncio.gather(read_stdout(), read_stderr(), process.wait())

        async def finish_cleanup(awaitable):
            """Repeated task cancellation cannot interrupt owned-process cleanup."""
            nonlocal interrupted
            cleanup = asyncio.ensure_future(awaitable)
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    interrupted = True
            return cleanup.result()

        async def drain_after_stop(task: asyncio.Task) -> None:
            try:
                await asyncio.wait_for(task, timeout=5)
            except asyncio.TimeoutError:
                # Escaped native descendants can keep pipes open. Preserve the
                # bounded stop outcome instead of converting it to a retryable error.
                metadata["stream_drain_incomplete"] = True

        try:
            spawning = asyncio.create_task(asyncio.create_subprocess_exec(
                str(self.binary), "--run-json", "-", cwd=state, env=environment,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, start_new_session=True, limit=8 * 1024 * 1024,
            ))
            try:
                process = await asyncio.shield(spawning)
            except asyncio.CancelledError:
                interrupted = True
                process = await finish_cleanup(spawning)
            if interrupted:
                await finish_cleanup(_terminate_group(process))
                await finish_cleanup(drain_after_stop(asyncio.gather(read_stdout(), read_stderr(), process.wait())))
            else:
                task = asyncio.create_task(communicate())
                try:
                    # Cleanup has its own bounded grace, never extra inference time.
                    await asyncio.wait_for(asyncio.shield(task), timeout=max(0, call_deadline_monotonic - time.monotonic()))
                except asyncio.TimeoutError:
                    metadata["timed_out"] = True
                    await finish_cleanup(_terminate_group(process))
                    await finish_cleanup(drain_after_stop(task))
                except asyncio.CancelledError:
                    interrupted = True
                    await finish_cleanup(_terminate_group(process))
                    await finish_cleanup(drain_after_stop(task))
        except OSError:
            metadata.update(status="startup_failed", failure_code="STARTUP_FAILED")
            _json_write(output_dir / "metadata.json", metadata, redactor)
            raise WorkerExecutionError("Native worker could not start.", code="STARTUP_FAILED", output_dir=output_dir) from None
        finally:
            if budget_stop_task is not None:
                await finish_cleanup(budget_stop_task)
            if policy_stop_task is not None:
                await finish_cleanup(policy_stop_task)
            if process is not None:
                # Successful parents must not leave tool subprocesses running.
                await finish_cleanup(_terminate_group(process, grace=0.1))
                actual_exit = process.returncode
                metadata.update(actual_exit_code=actual_exit,
                                signal=-actual_exit if actual_exit is not None and actual_exit < 0 else None)
            metadata["finished_at_monotonic"] = time.monotonic()
            metadata["elapsed_seconds"] = metadata["finished_at_monotonic"] - started
            metadata["finished_at_unix"] = time.time()
            metadata["native_text_artifacts_redacted"] = _redact_native_state(state, redactor)
            # Native startup may merge its shipped defaults and rewrite YAML.
            # Record both submitted and archived configurations, without claiming
            # that an after-run file proves every in-memory setting used at runtime.
            (output_dir / "native-config.after.yaml").write_bytes(config_path.read_bytes())
            metadata["archived_config_sha256"] = _sha256(output_dir / "native-config.after.yaml")
            metadata["archived_config_environment_sha256"] = _environment_sha256(config_path.read_text(encoding="utf-8"), environment)
            metadata["protocol_error"] = protocol_error
            metadata["tool_policy_proof_sequences"] = policy_proofs
            _json_write(output_dir / "metadata.json", metadata, redactor)

        actual_exit = process.returncode if process is not None else 1
        assert actual_exit is not None
        safe_records = redactor.value(records)
        partial_text, partial_usage, partial_session = _partial_summary(safe_records)
        final_text, final_provenance = (
            select_native_final_response(safe_records, redactor.value(terminal))
            if terminal is not None else (partial_text, {"source": "partial_native_stream"})
        )
        if not final_text and partial_text:
            final_text, final_provenance = partial_text, {"source": "partial_native_stream"}
        result = WorkerResult(
            final_text=final_text,
            events=[item for item in safe_records if item.get("type") == "event"],
            session_id=str((terminal or {}).get("session_id") or partial_session),
            usage=redactor.value((terminal or {}).get("usage") or partial_usage),
            status=str((terminal or {}).get("status") or "failed"), exit_code=actual_exit,
        )
        failure: type[WorkerError] | None = None
        code = ""
        if interrupted:
            result.status = "cancelled"
            code = "CANCELLED"
        elif budget_observed:
            result.status = "budget_exhausted"
            failure, code = WorkerBudgetExhaustedError, "BUDGET_EXHAUSTED"
        elif metadata["timed_out"] or result.status == "timed_out":
            result.status = "timed_out"
            failure, code = WorkerTimeoutError, "TIMEOUT"
        elif identity["tool_policy"] == "none" and (policy_stop_task is not None or (
                result.status == "completed" and not policy_proofs)):
            result.status = "failed"
            failure, code = WorkerProtocolError, protocol_error or "MISSING_TOOL_POLICY_PROOF"
        elif actual_exit < 0 or actual_exit in {130, 137, 143} or result.status == "cancelled":
            result.status = "cancelled"
            failure, code = WorkerExecutionError, "INTERRUPTED"
        elif protocol_error or terminal is None or terminal.get("exit_code") != actual_exit:
            result.status = "failed"
            failure, code = WorkerProtocolError, protocol_error or "MISSING_OR_INCONSISTENT_RESULT"
        elif result.status != "completed" or actual_exit != 0:
            failure, code = WorkerExecutionError, "NATIVE_FAILED"
        metadata.update(status=result.status, failure_code=code or None, usage=result.usage,
                        session_id=result.session_id, terminal_result_received=terminal is not None,
                        budget_stop_observed=budget_observed, final_response=final_provenance)
        if terminal is not None:
            _json_write(output_dir / "terminal-result.json", terminal, redactor)
        _json_write(output_dir / "final-response.json", {"text": result.final_text, **final_provenance}, redactor)
        _json_write(output_dir / "usage.json", result.usage, redactor)
        _json_write(output_dir / "metadata.json", metadata, redactor)
        if interrupted:
            raise asyncio.CancelledError
        if failure:
            raise failure("Native stage did not complete; inspect the stage artifacts.",
                          code=code, output_dir=output_dir, result=result)
        return result


__all__ = ["BASE_COMMIT", "JiuwenWorker", "WorkerResult", "WorkerError", "WorkerConfigurationError",
           "WorkerProtocolError", "WorkerExecutionError", "WorkerTimeoutError", "WorkerBudgetExhaustedError",
           "normalize_tool_events", "select_native_final_response", "READ_ONLY_TOOLS"]
