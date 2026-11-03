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
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from jiuwenswarm.channels.process_cli.protocol.model import (
    OneShotEvent,
    OneShotRunInput,
    OneShotRunResult,
)

BASE_COMMIT = "49c9f5b4c66d193e4168bfa9c82c2df025c773f0"
# At this native pin glob/list_files do not consistently enforce FileGuard's
# external path boundary. ReadFile and Grep were verified against that boundary.
READ_ONLY_TOOLS = ("read_file", "grep")
PROCESS_KILL_GUIDANCE_VERSION = "pid-scoped-kill/v1"
_PROCESS_KILL_GUIDANCE = (
    "\nProcess termination (pid-scoped): do not stop processes with broad command-line or name matching "
    "such as pkill -f, killall, or pgrep -f ... | xargs kill; these can match this Agent or the framework. "
    "Stop only a process you started and whose PID you recorded, or its process group. For a background "
    "service, write its PID to a file and stop it later using that PID.\n"
)
_CREDENTIAL_KEY = re.compile(
    r"(?:^|_)(?:api_key|apikey|secret|secret_key|password|passwd|access_token|"
    r"refresh_token|auth_token|authorization|credential|credentials|private_key)(?:$|_)|"
    r"(?:^|_)token$|(?:^|_)key_(?:file|path)$", re.I,
)
_ENV_REFERENCE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*(?::-)?\}\Z")
_SECRET_PREFIX = re.compile(
    r"(?:^|[/\s])(?:sk-|gh[opusr]_|github_pat_|xox[baprs]-|AKIA[0-9A-Z]{16})", re.I,
)
_URL_USERINFO = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^/\s?#@]*:[^/\s?#@]*@")
_PATH_USERINFO = re.compile(r"[^/\s?#@:]+:[^/\s?#@:]+@")
_SCHEMELESS_AUTHORITY_AT = re.compile(r"^//[^/?#]*@")
_SECRET_QUERY = re.compile(r"[?&](?:api[_-]?key|token|password|secret)=(?!\$\{)[^&\s]+", re.I)
_TOKEN_ALPHABET = re.compile(r"[A-Za-z0-9+/_=-]+\Z")
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


def _state_environment(state: Path, *, read_only: bool = False,
                       views: tuple[Path, ...] = ()) -> dict[str, str]:
    environment = {
        "JIUWENSWARM_DATA_DIR": str(state), "JIUWENSWARM_CONFIG_DIR": str(state / "config"),
        "JIUWENSWARM_TASKS_DIR": str(state / "tasks"),
        "JIUWENSWARM_TASK_REGISTRY_DIR": str(state / "task-registry"),
        "JIUWENSWARM_STARTUP_DIAGNOSTICS_DIR": str(state / "startup"),
        "AGENTOS_GATEWAY_LOG_DIR": str(state / "logs"),
        "OPENJIUWEN_CONTEXT_DEBUG_DIR": str(state / "context-debug"),
        "PYTHONUNBUFFERED": "1",
    }
    if read_only:
        # The child bootstrap uses these values to keep every write it owns in
        # the stage state even after native workspace activation changes cwd.
        environment.update({
            "JIUWENSWARM_STAGE_READ_ONLY": "1",
            "JIUWENSWARM_STAGE_WRITES_DIR": str(state),
            "JIUWENSWARM_STAGE_VIEW_DIRS": os.pathsep.join(str(view) for view in views),
            "TMPDIR": str(state / "tmp"), "TEMP": str(state / "tmp"), "TMP": str(state / "tmp"),
            "PYTHONPYCACHEPREFIX": str(state / "pycache"),
        })
    return environment


def _overlaps(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _resolved(path: Path) -> Path:
    return Path(os.path.realpath(path))


def _validate_stage_paths(state: Path, output_dir: Path, workspace: Path) -> None:
    """Refuse symlink aliases and overlaps before any artifact or state write."""
    state, output, view = _resolved(state), _resolved(output_dir), _resolved(workspace)
    if not view.is_dir():
        raise WorkerConfigurationError("Stage project directory must exist.",
                                       code="INVALID_WORKSPACE", output_dir=output_dir)
    if _overlaps(state, view):
        raise WorkerConfigurationError("Native state cannot alias or overlap the project directory.",
                                       code="STAGE_PATH_OVERLAP", output_dir=output_dir)
    if _overlaps(output, view):
        raise WorkerConfigurationError("Stage output cannot alias or overlap the project directory.",
                                       code="OUTPUT_VIEW_OVERLAP", output_dir=output_dir)
    if state == output or state in output.parents:
        raise WorkerConfigurationError("Call artifacts cannot be stored inside mutable native state.",
                                       code="OUTPUT_INSIDE_STATE", output_dir=output_dir)


def _stage_views(workspace: Path) -> tuple[Path, ...]:
    return (_resolved(workspace),)


def _configure_stage_writes(config: dict, state: Path) -> None:
    """Keep companion memory, traces and diagnostics inside the stage state.

    The memory store path is absolute so openjiuwen cannot resolve it against
    the request's frozen view. The remaining paths mirror the native settings
    already relocated by the worker.
    """
    _section(_section(config, "memory"), "store")["path"] = str(state / "memory" / "memory.db")
    _section(config, "trajectory_ui")["db_path"] = str(state / ".trace/sessions")
    _section(config, "agent_observability")["traces_dir"] = str(state / ".trace")
    _section(_section(config, "react"), "context_engine_config")["context_debug_dir"] = str(state / "context-debug")


def _native_interpreter(binary: Path) -> str:
    """Locate the interpreter of the native installation used to run ``binary``.

    An explicit setting wins. A Python script or shell wrapper names its own
    interpreter. Fall back to the interpreter that imported this companion.
    """
    explicit = os.environ.get("JIUWENSWARM_NATIVE_PYTHON", "").strip()
    if explicit:
        return explicit
    if binary.name.startswith("python") and os.access(binary, os.X_OK):
        return str(binary)
    try:
        head = binary.read_bytes()[:4096]
    except OSError:
        head = b""
    if head.startswith(b"#!"):
        try:
            parts = shlex.split(head.splitlines()[0][2:].decode("utf-8", "replace").strip())
        except (ValueError, IndexError):
            parts = []
        if parts:
            candidate = parts[1] if os.path.basename(parts[0]) == "env" and len(parts) > 1 else parts[0]
            if "python" in os.path.basename(candidate).lower():
                resolved = shutil.which(candidate) or candidate
                if os.path.isfile(resolved):
                    return resolved
    text = head.decode("utf-8", "replace")
    for match in re.finditer(r"(?m)^\s*(?:exec\s+)?([^\s'\";]+python[0-9.]*)", text):
        candidate = match.group(1)
        resolved = shutil.which(candidate) or candidate
        if os.path.isfile(resolved):
            return resolved
    try:
        import jiuwenswarm as installed

        origin = Path(installed.__file__).resolve()
        for parent in origin.parents:
            if parent.name in {"site-packages", "dist-packages"}:
                candidate = parent.parent.parent / "bin" / "python"
                if candidate.is_file():
                    return str(candidate)
    except (ImportError, OSError, AttributeError, TypeError):
        pass
    return sys.executable


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


_REDACT_CHUNK_CHARS = 256 * 1024
_REDACT_OVERLAP_CHARS = 64 * 1024
_REDACT_MAX_LINE_CHARS = 8 * 1024 * 1024


class _Redactor:
    def __init__(self, environment: dict[str, str]) -> None:
        self.secrets = sorted({value for key, value in environment.items()
                               if _credential_key(key) and len(value) >= 4}, key=len, reverse=True)
        # A cut is only safe once at least two maximum secret lengths are held
        # back, so any credential spanning the cut is still fully visible.
        self.overlap_chars = max(_REDACT_OVERLAP_CHARS, 2 * max((len(value) for value in self.secrets), default=0))

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


def _token_like(value: str) -> bool:
    """Detect long base64-like literals: >=32 chars and >=4 bits/char Shannon entropy.

    This deterministic screen includes '/' so a base64 token cannot masquerade
    as a path. Dots and tildes exclude ordinary file names from this screen.
    """
    if len(value) < 32 or _TOKEN_ALPHABET.fullmatch(value) is None:
        return False
    return -sum((count / len(value)) * math.log2(count / len(value))
                for count in Counter(value).values()) >= 4.0


def _credential_path(key: str, value: str) -> bool:
    normalized_key = key.replace("-", "_").lower()
    if not normalized_key.endswith(("_file", "_path")):
        return False
    if _SECRET_PREFIX.search(value) or _token_like(value):
        return False
    if _PATH_USERINFO.search(value) or _SCHEMELESS_AUTHORITY_AT.search(value):
        return False
    return "://" not in value and (
        value.startswith(("~", "/", "./")) or "/" in value
    )


def _credential_free(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if not _credential_free(item):
                return False
            if _credential_key(str(key)) and item not in (None, ""):
                if not isinstance(item, str) or not (
                    _ENV_REFERENCE.fullmatch(item) or _credential_path(str(key), item)
                ):
                    return False
    elif isinstance(value, list):
        return all(_credential_free(item) for item in value)
    elif isinstance(value, str):
        # Credentials embedded in URLs are not environment-only configuration.
        if _URL_USERINFO.search(value):
            return False
        if _SECRET_QUERY.search(value):
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


# Role turns (implementation/review and native mode) only consume the final
# response, usage, session and status. Their complete records stay in
# events.jsonl; the in-memory copy keeps message-boundary events whole and
# bounds every other payload string, so long tool streams cannot exhaust the
# controller process. Clarification stages keep complete in-memory events.
RETAINED_PAYLOAD_STRING_LIMIT = 4096
# Total in-memory event payload budget, independent of event count. Complete
# records remain durable in events.jsonl; the stream summary keeps the final
# response and usage needed by role turns even after older events are dropped.
RETAINED_EVENT_TOTAL_BYTES = 8 * 1024 * 1024
_RETAINED_WHOLE_EVENTS = frozenset({
    "chat.final", "chat.delta", "chat.usage_summary", "chat.usage_metadata", "chat.tool_policy",
})
MEMORY_RESERVE_MIN_BYTES = 512 * 1024 * 1024
MEMORY_RESERVE_MAX_BYTES = 2 * 1024 * 1024 * 1024
MEMORY_RESERVE_FRACTION = 0.15
MEMORY_WATCHDOG_INTERVAL_SECONDS = 1.0
MEMORY_ENV_LIMIT = "LONG_HORIZON_MEMORY_MAX_BYTES"
MEMORY_ENV_RESERVE = "LONG_HORIZON_MEMORY_RESERVE_BYTES"
MEMORY_ENV_CGROUP = "LONG_HORIZON_MEMORY_CGROUP"
MEMORY_CHECKPOINT_SCHEMA = "long-horizon/memory-checkpoint/v1"
OOM_PRIORITY_SCHEMA = "long-horizon/oom-priority/v1"
OOM_SCORE_ADJ_CHILD = 800
MEMORY_ROLE_SHARE_THRESHOLD = 0.25
DEFAULT_REVIEWER_MEMORY_SHARE = 0.80
REVIEWER_MEMORY_SHARE_ENV = "LONG_HORIZON_REVIEWER_MEMORY_SHARE"


def reviewer_memory_share() -> float:
    """Reviewer share of effective cgroup usage, configurable by environment."""
    raw = os.environ.get(REVIEWER_MEMORY_SHARE_ENV, "").strip()
    try:
        value = float(raw) if raw else DEFAULT_REVIEWER_MEMORY_SHARE
    except (TypeError, ValueError):
        value = DEFAULT_REVIEWER_MEMORY_SHARE
    return min(0.95, max(0.25, value))


def _role_memory_share(stage: str) -> float:
    """Worker roles retain 25%; Reviewer stages may own the configured share."""
    return reviewer_memory_share() if stage.startswith("benchmark-reviewer") else MEMORY_ROLE_SHARE_THRESHOLD


def compact_role_events(stage: str) -> bool:
    return stage == "native-code" or stage.startswith("benchmark-")


def _bounded_payload(value: Any, limit: int = RETAINED_PAYLOAD_STRING_LIMIT) -> Any:
    if isinstance(value, str):
        if len(value) <= limit:
            return value
        return value[:limit] + f"...[{len(value)} chars; complete record retained in events.jsonl]"
    if isinstance(value, list):
        return [_bounded_payload(item, limit) for item in value]
    if isinstance(value, dict):
        return {key: _bounded_payload(item, limit) for key, item in value.items()}
    return value


def retained_record(record: dict) -> dict:
    """The bounded in-memory copy of one already persisted native record."""
    if record.get("type") != "event" or record.get("event_type") in _RETAINED_WHOLE_EVENTS:
        return record
    return {key: (_bounded_payload(item) if key == "payload" else item) for key, item in record.items()}


def _record_bytes(record: dict) -> int:
    # Conservative default serialization: retained compact bytes stay below
    # the budget even for callers that re-serialize retained events.
    return len(json.dumps(record, ensure_ascii=False, allow_nan=False).encode("utf-8"))


class _RetainedEventStream:
    """Bounded in-memory events plus a streaming delta/usage summary."""

    def __init__(self, limit: int = RETAINED_EVENT_TOTAL_BYTES, *, compact: bool = False) -> None:
        self.limit = max(1024, int(limit))
        self.compact = compact
        self.records: list[dict] = []
        self.sizes: list[int] = []
        self.total_bytes = 0
        self.dropped = 0
        self.text_parts: list[str] = []
        self.text_bytes = 0
        self.text_truncated = False
        self.usage: dict = {}
        self.has_usage_summary = False
        self.session = ""

    def _remember_text(self, value: str) -> None:
        encoded = value.encode("utf-8")
        remaining = self.limit - self.text_bytes
        if remaining <= 0:
            self.text_truncated = True
            return
        if len(encoded) > remaining:
            value = encoded[:remaining].decode("utf-8", errors="ignore")
            self.text_truncated = True
        if value:
            self.text_parts.append(value)
            self.text_bytes += len(value.encode("utf-8"))

    def _remember_usage(self, record: dict) -> None:
        payload = record.get("payload")
        if not isinstance(payload, dict):
            return
        kind = record.get("event_type")
        if kind == "chat.usage_summary" and isinstance(payload.get("usage"), dict):
            self.usage = dict(payload["usage"])
            self.has_usage_summary = True
        elif kind == "chat.usage_metadata" and not self.has_usage_summary:
            metadata = payload.get("metadata")
            if isinstance(metadata, dict) and isinstance(metadata.get("usage_metadata"), dict):
                for key, value in metadata["usage_metadata"].items():
                    if (key in {"input_tokens", "output_tokens", "total_tokens", "cache_tokens",
                                "input_cost", "output_cost", "total_cost"}
                            and isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0):
                        self.usage[key] = self.usage.get(key, 0) + value

    def feed(self, record: dict) -> None:
        if isinstance(record.get("session_id"), str):
            self.session = record["session_id"]
        kind = record.get("event_type")
        payload = record.get("payload")
        if kind == "chat.delta" and isinstance(payload, dict):
            for key in ("delta", "content", "text", "message", "answer"):
                if isinstance(payload.get(key), str):
                    self._remember_text(payload[key])
                    break
        self._remember_usage(record)
        if record.get("type") != "event":
            return
        candidate = retained_record(record) if self.compact else record
        size = _record_bytes(candidate)
        if size > self.limit:
            # One pathological record still cannot own more than the whole
            # budget. Keep its correlation keys and a bounded payload.
            candidate = _bounded_payload(candidate, max(1024, self.limit // 8))
            size = _record_bytes(candidate)
            if size > self.limit:
                self.dropped += 1
                return
        while self.records and self.total_bytes + size > self.limit:
            self.total_bytes -= self.sizes.pop(0)
            self.records.pop(0)
            self.dropped += 1
        self.records.append(candidate)
        self.sizes.append(size)
        self.total_bytes += size

    def retained(self) -> list[dict]:
        return self.records

    def summary(self) -> tuple[str, dict, str]:
        text = "".join(self.text_parts)
        if self.text_truncated:
            text += "...[stream summary truncated at retained event budget]"
        return text, dict(self.usage), self.session


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


def _redact_log_file(path: Path, redactor: _Redactor) -> bool:
    """Stream one diagnostic file through the redactor line by line.

    A secret that straddles a read-chunk boundary stays in the incomplete-line
    buffer, so the whole line is redacted exactly once. A single line longer
    than ``_REDACT_MAX_LINE_CHARS`` is redacted as a whole before any prefix is
    emitted; the retained overlap is at least twice the longest secret, so a
    credential crossing a long-line cut is always seen complete before output.
    """
    chunk_chars = _REDACT_CHUNK_CHARS
    overlap = redactor.overlap_chars
    max_line_chars = _REDACT_MAX_LINE_CHARS
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.redact")
    changed = False
    try:
        with path.open("r", encoding="utf-8") as source, temporary.open("w", encoding="utf-8") as target:
            pending = ""
            while True:
                block = source.read(chunk_chars)
                if not block:
                    break
                data = pending + block
                start = 0
                while True:
                    newline = data.find("\n", start)
                    if newline < 0:
                        break
                    line = data[start:newline + 1]
                    scrubbed = redactor.text(line)
                    changed = changed or scrubbed != line
                    target.write(scrubbed)
                    start = newline + 1
                pending = data[start:]
                if len(pending) > max_line_chars:
                    # Pathological single line: redact the complete buffer
                    # before cutting so a secret spanning the cut is replaced.
                    scrubbed = redactor.text(pending)
                    changed = changed or scrubbed != pending
                    if len(scrubbed) > overlap:
                        target.write(scrubbed[:-overlap])
                        pending = scrubbed[-overlap:]
                    else:
                        pending = scrubbed
            if pending:
                scrubbed = redactor.text(pending)
                changed = changed or scrubbed != pending
                target.write(scrubbed)
        if changed:
            os.chmod(temporary, path.stat().st_mode)
            os.replace(temporary, path)
        else:
            temporary.unlink(missing_ok=True)
        return changed
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _redact_native_state(state: Path, redactor: _Redactor) -> int:
    """Scrub diagnostic logs, never the live native history used for resume."""
    count = 0
    for root, directories, files in os.walk(state, followlinks=False):
        directories[:] = [name for name in directories if not (Path(root) / name).is_symlink()]
        for name in files:
            path = Path(root) / name
            relative = path.relative_to(state)
            log_roots = ("openjiuwen-logs", "logs", "startup", "context-debug", "stage-writes")
            is_log = relative.parts[0] in log_roots or relative.parts[:2] == ("agent", ".logs")
            if not is_log:
                continue
            if path.is_symlink() or path.suffix.lower() not in {".json", ".jsonl", ".log", ".txt", ".yaml", ".yml"}:
                continue
            try:
                if _redact_log_file(path, redactor):
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


def _memory_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _memory_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None


def _set_oom_score_adj(pid: int, *, proc_root: Path = Path("/proc")) -> dict:
    """Prefer this child for the OOM killer; a write failure is recorded, not raised."""
    path = Path(proc_root) / str(int(pid)) / "oom_score_adj"
    try:
        path.write_text(f"{OOM_SCORE_ADJ_CHILD}\n", encoding="utf-8")
    except (OSError, UnicodeError, ValueError) as error:
        return {
            "schema": OOM_PRIORITY_SCHEMA, "pid": int(pid), "requested": OOM_SCORE_ADJ_CHILD,
            "observed": None, "status": "degraded", "reason": type(error).__name__,
        }
    try:
        observed = int(path.read_text(encoding="utf-8").strip())
    except (OSError, UnicodeError, ValueError):
        observed = None
    return {
        "schema": OOM_PRIORITY_SCHEMA, "pid": int(pid), "requested": OOM_SCORE_ADJ_CHILD,
        "observed": observed, "status": "applied" if observed == OOM_SCORE_ADJ_CHILD else "degraded",
        "reason": None if observed == OOM_SCORE_ADJ_CHILD else "unverified",
    }


def _process_tree_rss_bytes(pid: int, *, proc_root: Path = Path("/proc")) -> int | None:
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError):
        page_size = 4096
    rows: dict[int, tuple[int, int]] = {}
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return None
    for entry in entries:
        if not entry.isdigit():
            continue
        child = int(entry)
        try:
            stat = (Path(proc_root) / str(child) / "stat").read_text(encoding="utf-8")
            fields = stat[stat.rindex(")") + 2:].split()
            rows[child] = (int(fields[1]), max(0, int(fields[21])))
        except (OSError, UnicodeError, ValueError, IndexError):
            continue
    if pid not in rows:
        return None
    total = 0
    pending = [pid]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        total += rows[current][1] * page_size
        pending.extend(child for child, (parent, _rss) in rows.items() if parent == current)
    return total


def _memory_pressure_decision(
    pid: int, snapshot: dict, *, threshold: float = MEMORY_ROLE_SHARE_THRESHOLD
) -> dict:
    rss = _process_tree_rss_bytes(pid)
    usage = snapshot.get("effective_bytes")
    if usage is None:
        usage = snapshot.get("current_bytes")
    if rss is None or not isinstance(usage, int) or usage <= 0:
        return {
            "stop": False, "reason": "unattributable_pressure",
            "rss_bytes": rss, "current_bytes": usage, "effective_bytes": usage, "role_share": None,
            "threshold": threshold,
        }
    share = rss / usage
    stop = share >= threshold
    return {
        "stop": stop,
        "reason": "role_memory_pressure" if stop else "cgroup_memory_pressure_other_process",
        "rss_bytes": rss, "current_bytes": usage, "effective_bytes": usage, "role_share": share,
        "threshold": threshold,
    }


def _memory_cgroup(configured: Path | str | None = None) -> Path | None:
    if configured is not None:
        try:
            return Path(configured)
        except (OSError, ValueError, TypeError):
            return None
    root = Path("/sys/fs/cgroup")
    relative: str | None = None
    try:
        lines = Path("/proc/self/cgroup").read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        lines = []
    for line in lines:
        if line.startswith("0::"):
            relative = line[3:].lstrip("/")
            break
    if relative is not None and ".." not in Path(relative).parts:
        candidate = root / relative
        if (candidate / "memory.current").is_file() or (candidate / "memory.max").is_file():
            return candidate
    if (root / "memory.current").is_file() or (root / "memory.max").is_file():
        return root
    return None


def _memory_reserve(limit: int | None) -> int:
    if limit is None or limit <= 0:
        return 0
    return max(MEMORY_RESERVE_MIN_BYTES,
               min(MEMORY_RESERVE_MAX_BYTES, int(limit * MEMORY_RESERVE_FRACTION)))


def _memory_stat_counters(location: Path | None) -> dict[str, int] | None:
    """Read the memory.stat counters that separate reclaimable page cache."""
    if location is None:
        return None
    raw = _memory_text(location / "memory.stat")
    if raw is None:
        return None
    names = ("file", "file_dirty", "file_writeback")
    values: dict[str, int] = {}
    for line in raw.splitlines():
        fields = line.split()
        if len(fields) != 2 or fields[0] not in names:
            continue
        try:
            value = int(fields[1])
        except ValueError:
            return None
        if value < 0:
            return None
        values[fields[0]] = value
    if "file" not in values:
        return None
    values.setdefault("file_dirty", 0)
    values.setdefault("file_writeback", 0)
    return values


def _memory_usage(location: Path | None, current: int | None) -> tuple[int | None, str]:
    """Return effective non-reclaimable usage and the basis used to compute it."""
    if current is None:
        return None, "unavailable"
    counters = _memory_stat_counters(location)
    if counters is None:
        return current, "memory.current"
    effective = max(
        0,
        current - counters["file"] + counters["file_dirty"] + counters["file_writeback"],
    )
    return effective, "memory.stat"


def _memory_snapshot(
    *, limit_bytes: int | None = None, reserve_bytes: int | None = None,
    cgroup: Path | str | None = None,
) -> dict | None:
    """Observe cgroup v2 memory without ever raising or writing cgroup state."""
    location = _memory_cgroup(cgroup)
    maximum_raw = _memory_text(location / "memory.max") if location is not None else None
    maximum = None if maximum_raw in (None, "max") else _memory_int(maximum_raw)
    limit = _memory_int(limit_bytes) or maximum
    if limit is None:
        return None
    current = _memory_int(_memory_text(location / "memory.current")) if location is not None else None
    if current is None and location is not None and _memory_text(location / "memory.current") == "0":
        current = 0
    effective, usage_basis = _memory_usage(location, current)
    reserve = _memory_reserve(limit) if reserve_bytes is None else max(0, reserve_bytes)
    threshold = max(0, limit - reserve)
    degraded_reason = None
    if maximum is None:
        degraded_reason = "memory_max_unavailable"
    if current is None:
        degraded_reason = "memory_current_unavailable"
    assessable = degraded_reason is None and effective is not None
    return {
        "schema": "long-horizon/memory-budget/v1",
        "cgroup": str(location) if location is not None else None,
        "limit_bytes": limit,
        "current_bytes": current,
        "effective_bytes": effective,
        "usage_basis": usage_basis,
        "reserve_bytes": reserve,
        "threshold_bytes": threshold,
        "degraded": degraded_reason is not None,
        "degraded_reason": degraded_reason,
        "pressure_assessable": assessable,
        "at_pressure": assessable and effective >= threshold,
        "observed_at_unix": time.time(),
    }


def _memory_from_environment() -> dict | None:
    limit = _memory_int(os.environ.get(MEMORY_ENV_LIMIT))
    reserve = _memory_int(os.environ.get(MEMORY_ENV_RESERVE))
    location = os.environ.get(MEMORY_ENV_CGROUP) or None
    if limit is None:
        return None
    return _memory_snapshot(limit_bytes=limit, reserve_bytes=reserve, cgroup=location)


def _checkpoint_digest(body: dict) -> str:
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _write_memory_checkpoint(path: Path, payload: dict) -> dict:
    body = {"schema": MEMORY_CHECKPOINT_SCHEMA, "created_at_unix": time.time(), **payload}
    value = {**body, "sha256": _checkpoint_digest(body)}
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return value


class JiuwenWorker:
    def __init__(self, binary: Path, config_template: Path, source_commit: str = BASE_COMMIT,
                 default_timeout_seconds: float = 21600.0,
                 memory_limit_bytes: int | None = None,
                 memory_reserve_bytes: int | None = None,
                 memory_cgroup: Path | str | None = None,
                 memory_poll_seconds: float = MEMORY_WATCHDOG_INTERVAL_SECONDS,
                 native_python: Path | str | None = None) -> None:
        self.binary = Path(binary).expanduser().resolve()
        self.config_template = Path(config_template).expanduser().resolve()
        self.native_python = Path(native_python).expanduser() if native_python is not None else None
        self.source_commit = source_commit
        self.default_timeout_seconds = default_timeout_seconds
        self.memory_limit_bytes = memory_limit_bytes
        self.memory_reserve_bytes = memory_reserve_bytes
        self.memory_cgroup = memory_cgroup
        self.memory_poll_seconds = max(0.05, float(memory_poll_seconds))
        self.benchmark_source_root: Path | None = None
        # Deliberately process-local: no implicit cross-workflow restoration.
        self._sessions: dict[str, _SessionLease] = {}
        self._state_directories: set[Path] = set()

    def _native_command(self, *, state: Path, read_only: bool,
                        reviewer_launch_policy=None) -> tuple[list[str], object | None]:
        """Build the child command; read-only stages use the companion bootstrap.

        ``self.binary`` is the native installation's launcher. It is not a
        write boundary, so read-only calls invoke the pinned companion through
        the native interpreter instead of letting the launcher choose its own
        initialization order.
        """
        if read_only:
            interpreter = (str(self.native_python) if self.native_python is not None
                           else _native_interpreter(self.binary))
            command = [
                interpreter, "-X", f"pycache_prefix={state / 'pycache'}",
                "-m", "jiuwenswarm.long_horizon.native_entry", "--run-json", "-",
            ]
        else:
            command = [str(self.binary), "--run-json", "-"]
        proof = None
        if reviewer_launch_policy is not None:
            command, proof = reviewer_launch_policy.command(command)
        return command, proof

    def _admission_identity(self, *, stage: str, workspace: Path, read_only: bool,
                            schema: dict | None, tool_policy: str, environment: dict[str, str]) -> dict:
        try:
            template = self.config_template.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            template = ""
        return {
            "stage": stage, "workspace": str(workspace), "read_only": read_only, "tool_policy": tool_policy,
            "benchmark_workspace_policy": self.benchmark_workspace_policy,
            "schema_sha256": _object_sha256(schema), "source": _source_identity(self.source_commit, self.binary),
            "config_template_path": str(self.config_template), "config_template_sha256": _sha256(self.config_template),
            # Values (including provider credentials) never enter a receipt.
            "referenced_environment_sha256": _environment_sha256(template, environment),
        }

    @staticmethod
    def _receipt_hashes(output_dir: Path) -> dict[str, str | None]:
        return {name: _sha256(output_dir / name) for name in _RECEIPT_FILES}

    @property
    def benchmark_workspace_policy(self) -> dict | None:
        if self.benchmark_source_root is None:
            return None
        return {"schema": "jiuwenswarm-benchmark/workspace-policy/v1",
                "source_root": str(self.benchmark_source_root),
                "preserve_existing_tests": True, "automatic_path_rewrite": False}

    def _benchmark_prompt(self, prompt: str, workspace: Path, read_only: bool) -> str:
        if self.benchmark_source_root is None:
            return prompt
        source = self.benchmark_source_root
        instruction = (
            "Benchmark execution constraints (apply to every tool and subagent):\n"
            f"The authoritative workspace for this role is {workspace}.\n"
            f"The original task mentions {source}; it identifies the frozen source baseline. "
            "Resolve task source paths by their relative path under the authoritative workspace. "
            "Keep the original baseline unchanged. Do not run writes, git operations, dependency "
            "installation or build commands against the original baseline. "
            "All source edits and build outputs belong to this role's workspace.\n"
            "Preserve existing test files and benchmark/verifier assets. Add public regression "
            "checks in a separate new checks directory; do not modify or delete existing tests, "
            "test fixtures, hidden checks or acceptance rules. A failed check is evidence, "
            "not permission to weaken it.\n"
        )
        instruction += _PROCESS_KILL_GUIDANCE
        if read_only:
            instruction += "This role is read-only: inspect evidence without changing any files.\n"
        return instruction + "\nOriginal role request follows verbatim:\n" + prompt

    async def run(self, *, stage: str, prompt: str, workspace: Path, output_dir: Path,
                  read_only: bool = False, schema: dict | None = None,
                  timeout_seconds: float | None = None, session_id: str | None = None,
                  tool_policy: str = "configured", reviewer_launch_policy=None,
                  environment: dict[str, str] | None = None) -> WorkerResult:
        """Run or explicitly correct one successful native role session.

        Every result contains only this request's events and usage. Callers
        own bounded correction policy and must aggregate actual request chains.
        A continuation keeps the initial deadline and persisted native state;
        it never recreates a missing session or repeats a failed invocation.
        """
        output_dir = Path(output_dir).resolve()
        workspace = Path(workspace).resolve()
        prompt = self._benchmark_prompt(prompt, workspace, read_only)
        if tool_policy not in ("configured", "none"):
            raise WorkerConfigurationError("Native tool policy must be configured or none.",
                                           code="INVALID_TOOL_POLICY", output_dir=output_dir)
        timeout = self.default_timeout_seconds if timeout_seconds is None else timeout_seconds
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or timeout <= 0):
            raise WorkerConfigurationError("Stage timeout must be finite and positive.",
                                           code="INVALID_TIMEOUT", output_dir=output_dir)
        admitted_monotonic, admitted_unix = time.monotonic(), time.time()
        environment = dict(os.environ if environment is None else environment)
        if reviewer_launch_policy is not None:
            if not stage.startswith("benchmark-reviewer"):
                raise WorkerConfigurationError("Reviewer policy cannot bind a solver stage.",
                                               code="INVALID_REVIEWER_POLICY", output_dir=output_dir)
            environment = reviewer_launch_policy.environment(environment)
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
                                      {**environment, **_state_environment(
                                          lease.state_dir, read_only=read_only,
                                          views=_stage_views(workspace))}) != lease.config_environment_sha256:
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
                reviewer_launch_policy=reviewer_launch_policy,
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
                        call_deadline_monotonic: float, reviewer_launch_policy=None) -> WorkerResult:
        timeout = call_deadline_monotonic - time.monotonic()
        state = lease.state_dir if lease else output_dir / "state"
        _validate_stage_paths(state, output_dir, workspace)
        if not workspace.is_dir() or output_dir == workspace or workspace in output_dir.parents:
            raise WorkerConfigurationError("Stage artifacts must be outside the existing project directory.",
                                           code="INVALID_WORKSPACE", output_dir=output_dir)
        if any(output_dir == registered or registered in output_dir.parents
               for registered in self._state_directories):
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
        _validate_stage_paths(state, output_dir, workspace)
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
                # companion-owned writes and apply the explicit phase policy.
                _section(config, "trajectory_ui")["db_path"] = str(state / ".trace/sessions")
                _section(config, "agent_observability")["traces_dir"] = str(state / ".trace")
                _section(_section(config, "react"), "context_engine_config")["context_debug_dir"] = str(state / "context-debug")
                if read_only:
                    _configure_stage_writes(config, state)
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
        views = _stage_views(workspace)
        environment.update(_state_environment(state, read_only=read_only, views=views))
        metadata["stage_writes"] = {
            "schema": "long-horizon/stage-writes/v1",
            "read_only": read_only,
            "state_dir": str(state),
            "view_dirs": [str(view) for view in views],
            "log_dir": str(state / "openjiuwen-logs") if read_only else None,
            "memory_db": str(state / "memory" / "memory.db") if read_only else None,
            "temporary_dir": str(state / "tmp") if read_only else None,
            "mechanism": "companion-native-entry" if read_only else "native-launcher",
        }
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
        command, _proof = self._native_command(state=state, read_only=read_only)
        metadata.update(status="running", command=command)
        _json_write(output_dir / "metadata.json", metadata, redactor)
        compact = compact_role_events(stage)
        retained = _RetainedEventStream(compact=compact)
        metadata["retained_event_payload_limit"] = RETAINED_PAYLOAD_STRING_LIMIT if compact else None
        metadata["retained_event_total_limit"] = retained.limit
        terminal: dict | None = None
        protocol_error: str | None = None
        process: asyncio.subprocess.Process | None = None
        interrupted = False
        budget_observed = False
        budget_stop_task: asyncio.Task | None = None
        policy_stop_task: asyncio.Task | None = None
        memory_stop_task: asyncio.Task | None = None
        policy_proofs: list[int] = []
        native_session: str | None = session_id
        seen_records = 0
        started = time.monotonic()
        configured_limit = self.memory_limit_bytes or _memory_int(os.environ.get(MEMORY_ENV_LIMIT))
        configured_reserve = (self.memory_reserve_bytes if self.memory_reserve_bytes is not None
                              else _memory_int(os.environ.get(MEMORY_ENV_RESERVE)))
        configured_cgroup = self.memory_cgroup or os.environ.get(MEMORY_ENV_CGROUP)
        memory_enabled = configured_limit is not None or configured_cgroup is not None
        memory_pressure_observed = False
        memory_pressure: dict | None = None
        role_memory_share = _role_memory_share(stage)
        metadata["memory_watchdog_enabled"] = memory_enabled
        metadata["memory_watchdog_role_share_threshold"] = role_memory_share
        if memory_enabled:
            metadata["memory_budget_initial"] = _memory_snapshot(
                limit_bytes=configured_limit, reserve_bytes=configured_reserve, cgroup=configured_cgroup
            )

        def observe_memory() -> dict | None:
            if not memory_enabled:
                return None
            return _memory_snapshot(limit_bytes=configured_limit, reserve_bytes=configured_reserve,
                                    cgroup=configured_cgroup)

        def memory_checkpoint(reason: str, snapshot: dict, status: str) -> dict:
            return _write_memory_checkpoint(
                output_dir / "memory-checkpoint.json",
                {
                    "reason": reason,
                    "status": status,
                    "stage": stage,
                    "request_id": request_id,
                    "workspace": str(workspace),
                    "child_pid": process.pid if process is not None else None,
                    "memory": snapshot,
                    "retained_event_bytes": retained.total_bytes,
                    "retained_events_dropped": retained.dropped,
                },
            )

        async def memory_watchdog() -> None:
            nonlocal memory_pressure_observed, memory_pressure
            other_pressure_recorded = False
            try:
                while process is not None and process.returncode is None:
                    await asyncio.sleep(max(0.05, self.memory_poll_seconds))
                    if process.returncode is not None:
                        return
                    snapshot = observe_memory()
                    if snapshot is None or not snapshot.get("at_pressure"):
                        continue
                    decision = _memory_pressure_decision(
                        process.pid, snapshot, threshold=role_memory_share
                    )
                    if not decision["stop"]:
                        if not other_pressure_recorded:
                            other_pressure_recorded = True
                            metadata["memory_watchdog_other_pressure"] = {
                                **decision, "snapshot": snapshot, "observed_at_unix": time.time(),
                            }
                            try:
                                _json_write(output_dir / "metadata.json", metadata, redactor)
                            except Exception:
                                pass
                        continue
                    memory_pressure_observed = True
                    memory_pressure = snapshot
                    metadata.update(status="memory_pressure", failure_code="MEMORY_PRESSURE",
                                    memory_watchdog=snapshot,
                                    memory_watchdog_role_share=decision["role_share"],
                                    memory_watchdog_role_share_threshold=decision.get("threshold"),
                                    memory_watchdog_rss_bytes=decision["rss_bytes"],
                                    memory_watchdog_at_unix=time.time(),
                                    memory_watchdog_native_sequence=seen_records)
                    # Stop before writing any evidence. An unwritable artifact
                    # must never keep a pressure-owning child alive.
                    try:
                        await _terminate_group(process, grace=1.0)
                    except Exception as error:
                        metadata["memory_stop_error"] = type(error).__name__[:128]
                    try:
                        memory_checkpoint("cgroup_memory_pressure", snapshot, "stopping_child")
                    except Exception as error:
                        metadata["memory_checkpoint_error"] = type(error).__name__[:128]
                    try:
                        _json_write(output_dir / "metadata.json", metadata, redactor)
                    except Exception:
                        pass
                    return
            except asyncio.CancelledError:
                raise
            except Exception as error:
                metadata["memory_watchdog_error"] = type(error).__name__[:128]
                metadata["memory_watchdog_error_detail"] = (str(error) or repr(error))[:200]
                try:
                    _json_write(output_dir / "metadata.json", metadata, redactor)
                except Exception:
                    pass

        async def read_stdout() -> None:
            nonlocal terminal, protocol_error, budget_observed, budget_stop_task, policy_stop_task, native_session, seen_records
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
                            if record.get("sequence") != seen_records or record.get("request_id") != request_id:
                                raise ValueError("invalid correlation or sequence")
                            seen_records += 1
                            if native_session and record.get("session_id") != native_session:
                                raise ValueError("native session identity changed")
                            if record.get("type") == "result":
                                OneShotRunResult.from_dict(record)
                            else:
                                OneShotEvent.from_dict(record)
                            if record.get("session_id"):
                                native_session = record["session_id"]
                            safe_record = redactor.value(record)
                            if record.get("type") == "result":
                                terminal = safe_record
                            else:
                                retained.feed(safe_record)
                            event_log.write(json.dumps(safe_record, ensure_ascii=False, allow_nan=False) + "\n")
                            event_log.flush()
                            metadata["retained_event_bytes"] = retained.total_bytes
                            metadata["retained_events_dropped"] = retained.dropped
                            metadata["retained_delta_bytes"] = retained.text_bytes
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
            before_spawn = observe_memory()
            if before_spawn is not None and before_spawn.get("at_pressure"):
                memory_pressure_observed = True
                memory_pressure = before_spawn
                metadata.update(status="memory_pressure", failure_code="MEMORY_PRESSURE",
                                memory_watchdog=before_spawn,
                                memory_watchdog_at_unix=time.time(),
                                memory_watchdog_native_sequence=0)
                try:
                    memory_checkpoint("pre_spawn_memory_pressure", before_spawn, "stopped_before_spawn")
                except Exception as error:
                    metadata["memory_checkpoint_error"] = type(error).__name__[:128]
                try:
                    _json_write(output_dir / "metadata.json", metadata, redactor)
                except Exception as error:
                    metadata["metadata_write_error"] = type(error).__name__[:128]
                pressure_result = WorkerResult("", [], session_id or "", {}, "memory_pressure", 1)
                raise WorkerExecutionError(
                    "Native worker was not started because the cgroup memory watchdog threshold was reached.",
                    code="MEMORY_PRESSURE", output_dir=output_dir, result=pressure_result,
                ) from None
            if reviewer_launch_policy is not None:
                reviewer_launch_policy.grant_traversal(output_dir)
                reviewer_launch_policy.own_tree(state)
                reviewer_launch_policy.check_managed(state)
            command, proof = self._native_command(
                state=state, read_only=read_only, reviewer_launch_policy=reviewer_launch_policy)
            metadata["command"] = command
            _json_write(output_dir / "metadata.json", metadata, redactor)
            spawning = asyncio.create_task(asyncio.create_subprocess_exec(
                *command, cwd=state, env=environment,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, start_new_session=True, limit=8 * 1024 * 1024,
            ))
            try:
                process = await asyncio.shield(spawning)
            except asyncio.CancelledError:
                interrupted = True
                process = await finish_cleanup(spawning)
            if reviewer_launch_policy is not None:
                try:
                    await asyncio.to_thread(reviewer_launch_policy.verify, process.pid, proof)
                except Exception:
                    reviewer_launch_policy.enforcement_error = "reviewer child credential verification failed"
                    await finish_cleanup(_terminate_group(process))
                    raise
            metadata["oom_priority"] = _set_oom_score_adj(process.pid)
            if interrupted:
                await finish_cleanup(_terminate_group(process))
                await finish_cleanup(drain_after_stop(asyncio.gather(read_stdout(), read_stderr(), process.wait())))
            else:
                if memory_enabled:
                    memory_stop_task = asyncio.create_task(memory_watchdog())
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
            if memory_stop_task is not None:
                if not memory_stop_task.done():
                    memory_stop_task.cancel()
                await asyncio.gather(memory_stop_task, return_exceptions=True)
            if memory_pressure_observed:
                metadata.update(memory_pressure_observed=True, memory_watchdog=memory_pressure)
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
        safe_records = retained.retained()
        partial_text, partial_usage, partial_session = retained.summary()
        final_text, final_provenance = (
            select_native_final_response(safe_records, terminal)
            if terminal is not None else (partial_text, {"source": "partial_native_stream"})
        )
        if not final_text and partial_text:
            final_text, final_provenance = partial_text, {"source": "partial_native_stream"}
        result = WorkerResult(
            final_text=final_text,
            events=safe_records,
            session_id=str((terminal or {}).get("session_id") or partial_session),
            usage=(terminal or {}).get("usage") or partial_usage,
            status=str((terminal or {}).get("status") or "failed"), exit_code=actual_exit,
        )
        failure: type[WorkerError] | None = None
        code = ""
        if interrupted:
            result.status = "cancelled"
            code = "CANCELLED"
        elif memory_pressure_observed:
            result.status = "memory_pressure"
            failure, code = WorkerExecutionError, "MEMORY_PRESSURE"
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
           "normalize_tool_events", "select_native_final_response", "READ_ONLY_TOOLS",
           "compact_role_events", "retained_record", "RETAINED_PAYLOAD_STRING_LIMIT",
           "RETAINED_EVENT_TOTAL_BYTES", "MEMORY_WATCHDOG_INTERVAL_SECONDS"]
