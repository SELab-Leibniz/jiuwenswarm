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
import stat
import subprocess
import sys
import tempfile
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

from .benchmark_write_guard import (
    NATIVE_WRITE_GUARD_POLICY, BenchmarkWriteGuard, WriteGuardError, public_test_directories, public_test_paths,
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
# The native browser config uses these public names directly with os.environ.get.
_BROWSER_KEY_FIELD = ("browser", "decision", "api_key_env")
_BROWSER_KEY_NAMES = frozenset({"OPENROUTER_API_KEY", "TYPESAFE_API_KEY", "API_KEY"})
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


_PREFLIGHT_ACCESS = r"""
import json
import os
import stat
import sys

payload = json.loads(sys.argv[1])
roots = [str(value) for value in payload.get("roots", [])]


def mode(value):
    return format(stat.S_IMODE(value), "04o")


def chain(path):
    rows = []
    try:
        absolute = os.path.abspath(path)
        resolved = os.path.realpath(path)
    except (OSError, ValueError):
        absolute, resolved = os.path.abspath(path), path
    seen = set()
    for candidate in (resolved, absolute):
        parts = []
        current = candidate
        while current and current not in seen:
            seen.add(current)
            parts.append(current)
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
        for item in reversed(parts):
            try:
                info = os.lstat(item)
            except OSError as error:
                rows.append({"path": item, "error": type(error).__name__})
            else:
                rows.append({
                    "path": item, "mode": mode(info.st_mode), "uid": info.st_uid,
                    "gid": info.st_gid, "kind": (
                        "symlink" if stat.S_ISLNK(info.st_mode) else
                        "dir" if stat.S_ISDIR(info.st_mode) else
                        "file" if stat.S_ISREG(info.st_mode) else "other"
                    ),
                })
    return rows


def failure(path, error):
    return {
        "path": str(path), "error": type(error).__name__, "detail": str(error),
        "errno": getattr(error, "errno", None),
        "errno_name": ("EACCES" if isinstance(error, PermissionError) else
                       ("ENOENT" if isinstance(error, FileNotFoundError) else None)),
        "euid": os.geteuid(), "egid": os.getegid(), "ancestors": chain(path),
    }


def check_file(path):
    with open(path, "rb") as stream:
        stream.read(1)


def check_directory(path, seen):
    resolved = os.path.realpath(path)
    if resolved in seen:
        return
    seen.add(resolved)
    with os.scandir(path) as entries:
        names = list(entries)
    for entry in names:
        child = entry.path
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError as error:
            failures.append(failure(child, error))
            continue
        if stat.S_ISLNK(info.st_mode):
            try:
                target = os.path.realpath(child)
                target_info = os.stat(target)
            except OSError as error:
                failures.append(failure(target, error))
                continue
            try:
                if stat.S_ISDIR(target_info.st_mode):
                    check_directory(target, seen)
                elif stat.S_ISREG(target_info.st_mode):
                    check_file(target)
            except OSError as error:
                failures.append(failure(target, error))
        elif stat.S_ISDIR(info.st_mode):
            try:
                check_directory(child, seen)
            except OSError as error:
                failures.append(failure(child, error))
        elif stat.S_ISREG(info.st_mode):
            try:
                check_file(child)
            except OSError as error:
                failures.append(failure(child, error))


failures = []
seen = set()
for root in roots:
    try:
        info = os.stat(root)
    except OSError as error:
        failures.append(failure(root, error))
        continue
    try:
        if stat.S_ISDIR(info.st_mode):
            check_directory(root, seen)
        elif stat.S_ISREG(info.st_mode):
            check_file(root)
    except OSError as error:
        failures.append(failure(root, error))
result = {
    "schema": "long-horizon/native-access-preflight/v1",
    "status": "failed" if failures else "ok",
    "roots": roots, "uid": os.getuid(), "gid": os.getgid(),
    "euid": os.geteuid(), "egid": os.getegid(), "failures": failures,
}
sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
sys.stdout.flush()
raise SystemExit(1 if failures else 0)
"""

_PREFLIGHT_DISCOVERY = r"""
import hashlib
import importlib.util
import json
import os
import sys

failures = []
packages = {}
for name in ("jiuwenswarm", "openjiuwen"):
    try:
        spec = importlib.util.find_spec(name)
    except Exception as error:
        failures.append({
            "module": name, "error": type(error).__name__,
            "detail": str(error),
        })
        continue
    if spec is None:
        failures.append({"module": name, "error": "ModuleNotFound"})
        continue
    origin = None if spec.origin in (None, "namespace") else spec.origin
    locations = [str(value) for value in (spec.submodule_search_locations or [])]
    if origin:
        locations.append(os.path.dirname(os.path.abspath(origin)))
    packages[name] = {
        "origin": origin,
        "locations": list(dict.fromkeys(locations)),
    }

try:
    paths = [str(value) for value in sys.path]
except Exception as error:
    paths = []
    failures.append({"module": "sys.path", "error": type(error).__name__, "detail": str(error)})

interpreter_paths = {}
try:
    import sysconfig

    configured = sysconfig.get_paths()
except Exception as error:
    configured = {}
    failures.append({"module": "sysconfig", "error": type(error).__name__, "detail": str(error)})
if not isinstance(configured, dict):
    failures.append({"module": "sysconfig", "error": "MalformedPaths"})
    configured = {}
for key in ("purelib", "platlib", "stdlib", "platstdlib"):
    value = configured.get(key)
    if value:
        interpreter_paths[key] = str(value)
try:
    pycache_prefix = sys.pycache_prefix
except AttributeError:
    pycache_prefix = None
try:
    cwd = os.getcwd()
except Exception as error:
    cwd = None
    failures.append({"module": "cwd", "error": type(error).__name__, "detail": str(error)})
try:
    environment = {
        key: os.environ[key] for key in sorted(os.environ)
    }
    environment_sha256 = hashlib.sha256(
        json.dumps(environment, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
except Exception as error:
    environment_sha256 = None
    failures.append({"module": "environment", "error": type(error).__name__, "detail": str(error)})
result = {
    "schema": "long-horizon/native-import-discovery/v1",
    "status": "failed" if failures else "ok",
    "executable": sys.executable,
    "prefix": sys.prefix,
    "base_prefix": sys.base_prefix,
    "cwd": cwd,
    "uid": os.getuid(), "gid": os.getgid(),
    "euid": os.geteuid(), "egid": os.getegid(),
    "pycache_prefix": pycache_prefix,
    "dont_write_bytecode": bool(sys.dont_write_bytecode),
    "environment_sha256": environment_sha256,
    "path": paths,
    "packages": packages,
    "sysconfig": interpreter_paths,
    "failures": failures,
}
sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
sys.stdout.flush()
raise SystemExit(1 if failures else 0)
"""


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
    try:
        browser_key = yaml.safe_load(config_text)
    except (yaml.YAMLError, ValueError, OverflowError):
        browser_key = None  # Malformed templates fail normal configuration admission.
    for field_name in _BROWSER_KEY_FIELD:
        browser_key = browser_key.get(field_name) if isinstance(browser_key, dict) else None
    if isinstance(browser_key, str) and browser_key in _BROWSER_KEY_NAMES:
        references.add(browser_key)
    return _object_sha256({key: environment.get(key) for key in sorted(references)})


def _environment_digest(environment: dict[str, str]) -> str:
    """Hash the full child environment for preflight parity evidence."""
    return hashlib.sha256(
        json.dumps(
            {key: environment[key] for key in sorted(environment)},
            sort_keys=True, ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


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
        # Applies before interpreter startup for every native stage, including
        # root writable ones. A restrictive umask must not publish 0700
        # bytecode directories into a shared native installation.
        "PYTHONDONTWRITEBYTECODE": "1",
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


def _notice_context(environment: dict[str, str]) -> dict[str, Any]:
    """Return the active notice identity without inventing collection time.

    The launcher may pass a JSON object or individual token-bound values. The
    context is copied verbatim into cancellation evidence; a missing context is
    reported as such and never synthesized from an unrelated timestamp.
    """
    context: dict[str, Any] = {}
    for name in NOTICE_CONTEXT_ENV:
        raw = environment.get(name)
        if not raw:
            continue
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict):
            context.update(value)
    canonical = {
        "LH_INVOCATION_ID": "invocation_id", "GB_LH_INVOCATION_ID": "invocation_id",
        "JIUWENSWARM_INVOCATION_ID": "invocation_id",
        "LH_INVOCATION_TOKEN": "invocation_token", "GB_LH_INVOCATION_TOKEN": "invocation_token",
        "LH_NOTICE_ID": "notice_id", "GB_LH_NOTICE_ID": "notice_id",
        "LH_NOTICE_TOKEN": "notice_token", "GB_LH_NOTICE_TOKEN": "notice_token",
    }
    for name in NOTICE_VALUE_ENV:
        value = environment.get(name)
        if value not in (None, ""):
            context.setdefault(canonical.get(name, name.lower()), value)
    return context


def _terminal_timestamp(terminal: dict | None) -> float | None:
    """Use the child's own terminal timestamp when it provides one."""
    if not isinstance(terminal, dict):
        return None
    for name in ("finished_at_unix", "ended_at_unix", "completed_at_unix", "timestamp", "at_unix"):
        value = terminal.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if math.isfinite(value) and value > 0:
            return float(value)
    return None


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


_SHELL_BASENAMES = frozenset({"sh", "bash", "dash", "zsh", "ksh", "ash", "busybox"})
_PYTHON_BASENAME = re.compile(r"^python(?:[0-9]+(?:\.[0-9]+)*)?(?:[a-z])?$", re.I)
_SHELL_VARIABLE = re.compile(
    r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)(?::([-+])([^}]*))?\}|([A-Za-z_][A-Za-z0-9_]*)|([0@]))"
)
_ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.S)


def _is_python_executable(path: str | Path) -> bool:
    return _PYTHON_BASENAME.fullmatch(Path(path).name) is not None


def _is_shell_executable(path: str | Path) -> bool:
    name = Path(path).name
    return name in _SHELL_BASENAMES or re.fullmatch(r"sh[0-9.]*", name) is not None


def _resolve_program(value: str, environment: dict[str, str], cwd: Path) -> str | None:
    """Resolve an absolute, relative or PATH-named program without running it."""
    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        return str(candidate)
    if "/" in value:
        return str((cwd / candidate).resolve())
    found = shutil.which(value, path=environment.get("PATH", os.defpath))
    return found


def _shell_expand(value: str, environment: dict[str, str], wrapper: Path) -> str:
    """Expand only the shell forms a launch wrapper can demonstrably use."""
    if "$(" in value or "`" in value:
        raise ValueError("shell command substitution is not supported")

    def replace(match: re.Match) -> str:
        name, operator, alternative, bare, special = match.groups()
        key = name or bare
        if special == "0":
            return str(wrapper)
        if special == "@":
            return ""
        current = environment.get(key, "")
        if operator == "-":
            return current if current else _shell_expand(alternative or "", environment, wrapper)
        if operator == "+":
            return _shell_expand(alternative or "", environment, wrapper) if current else ""
        return current

    return _SHELL_VARIABLE.sub(replace, value)


def _shell_tokens(line: str, environment: dict[str, str], wrapper: Path) -> list[str]:
    tokens = shlex.split(line, posix=True)
    expanded: list[str] = []
    for token in tokens:
        value = _shell_expand(token, environment, wrapper)
        # "$@" expands to the wrapper's launch arguments, which are not part of
        # the probe; drop it rather than inventing the child request argv.
        if token == "$@":
            continue
        expanded.append(value)
    return expanded


def _apply_assignment(token: str, environment: dict[str, str], wrapper: Path) -> bool:
    match = _ASSIGNMENT.match(token)
    if match is None:
        return False
    environment[match.group(1)] = _shell_expand(match.group(2), environment, wrapper)
    return True


def _consume_env_command(tokens: list[str], environment: dict[str, str],
                         wrapper: Path) -> tuple[dict[str, str], list[str]] | None:
    """Interpret ``env`` options/assignments and return the command tokens."""
    env = dict(environment)
    index = 0
    if tokens and Path(tokens[0]).name == "env":
        index = 1
    while index < len(tokens):
        token = tokens[index]
        if token in {"-S", "--split-string"}:
            index += 1
            continue
        if token in {"-i", "--ignore-environment"}:
            env = {"PATH": env.get("PATH", os.defpath)}
            index += 1
            continue
        if token in {"-u", "--unset"}:
            if index + 1 >= len(tokens):
                return None
            env.pop(tokens[index + 1], None)
            index += 2
            continue
        if token.startswith("--unset="):
            env.pop(token.split("=", 1)[1], None)
            index += 1
            continue
        if token in {"-C", "--chdir", "-P", "--default-signal", "--ignore-signal"}:
            index += 2
            continue
        if token.startswith("--chdir=") or token.startswith("--default-signal=") or token.startswith("--ignore-signal="):
            index += 1
            continue
        if token == "--":
            index += 1
            break
        if token.startswith("-"):
            index += 1
            continue
        if _apply_assignment(token, env, wrapper):
            index += 1
            continue
        break
    if index >= len(tokens):
        return None
    return env, tokens[index:]


def _python_probe_details(tokens: list[str], wrapper: Path, environment: dict[str, str],
                          cwd: Path, *, default_script: Path | None = None
                          ) -> tuple[str, tuple[str, ...], Path | None] | None:
    """Derive interpreter, flags and script directory from a Python argv tail.

    Only demonstrable console-script semantics are accepted: interpreter
    options before a script path, and the script directory that CPython puts
    at ``sys.path[0]``. ``-c`` and ``-m`` invocations have no console-script
    path to derive, and command substitution/options are left unresolved.
    """
    interpreter = _resolve_program(tokens[0], environment, cwd)
    if interpreter is None or not Path(interpreter).is_file():
        return None
    flags: list[str] = []
    script: Path | None = None
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            if index + 1 < len(tokens):
                script = Path(tokens[index + 1]).expanduser()
            break
        if token == "-c":
            return None
        if token == "-m":
            # ``-m`` puts the child cwd at sys.path[0]; the module itself is
            # not resolved here, so only that demonstrable semantics is used.
            return interpreter, tuple(flags), Path(cwd)
        if token.startswith("-"):
            flags.append(token)
            index += 1
            continue
        candidate = Path(token).expanduser()
        if candidate == wrapper or candidate.is_file() or token.endswith(".py"):
            script = candidate
            break
        # A separate value for a preceding interpreter option (for example
        # ``-X pycache_prefix=...``) is still part of the probe flags.
        flags.append(token)
        index += 1
    if script is None:
        script = default_script
    script_dir = None if script is None else Path(os.path.abspath(script)).parent
    return interpreter, tuple(flags), script_dir


def _resolve_launcher_chain(binary: Path, environment: dict[str, str] | None = None,
                            *, cwd: Path | str | None = None,
                            _depth: int = 0, _seen: set[str] | None = None,
                            _chain: list[dict[str, Any]] | None = None
                            ) -> dict[str, Any]:
    """Follow a native launcher's own shebang/symlink/env/wrapper exec chain.

    The resolved result is probe information only; it never supplies the real
    writable-stage command. ``status="unresolved"`` is an expected limitation,
    not an access failure. The final interpreter path is preserved exactly as
    invoked (not replaced by its ``realpath``) so virtual-environment semantics
    remain observable.
    """
    base_environment = dict(os.environ if environment is None else environment)
    cwd_path = Path(cwd if cwd is not None else os.getcwd()).expanduser()
    seen = set() if _seen is None else _seen
    chain = [] if _chain is None else _chain
    path = Path(binary).expanduser()
    if not path.is_absolute():
        path = (cwd_path / path).resolve()

    def unresolved(reason: str, *, env: dict[str, str] | None = None,
                   interpreter: str | None = None, flags: tuple[str, ...] = (),
                   script_dir: Path | None = None) -> dict[str, Any]:
        return {
            "status": "unresolved", "reason": reason,
            "interpreter": interpreter, "flags": list(flags),
            "script_dir": None if script_dir is None else str(script_dir),
            "environment": dict(base_environment if env is None else env),
            "environment_updates": {}, "chain": list(chain), "launcher": str(path),
        }

    def resolved(interpreter: str, flags: tuple[str, ...], script_dir: Path | None,
                 env: dict[str, str]) -> dict[str, Any]:
        return {
            "status": "resolved", "reason": None,
            "interpreter": interpreter, "flags": list(flags),
            "script_dir": None if script_dir is None else str(script_dir),
            "environment": dict(env),
            "environment_updates": {
                key: value for key, value in env.items()
                if base_environment.get(key) != value
            },
            "chain": list(chain), "launcher": str(path),
        }

    if _depth > 12:
        return unresolved("launcher resolution exceeded its depth limit")
    key = str(path)
    if key in seen:
        return unresolved("launcher chain is cyclic: " + key)
    seen.add(key)
    try:
        info = path.lstat()
    except OSError as error:
        chain.append({"path": key, "error": type(error).__name__, "detail": str(error)})
        return unresolved("launcher could not be inspected: " + (str(error) or type(error).__name__))
    kind = ("symlink" if stat.S_ISLNK(info.st_mode) else
            "file" if stat.S_ISREG(info.st_mode) else "other")
    row: dict[str, Any] = {"path": key, "kind": kind}
    try:
        row["mode"] = format(stat.S_IMODE(info.st_mode), "04o")
        row["uid"], row["gid"] = info.st_uid, info.st_gid
    except OSError:
        pass
    chain.append(row)
    resolved_path = Path(os.path.realpath(path))
    if str(resolved_path) != key:
        row["target"] = str(resolved_path)
    try:
        data = resolved_path.read_bytes()[:8192]
    except OSError as error:
        return unresolved("launcher could not be read: " + (str(error) or type(error).__name__))

    # A bare interpreter launcher (for example a symlink to python) is usable.
    if not data.startswith(b"#!") and _is_python_executable(resolved_path):
        if os.access(path, os.X_OK):
            return resolved(str(path), (), path.parent, base_environment)
        return unresolved("Python launcher is not executable", interpreter=str(path))

    if not data.startswith(b"#!"):
        return unresolved("launcher has no supported shebang or is an opaque executable")

    try:
        shebang = shlex.split(data.splitlines()[0][2:].decode("utf-8", "replace").strip())
    except (ValueError, IndexError, UnicodeError):
        return unresolved("launcher shebang is malformed")
    if not shebang:
        return unresolved("launcher shebang is empty")
    interpreter = shebang[0]
    environment_for_chain = dict(base_environment)
    if Path(interpreter).name == "env":
        consumed = _consume_env_command(shebang, environment_for_chain, path)
        if consumed is None:
            return unresolved("env shebang names no interpreter", env=environment_for_chain)
        environment_for_chain, command = consumed
        if not command:
            return unresolved("env shebang names no interpreter", env=environment_for_chain)
        interpreter, arguments = command[0], command[1:]
    else:
        arguments = shebang[1:]

    interpreter_path = _resolve_program(interpreter, environment_for_chain, cwd_path)
    if interpreter_path is None:
        return unresolved("launcher interpreter was not found: " + interpreter,
                          env=environment_for_chain)
    if _is_shell_executable(interpreter_path):
        return _resolve_shell_wrapper(
            path, resolved_path, environment_for_chain, cwd_path,
            _depth=_depth, _seen=seen, _chain=chain,
        )
    if _is_python_executable(interpreter_path):
        if not Path(interpreter_path).exists():
            return unresolved("Python launcher interpreter does not exist: " + interpreter_path,
                              interpreter=interpreter_path, env=environment_for_chain)
        # The shebang's optional arguments belong to the Python probe. The
        # console script itself is the script whose directory is sys.path[0].
        details = _python_probe_details(
            [interpreter_path, *arguments], path, environment_for_chain, cwd_path,
            default_script=path,
        )
        if details is None:
            return unresolved("Python launcher arguments are not probeable",
                              interpreter=interpreter_path, env=environment_for_chain)
        final_interpreter, flags, script_dir = details
        return resolved(final_interpreter, flags, script_dir, environment_for_chain)
    # A shebang may name another wrapper script rather than an interpreter.
    if Path(interpreter_path).is_file():
        return _resolve_launcher_chain(
            Path(interpreter_path), environment_for_chain, cwd=cwd_path,
            _depth=_depth + 1, _seen=seen, _chain=chain,
        )
    return unresolved("launcher interpreter is not a supported executable: " + interpreter_path,
                      env=environment_for_chain)


def _resolve_shell_wrapper(path: Path, resolved_path: Path, environment: dict[str, str],
                           cwd: Path, *, _depth: int, _seen: set[str],
                           _chain: list[dict[str, Any]]) -> dict[str, Any]:
    """Parse a shell launcher's assignments and its supported ``exec`` chain."""
    try:
        text = resolved_path.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        return {
            "status": "unresolved", "reason": "shell wrapper could not be read: " + (
                str(error) or type(error).__name__),
            "interpreter": None, "flags": [], "script_dir": None,
            "environment": dict(environment), "environment_updates": {},
            "chain": list(_chain), "launcher": str(path),
        }
    env = dict(environment)
    # A POSIX shell exports the cwd as PWD before executing the wrapper body.
    if Path(cwd).is_absolute():
        env["PWD"] = str(cwd)
    for line in text.splitlines()[1:]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            tokens = _shell_tokens(stripped, env, path)
        except (ValueError, IndexError):
            return {
                "status": "unresolved", "reason": "shell wrapper uses unsupported expansion: " + stripped,
                "interpreter": None, "flags": [], "script_dir": None,
                "environment": env, "environment_updates": {},
                "chain": list(_chain), "launcher": str(path),
            }
        if not tokens:
            continue
        if tokens[0] in {"export", "local", "readonly"}:
            for token in tokens[1:]:
                _apply_assignment(token, env, path)
            continue
        if tokens[0] == "unset":
            for name in tokens[1:]:
                env.pop(name, None)
            continue
        if tokens[0] in {".", "source"}:
            return {
                "status": "unresolved", "reason": "shell wrapper sources another file",
                "interpreter": None, "flags": [], "script_dir": None,
                "environment": env, "environment_updates": {},
                "chain": list(_chain), "launcher": str(path),
            }
        assignments = 0
        for token in tokens:
            if _apply_assignment(token, env, path):
                assignments += 1
            else:
                break
        if assignments:
            if assignments == len(tokens):
                continue
            tokens = tokens[assignments:]
        if tokens[0] == "exec":
            tokens = tokens[1:]
            if not tokens:
                break
            if tokens[0] in {"-a", "-c"}:
                tokens = tokens[2:]
            elif tokens[0].startswith("-"):
                tokens = tokens[1:]
            if not tokens:
                break
            if Path(tokens[0]).name == "env":
                consumed = _consume_env_command(tokens, env, path)
                if consumed is None:
                    break
                env, tokens = consumed
            command = _resolve_program(tokens[0], env, cwd)
            if command is None:
                return {
                    "status": "unresolved", "reason": "wrapper exec target was not found: " + tokens[0],
                    "interpreter": None, "flags": [], "script_dir": None,
                    "environment": env, "environment_updates": {},
                    "chain": list(_chain), "launcher": str(path),
                }
            if _is_python_executable(command):
                details = _python_probe_details(tokens, path, env, cwd)
                if details is None:
                    return {
                        "status": "unresolved", "reason": "wrapper Python exec is not probeable",
                        "interpreter": command, "flags": [], "script_dir": None,
                        "environment": env, "environment_updates": {},
                        "chain": list(_chain), "launcher": str(path),
                    }
                final_interpreter, flags, script_dir = details
                return {
                    "status": "resolved", "reason": None,
                    "interpreter": final_interpreter, "flags": list(flags),
                    "script_dir": None if script_dir is None else str(script_dir),
                    "environment": dict(env),
                    "environment_updates": {
                        key: value for key, value in env.items()
                        if environment.get(key) != value
                    },
                    "chain": list(_chain), "launcher": str(path),
                }
            if Path(command).is_file():
                result = _resolve_launcher_chain(
                    Path(command), env, cwd=cwd,
                    _depth=_depth + 1, _seen=_seen, _chain=_chain,
                )
                return result
            return {
                "status": "unresolved", "reason": "wrapper exec target is not a supported launcher: " + command,
                "interpreter": None, "flags": [], "script_dir": None,
                "environment": env, "environment_updates": {},
                "chain": list(_chain), "launcher": str(path),
            }
        # An unqualified command that is not exec-based may still be the final
        # line of a wrapper; do not guess about arbitrary shell behavior.
        break
    return {
        "status": "unresolved", "reason": "shell wrapper has no supported exec chain",
        "interpreter": None, "flags": [], "script_dir": None,
        "environment": env, "environment_updates": {},
        "chain": list(_chain), "launcher": str(path),
    }


def _resolve_native_launcher_chain(binary: Path | str,
                                   environment: dict[str, str] | None = None,
                                   cwd: Path | str | None = None) -> dict[str, Any]:
    """Module-level entry point for launcher chain resolution."""
    return _resolve_launcher_chain(Path(binary), environment, cwd=cwd)


@dataclass(frozen=True)
class _NativeStartup:
    """The real native command, kept separate from optional probe information.

    ``command()`` is byte-for-byte the native stage command from the baseline:
    read-only stages invoke the pinned companion through the validated Python
    interpreter, while every writable stage starts exactly ``self.binary``.
    ``probe()`` is the interpreter the probe actually runs: read-only stages
    always probe the same interpreter/flags as their real command, while a
    writable stage probes only when the launcher's own exec chain resolved to a
    demonstrable Python interpreter/flags/script directory. A missing probe is
    a recorded limitation and never changes ``command()``.
    """

    interpreter: str | None
    flags: tuple[str, ...]
    launcher: str | None
    native_executable: str | None
    read_only: bool
    real_command: tuple[str, ...] = ()
    probe_environment: dict[str, str] = field(default_factory=dict)
    script_dir: str | None = None
    resolution: dict[str, Any] = field(default_factory=dict)
    resolution_reason: str | None = None
    chain: list[dict[str, Any]] = field(default_factory=list)

    def probe(self) -> list[str] | None:
        if self.interpreter is None:
            return None
        return [self.interpreter, *self.flags]

    def command(self) -> list[str]:
        if self.real_command:
            return list(self.real_command)
        raise AssertionError("native startup has no real command")

    def launched(self) -> str:
        """The launcher actually named by the writable child command."""
        if self.native_executable is not None:
            return self.native_executable
        if self.launcher is not None:
            return self.launcher
        return "jiuwenswarm.long_horizon.native_entry"

    def environment(self) -> dict[str, str]:
        return dict(self.probe_environment)

    def resolved(self) -> bool:
        return self.interpreter is not None



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


def _credential_free(value: Any, path: tuple[object, ...] = ()) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            item_path = (*path, key)
            if not _credential_free(item, item_path):
                return False
            if _credential_key(str(key)) and item not in (None, ""):
                if not isinstance(item, str) or not (
                    _ENV_REFERENCE.fullmatch(item) or _credential_path(str(key), item)
                    or (item_path == _BROWSER_KEY_FIELD and item in _BROWSER_KEY_NAMES)
                ):
                    return False
    elif isinstance(value, list):
        return all(_credential_free(item, (*path, index)) for index, item in enumerate(value))
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
        "native_transport_retry_sha256": _sha256(Path(__file__).with_name("native_transport_retry.py")),
        "native_tool_free_sha256": _sha256(Path(__file__).with_name("native_tool_free.py")),
        "memgraph_reader_sha256": _sha256(Path(__file__).with_name("memgraph.py")),
        "memgraph_mcp_sha256": _sha256(Path(__file__).with_name("memgraph_mcp.py")),
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
CAPACITY_SCALE_ENV = "LONG_HORIZON_CAPACITY_SCALE"
CAPACITY_PROFILE_SCHEMA = "long-horizon/capacity-profile/v1"
STREAM_SPOOL_ENV = "JIUWENSWARM_STREAM_SPOOL_BYTES"
DEFAULT_STREAM_SPOOL_BYTES = 8 * 1024 * 1024


def _capacity_scale() -> int:
    """Read the same opt-in profile setting as the controller package.

    The vendored archive cannot import the controller package, so this tiny
    parser is intentionally duplicated.  Missing is the historical default;
    malformed is a configuration error rather than a silent study mismatch.
    """
    raw = os.environ.get(CAPACITY_SCALE_ENV, "").strip()
    if not raw:
        return 1
    try:
        value = int(raw, 10)
    except (TypeError, ValueError):
        raise ValueError(f"{CAPACITY_SCALE_ENV} must be a positive integer") from None
    if isinstance(value, bool) or not 1 <= value <= 10_000:
        raise ValueError(f"{CAPACITY_SCALE_ENV} must be an integer from 1 to 10000")
    return value


def _scaled(value: int) -> int:
    return max(1, int(value) * _capacity_scale())


def _capacity_receipt() -> dict:
    return {
        "schema": CAPACITY_PROFILE_SCHEMA,
        "setting": CAPACITY_SCALE_ENV,
        "scale": _capacity_scale(),
        "default_scale": 1,
        "experiment_scale": 100,
    }


def retained_payload_string_limit() -> int:
    return _scaled(4096)


def stream_spool_threshold() -> int:
    raw = os.environ.get(STREAM_SPOOL_ENV, "").strip()
    try:
        value = int(raw, 10) if raw else DEFAULT_STREAM_SPOOL_BYTES
    except (TypeError, ValueError):
        value = DEFAULT_STREAM_SPOOL_BYTES
    return max(1024, value)


RETAINED_PAYLOAD_STRING_LIMIT = retained_payload_string_limit()
# Total in-memory event payload budget, independent of event count. Complete
# records remain durable in events.jsonl; the stream summary keeps the final
# response and usage needed by role turns even after older events are dropped.
RETAINED_EVENT_TOTAL_BYTES = _scaled(8 * 1024 * 1024)
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
# The launcher owns the notice identity. These names are deliberately a small,
# stable contract: a JSON context object is preferred, while individual values
# remain readable for older callers. Only the active invocation is recorded.
NOTICE_CONTEXT_ENV = ("LH_NOTICE_CONTEXT", "GB_LH_NOTICE_CONTEXT", "JIUWENSWARM_NOTICE_CONTEXT")
NOTICE_VALUE_ENV = (
    "LH_NOTICE_ID", "GB_LH_NOTICE_ID", "LH_NOTICE_TOKEN", "GB_LH_NOTICE_TOKEN",
    "LH_INVOCATION_TOKEN", "GB_LH_INVOCATION_TOKEN", "LH_INVOCATION_ID",
    "GB_LH_INVOCATION_ID", "JIUWENSWARM_INVOCATION_ID",
)
STAGE_STATE_DIRECTORIES = (
    "config", ".trace/sessions", "logs", "openjiuwen-logs", "startup",
    "context-debug", "tasks", "task-registry", "memory", "pycache", "tmp",
    "stage-writes",
)
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


def _bounded_payload(value: Any, limit: int | None = None) -> Any:
    limit = retained_payload_string_limit() if limit is None else max(1, int(limit))
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
    limit = retained_payload_string_limit()
    return {key: (_bounded_payload(item, limit) if key == "payload" else item) for key, item in record.items()}


def _record_bytes(record: dict) -> int:
    # Conservative default serialization: retained compact bytes stay below
    # the budget even for callers that re-serialize retained events.
    return len(json.dumps(record, ensure_ascii=False, allow_nan=False).encode("utf-8"))


class _RetainedEventStream:
    """Bounded in-memory events plus a streaming delta/usage summary.

    At the historical default the event copies and the delta text each keep
    their original 8 MiB bound.  At an opt-in capacity profile they share one
    scaled total, so a role cannot retain twice the documented study bound.
    The caller derives that shared total from cgroup headroom when it is
    smaller than the configured study capacity.
    """

    def __init__(self, limit: int | None = None, *, compact: bool = False) -> None:
        limit = _scaled(8 * 1024 * 1024) if limit is None else limit
        self.limit = max(1024, int(limit))
        if _capacity_scale() == 1:
            # Byte-for-byte historical behaviour: independent 8 MiB bounds.
            self.event_limit = self.limit
            self.text_limit = self.limit
        else:
            self.event_limit = max(1024, self.limit - self.limit // 2)
            self.text_limit = max(0, self.limit - self.event_limit)
        self.compact = compact
        self.records: list[dict] = []
        self.sizes: list[int] = []
        self.positions: list[tuple[int, int] | None] = []
        self.total_bytes = 0
        self.dropped = 0
        self.final_event_dropped = False
        self.final_event_dropped_sequence: int | None = None
        self.final_event_dropped_offset: int | None = None
        self.final_event_dropped_length: int | None = None
        # The latest activity that makes an earlier final non-authoritative.
        # Tracking it here survives eviction of the activity itself.
        self.last_invalidating_sequence: int | None = None
        self.text_parts: list[str] = []
        self.text_bytes = 0
        self.text_truncated = False
        self.usage: dict = {}
        self.has_usage_summary = False
        self.session = ""

    def _remember_text(self, value: str) -> None:
        encoded = value.encode("utf-8")
        remaining = self.text_limit - self.text_bytes
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

    def _mark_final_dropped(
        self, record: dict, position: tuple[int, int] | None = None
    ) -> None:
        self.final_event_dropped = True
        sequence = record.get("sequence")
        if type(sequence) is not int or self.final_event_dropped_sequence is None or (
                sequence >= self.final_event_dropped_sequence):
            if type(sequence) is int:
                self.final_event_dropped_sequence = sequence
            self.final_event_dropped_offset = position[0] if position is not None else None
            self.final_event_dropped_length = position[1] if position is not None else None

    def feed(
        self,
        record: dict,
        *,
        event_log_offset: int | None = None,
        event_log_length: int | None = None,
    ) -> None:
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
        sequence = record.get("sequence")
        if type(sequence) is int and _invalidates_final(record):
            if (self.last_invalidating_sequence is None
                    or sequence > self.last_invalidating_sequence):
                self.last_invalidating_sequence = sequence
        position = (
            (event_log_offset, event_log_length)
            if type(event_log_offset) is int and type(event_log_length) is int
            and event_log_offset >= 0 and event_log_length > 0
            else None
        )
        candidate = retained_record(record) if self.compact else record
        size = _record_bytes(candidate)
        if size > self.event_limit:
            if kind == "chat.final":
                # Never let a truncated final message pass as the authoritative
                # completed response. The exact safe record remains durable in
                # events.jsonl and its position is passed to the final reader.
                self._mark_final_dropped(record, position)
                self.dropped += 1
                return
            # One pathological record still cannot own more than the whole
            # budget. Keep its correlation keys and a bounded payload.
            candidate = _bounded_payload(candidate, max(1024, self.event_limit // 8))
            size = _record_bytes(candidate)
            if size > self.event_limit:
                self.dropped += 1
                return
        while self.records and self.total_bytes + size > self.event_limit:
            evicted = self.records.pop(0)
            position = self.positions.pop(0) if self.positions else None
            self.total_bytes -= self.sizes.pop(0)
            self.dropped += 1
            if evicted.get("event_type") == "chat.final":
                self._mark_final_dropped(evicted, position)
        self.records.append(candidate)
        self.sizes.append(size)
        self.positions.append(
            (event_log_offset, event_log_length)
            if type(event_log_offset) is int and type(event_log_length) is int
            and event_log_offset >= 0 and event_log_length > 0
            else None
        )
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


class _SpooledRecord:
    """One complete native JSONL record retained on disk, never truncated."""

    __slots__ = ("path", "sha256", "size")

    def __init__(self, path: Path, sha256: str, size: int) -> None:
        self.path, self.sha256, self.size = path, sha256, size


# ---------------------------------------------------------------------------
# Actual Process CLI JSONL chunk transport
# ---------------------------------------------------------------------------
# The Process CLI emits a logical record above 8 MiB as bounded
# ``record_chunk`` frames.  Those frames are transport, not native events: the
# consumer verifies and reassembles the complete logical record before the
# ordinary request/sequence/session checks in ``handle_record``.  The wire
# contract is the SDK's ``protocol.py`` / ``protocol/jsonl.py``: each frame
# carries a stream identity, a contiguous index, UTF-8 text, and a final
# sha256/record_bytes pair over the exact logical bytes.
#
# When the installed SDK exposes that transport reader, its strict JSON hooks
# and ProtocolError are reused.  An older SDK without chunk support leaves the
# reader on the identical private decoder, and unframed records keep their
# historical path unchanged.
# ---------------------------------------------------------------------------

CHUNK_TYPE = "record_chunk"
CHUNK_VERSION = 1
CHUNK_FRAME_BYTES = 512 * 1024
_SCHEMA_VERSION = "0.1"
_CHUNK_SPOOL_BYTES = 8 * 1024 * 1024
_CHUNK_MAX_STREAMS = 64
_NATIVE_PROTOCOL: Any = None
_NATIVE_PROTOCOL_CHECKED = False


class ChunkStreamError(RuntimeError):
    """Malformed, incomplete or mismatched ``record_chunk`` transport."""


def _local_chunk_pairs(pairs: list[tuple[str, Any]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ChunkStreamError("duplicate output key")
        result[key] = value
    return result


def _local_chunk_constant(_value: str) -> None:
    raise ChunkStreamError("non-finite output number")


def _local_chunk_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ChunkStreamError("non-finite output number")
    return result


def _native_protocol() -> Any:
    """Return an installed SDK protocol module that understands chunks.

    Import is intentionally lazy and optional: the worker must run with the
    older SDK shipped in some images and with no SDK at all in the archived
    test harness.  A module without the exact chunk constants and strict JSON
    hooks is ignored rather than partially trusted.

    ``Records.accept`` itself is not the worker's reader: it requires a
    trailing newline and writes reassembly spools under ``TMPDIR``.  The
    worker's line reader legitimately yields a final unterminated line at EOF
    and must keep raw bytes in the stage's ``stdout-spool`` directory.  Reusing
    the SDK's strict hooks and ``ProtocolError`` preserves the exact wire
    decoding and error text without changing those two contracts.
    """
    global _NATIVE_PROTOCOL, _NATIVE_PROTOCOL_CHECKED
    if not _NATIVE_PROTOCOL_CHECKED:
        _NATIVE_PROTOCOL_CHECKED = True
        try:
            from jiuwenswarm_sdk import protocol as native
        except Exception:
            native = None
        if native is not None and all(
            hasattr(native, name)
            for name in (
                "ProtocolError",
                "CHUNK_TYPE",
                "CHUNK_VERSION",
                "SCHEMA_VERSION",
                "_pairs",
                "_constant",
                "_float",
            )
        ):
            _NATIVE_PROTOCOL = native
    return _NATIVE_PROTOCOL


def _chunk_protocol_hooks() -> tuple:
    native = _native_protocol()
    if native is None:
        return (
            ChunkStreamError,
            _local_chunk_pairs,
            _local_chunk_constant,
            _local_chunk_float,
            _SCHEMA_VERSION,
            CHUNK_VERSION,
        )
    return (
        native.ProtocolError,
        native._pairs,
        native._constant,
        native._float,
        getattr(native, "SCHEMA_VERSION", _SCHEMA_VERSION),
        getattr(native, "CHUNK_VERSION", CHUNK_VERSION),
    )


def _looks_like_chunk(raw: bytes) -> bool:
    """Whether an undecodable physical line claims the chunk wire prefix."""
    return raw.lstrip().startswith(b'{"type":"record_chunk"')


def _chunk_frame_note(frame: dict, raw_length: int) -> str:
    """Describe one transport frame without publishing its payload.

    Frame data is model-controlled and may contain a credential that straddles
    two frames, so neither the data nor its digest is written to a durable log.
    The logical record is published separately after the stream digest and
    length have been verified.
    """
    index = frame.get("chunk_index")
    final = frame.get("final")
    index_text = str(index) if type(index) is int else "unknown"
    final_text = "true" if final is True else "false" if final is False else "unknown"
    return (
        f"[record_chunk frame index={index_text} final={final_text} bytes={raw_length}; "
        "transport frame not retained]\n"
    )


def _undecodable_chunk_note(raw_length: int) -> str:
    return (
        f"[record_chunk frame undecodable bytes={raw_length}; "
        "no logical record retained]\n"
    )


def _undecodable_record_note(raw_length: int) -> str:
    return f"[undecodable record bytes={raw_length}; no content retained]\n"


def _rejected_record_note(error: BaseException) -> str:
    return f"[record rejected ({type(error).__name__}); no content retained]\n"


def _protocol_error_fields(error: BaseException) -> dict:
    """Exact native message plus its exception class for stage metadata."""
    return {
        "protocol_error_detail": str(error)[:2000],
        "protocol_error_exception": type(error).__name__,
    }


def _strict_chunk_json(text: str, error_type, pairs, constant, float_hook) -> dict:
    try:
        record = json.loads(
            text,
            object_pairs_hook=pairs,
            parse_constant=constant,
            parse_float=float_hook,
        )
    except (ValueError, RecursionError) as error:
        raise error_type("invalid UTF-8 JSON record") from error
    if not isinstance(record, dict):
        raise error_type("output record must be an object")
    return record


class _RecordChunkReader:
    """Incrementally reassemble the actual ``record_chunk`` wire.

    Each logical stream spools to a bounded in-memory buffer that rolls over to
    an anonymous file in the stage's ``stdout-spool`` directory.  No frame is
    interpreted as a business event, and no logical record is returned until
    the final frame's byte length and sha256 both match the complete UTF-8
    payload.
    """

    def __init__(
        self,
        request_id: str,
        spool_dir: Path,
        *,
        max_memory: int = _CHUNK_SPOOL_BYTES,
        max_streams: int = _CHUNK_MAX_STREAMS,
    ) -> None:
        self.request_id = request_id
        self.spool_dir = spool_dir
        self.max_memory = max(1024, int(max_memory))
        self.max_streams = max(1, int(max_streams))
        self.streams: dict[str, dict] = {}
        self.completed = 0
        self.last_receipt: dict | None = None

    @property
    def reader_name(self) -> str:
        return "jiuwenswarm_sdk.protocol" if _native_protocol() is not None else "private-wire-v1"

    def _discard(self, stream_id: str) -> None:
        state = self.streams.pop(stream_id, None)
        if state is not None:
            try:
                state["spool"].close()
            except OSError:
                pass

    def accept(self, frame: dict) -> dict | None:
        """Consume one frame; return the verified logical record on ``final``."""
        error_type, pairs, constant, float_hook, schema_version, chunk_version = (
            _chunk_protocol_hooks()
        )
        if (
            frame.get("chunk_version") != chunk_version
            or frame.get("schema_version") != schema_version
            or frame.get("request_id") != self.request_id
        ):
            raise error_type("output chunk version/request identity mismatch")
        stream_id = frame.get("stream_id")
        index = frame.get("chunk_index")
        data = frame.get("data")
        final = frame.get("final")
        if not isinstance(stream_id, str) or not stream_id:
            raise error_type("output chunk has no stream identity")
        if type(index) is not int or index < 0:
            raise error_type("output chunk index is invalid")
        if not isinstance(data, str):
            raise error_type("output chunk data is not text")
        if not isinstance(final, bool):
            raise error_type("output chunk final flag is invalid")
        state = self.streams.get(stream_id)
        if state is None:
            if index != 0:
                raise error_type("output chunk stream does not start at zero")
            if len(self.streams) >= self.max_streams:
                raise error_type("too many concurrent output chunk streams")
            try:
                self.spool_dir.mkdir(parents=True, exist_ok=True)
                os.chmod(self.spool_dir, 0o700)
                spool = tempfile.SpooledTemporaryFile(
                    max_size=self.max_memory,
                    dir=str(self.spool_dir),
                    prefix="chunk-",
                    suffix=".jsonl",
                )
            except OSError as error:
                raise error_type("output chunk spool is unavailable") from error
            state = {
                "next": 0,
                "digest": hashlib.sha256(),
                "bytes": 0,
                "spool": spool,
            }
            self.streams[stream_id] = state
        if index != state["next"]:
            self._discard(stream_id)
            raise error_type("non-contiguous output chunk sequence")
        raw = data.encode("utf-8")
        try:
            state["spool"].write(raw)
        except OSError as error:
            self._discard(stream_id)
            raise error_type("output chunk spool write failed") from error
        state["digest"].update(raw)
        state["bytes"] += len(raw)
        state["next"] += 1
        if not final:
            return None
        expected = frame.get("sha256")
        expected_bytes = frame.get("record_bytes")
        actual = state["digest"].hexdigest()
        if (
            expected != actual
            or type(expected_bytes) is not int
            or expected_bytes != state["bytes"]
        ):
            self._discard(stream_id)
            raise error_type("output chunk digest/length mismatch")
        receipt = {
            "stream_id": stream_id,
            "chunk_count": state["next"],
            "bytes": state["bytes"],
            "sha256": actual,
            "request_id": self.request_id,
        }
        try:
            state["spool"].seek(0)
            raw_record = state["spool"].read()
        except OSError as error:
            raise error_type("output chunk spool read failed") from error
        finally:
            self._discard(stream_id)
        try:
            text = raw_record.decode("utf-8")
        except UnicodeDecodeError as error:
            raise error_type("invalid UTF-8 JSON record") from error
        record = _strict_chunk_json(text, error_type, pairs, constant, float_hook)
        self.completed += 1
        self.last_receipt = receipt
        return record

    def close(self) -> None:
        for stream_id in list(self.streams):
            self._discard(stream_id)


async def _close_async_iterator(iterator: Any) -> None:
    """Run an async generator's cleanup even when the caller is cancelled."""
    close = asyncio.ensure_future(iterator.aclose())
    cancelled = False
    while not close.done():
        try:
            await asyncio.shield(close)
        except asyncio.CancelledError:
            cancelled = True
    close.result()
    if cancelled:
        raise asyncio.CancelledError


async def _iter_stream_lines(
    stream: Any, *, spool_dir: Path, threshold: int, first_index: int = 0
):
    """Yield complete byte lines, spooling an over-threshold line instead of failing.

    ``StreamReader.__anext__`` raises when one line exceeds the transport
    limit.  Reading explicit chunks and spilling to a private file keeps the
    process usable for a valid oversized record.  The caller receives either
    bytes or a spool receipt with the real digest and length.

    A spool file is a private intermediate, never a published record.  It is
    removed by this generator's ``finally`` unless the receipt has been yielded
    to the caller; the caller then owns the commit-or-remove decision.  Closing
    the generator on cancellation or an I/O failure therefore cannot leave a
    raw prefix behind.
    """
    buffer = bytearray()
    spool = None
    digest = None
    size = 0
    path: Path | None = None
    index = first_index

    def discard_spool() -> None:
        nonlocal spool, digest, size, path
        if spool is not None:
            try:
                spool.close()
            except OSError:
                pass
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        spool = None
        digest = None
        size = 0
        path = None

    def begin_spool() -> bool:
        nonlocal spool, digest, size, path
        candidate = spool_dir / f"record-{index:06d}.jsonl"
        handle = None
        try:
            spool_dir.mkdir(parents=True, exist_ok=True)
            os.chmod(spool_dir, 0o700)
            handle = candidate.open("wb")
            os.chmod(candidate, 0o600)
        except OSError:
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass
            try:
                candidate.unlink(missing_ok=True)
            except OSError:
                pass
            return False
        spool, path = handle, candidate
        digest, size = hashlib.sha256(), 0
        return True

    def finish_spool() -> _SpooledRecord:
        nonlocal spool, digest, size, path
        assert spool is not None and path is not None and digest is not None
        spool.close()
        record = _SpooledRecord(path, digest.hexdigest(), size)
        # Ownership moves to the caller; this generator must not unlink a
        # receipt it has already handed out.
        spool = None
        digest = None
        size = 0
        path = None
        return record

    try:
        while True:
            chunk = await stream.read(256 * 1024)
            if not chunk:
                break
            buffer.extend(chunk)
            while True:
                newline = buffer.find(b"\n")
                if newline < 0:
                    if spool is None and len(buffer) > threshold:
                        if begin_spool():
                            try:
                                spool.write(buffer)
                            except OSError:
                                # Keep the bytes in memory so a later attempt
                                # can still spool or yield the same record.
                                discard_spool()
                            else:
                                digest.update(buffer)
                                size = len(buffer)
                                buffer.clear()
                    elif spool is not None:
                        try:
                            spool.write(buffer)
                        except OSError:
                            discard_spool()
                        else:
                            digest.update(buffer)
                            size += len(buffer)
                            buffer.clear()
                    break
                line = bytes(buffer[: newline + 1])
                del buffer[: newline + 1]
                if spool is not None:
                    try:
                        spool.write(line)
                        digest.update(line)
                        size += len(line)
                        record = finish_spool()
                    except OSError:
                        discard_spool()
                        yield line
                        continue
                    yield record
                    index += 1
                elif len(line) > threshold:
                    if begin_spool():
                        try:
                            spool.write(line)
                            digest.update(line)
                            size = len(line)
                            record = finish_spool()
                        except OSError:
                            discard_spool()
                            yield line
                            continue
                        yield record
                        index += 1
                    else:
                        yield line
                else:
                    yield line
        if spool is not None:
            if buffer:
                try:
                    spool.write(buffer)
                    digest.update(buffer)
                    size += len(buffer)
                except OSError:
                    discard_spool()
                    raise
                buffer.clear()
            try:
                record = finish_spool()
            except OSError:
                discard_spool()
                raise
            yield record
        elif buffer:
            yield bytes(buffer)
    finally:
        discard_spool()


def _read_logged_event(
    event_log: Path | str | None, offset: int | None, length: int | None
) -> dict | None:
    """Read exactly the safe JSONL record written at a recorded position."""
    if event_log is None or type(offset) is not int or type(length) is not int:
        return None
    if offset < 0 or length <= 0:
        return None
    try:
        path = Path(event_log)
        if path.is_symlink() or not path.is_file():
            return None
        with path.open("rb") as stream:
            stream.seek(offset)
            raw = stream.read(length)
        if len(raw) != length or not raw.endswith(b"\n"):
            return None
        record = json.loads(raw.decode("utf-8"))
        if not isinstance(record, dict):
            return None
        # Consume the recovered line with the same official record class that
        # accepted it while streaming; a stale or foreign offset must not be
        # promoted into the final response.
        OneShotEvent.from_dict(record)
    except (OSError, UnicodeError, ValueError, TypeError, RuntimeError):
        return None
    return record


def _native_final_message(record: dict) -> tuple[str, dict] | None:
    """Return one complete native ``chat.final`` message and its provenance."""
    if record.get("type") != "event" or record.get("event_type") != "chat.final":
        return None
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    mode = payload.get("final_mode")
    content = payload.get("content")
    if (mode not in {"patch_segment", "replace_turn", "append"}
            or not isinstance(content, str) or not content.strip()
            or payload.get("output_suppressed")):
        return None
    return content, {
        "source": "native_chat_final", "native_sequence": record.get("sequence"),
        "final_mode": mode, "output_phase_id": payload.get("output_phase_id"),
        "execution_id": payload.get("execution_id"),
    }


def _invalidates_final(record: dict) -> bool:
    """Whether one event makes an earlier final non-authoritative."""
    kind = record.get("event_type")
    payload = record.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    if kind in {
        "chat.tool_call", "chat.tool_update", "chat.tool_result",
        "chat.output_phase", "chat.input_received",
    }:
        return True
    return kind in {"chat.delta", "chat.reasoning"} and any(
        isinstance(payload.get(key), str) and payload[key]
        for key in ("content", "delta", "text", "message", "answer"))


def _invalidated_after(
    records: list[dict], sequence: int | None, *,
    invalidating_sequence: int | None = None,
) -> bool:
    """Whether retained activity after one final makes it non-authoritative.

    ``invalidating_sequence`` is the latest invalidating event seen by the
    bounded stream, including events that were subsequently evicted or dropped
    before this check.  It closes the gap where the only later tool/activity is
    no longer present in ``records``.
    """
    if type(sequence) is not int:
        return False
    if type(invalidating_sequence) is int and invalidating_sequence > sequence:
        return True
    for following in records:
        after = following.get("sequence")
        if type(after) is not int or after <= sequence:
            continue
        if _invalidates_final(following):
            return True
    return False


def select_native_final_response(
    records: list[dict], terminal: dict, *, final_event_dropped: bool = False,
    final_event_dropped_sequence: int | None = None,
    event_log: Path | str | None = None,
    final_event_dropped_offset: int | None = None,
    final_event_dropped_length: int | None = None,
    invalidating_sequence: int | None = None,
) -> tuple[str, dict]:
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

    For a completed terminal, a complete final that was too large for the
    bounded in-memory copy is recovered from the exact safe JSONL bytes at the
    recorded event-log position.  The terminal aggregate is only a fallback
    when that record cannot be read or does not match the dropped sequence.
    A non-completed terminal always keeps the terminal aggregate.

    ``invalidating_sequence`` lets the caller preserve later tool/assistant
    activity that the bounded copy already evicted; it is checked before the
    retained records and therefore cannot be hidden by retention.
    """
    aggregate = terminal.get("output") or ""
    provenance = {"source": "native_terminal_aggregate", "native_sequence": terminal.get("sequence")}
    finals = [(index, record) for index, record in enumerate(records)
              if record.get("type") == "event" and record.get("event_type") == "chat.final"]
    if final_event_dropped:
        # A complete chat.final was too large for the bounded in-memory copy.
        # Prefer the exact durable record when it is later than every retained
        # final; a retained message must never stand in for a newer dropped one.
        retained_latest = max(
            (record.get("sequence") for _, record in finals
             if type(record.get("sequence")) is int),
            default=None,
        )
        newer_than_retained = (
            final_event_dropped_sequence is None or retained_latest is None
            or final_event_dropped_sequence > retained_latest
        )
        if newer_than_retained:
            # A non-completed run must not promote a dropped final, exactly as
            # the retained-final path below never promotes one.
            if terminal.get("status") != "completed":
                provenance["final_event_dropped"] = True
                return aggregate, provenance
            dropped = _read_logged_event(
                event_log, final_event_dropped_offset, final_event_dropped_length)
            matches = (
                dropped is not None
                and dropped.get("type") == "event"
                and dropped.get("event_type") == "chat.final"
                and (final_event_dropped_sequence is None
                     or dropped.get("sequence") == final_event_dropped_sequence)
                and not _invalidated_after(
                    records, final_event_dropped_sequence,
                    invalidating_sequence=invalidating_sequence)
            )
            selected = _native_final_message(dropped) if matches else None
            if selected is not None:
                content, result = selected
                result.update({
                    "source": "native_chat_final_event_log",
                    "recovered_from": "events.jsonl",
                    "final_event_dropped": True,
                })
                return content, result
            provenance["final_event_dropped"] = True
            provenance["final_event_recovery"] = (
                "terminal_aggregate" if dropped is None else "event_log_mismatch")
            return aggregate, provenance
    if terminal.get("status") != "completed":
        return aggregate, provenance
    if not finals:
        return aggregate, provenance
    index, final = finals[-1]
    selected = _native_final_message(final)
    if selected is None:
        return aggregate, provenance
    if _invalidated_after(
            records, final.get("sequence"), invalidating_sequence=invalidating_sequence):
        return aggregate, provenance
    return selected


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


def _retained_event_budget(
    stage: str, *, limit_bytes: int | None, reserve_bytes: int | None, cgroup: Path | str | None
) -> tuple[int, dict]:
    """Choose the controller-side retention budget without exceeding cgroup headroom.

    The configured study scale is the ceiling, not a promise that the host can
    hold it.  At default scale this returns the historical 8 MiB.  The actual
    value and its basis are recorded in the stage metadata.
    """
    requested = _scaled(8 * 1024 * 1024)
    if _capacity_scale() == 1:
        # The historical default has fixed independent 8 MiB event and delta
        # bounds.  A small cgroup must not shrink or split them.
        return 8 * 1024 * 1024, {
            "basis": "historical_default",
            "requested_bytes": 8 * 1024 * 1024,
            "degraded": False,
        }
    snapshot = _memory_snapshot(limit_bytes=limit_bytes, reserve_bytes=reserve_bytes, cgroup=cgroup)
    if snapshot is not None and isinstance(snapshot.get("limit_bytes"), int):
        limit = int(snapshot["limit_bytes"])
        reserve = int(snapshot.get("reserve_bytes") or 0)
        headroom = max(0, limit - reserve)
        share = _role_memory_share(stage)
        cap = int(headroom * share)
        actual = min(requested, cap)
        if actual < 1024:
            actual = 1024
        basis = {
            "basis": "min(capacity_scale, cgroup_headroom_share)",
            "requested_bytes": requested,
            "cgroup_limit_bytes": limit,
            "cgroup_reserve_bytes": reserve,
            "role_share": share,
            "cgroup_cap_bytes": cap,
            "degraded": bool(snapshot.get("degraded")),
        }
    else:
        actual, basis = requested, {"basis": "capacity_scale", "requested_bytes": requested, "degraded": True}
    return actual, basis


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
                 native_python: Path | str | None = None,
                 benchmark_write_guard: bool = False) -> None:
        self.binary = Path(binary).expanduser().resolve()
        self.config_template = Path(config_template).expanduser().resolve()
        self.native_python = Path(native_python).expanduser() if native_python is not None else None
        self.source_commit = source_commit
        self.default_timeout_seconds = default_timeout_seconds
        self.memory_limit_bytes = memory_limit_bytes
        self.memory_reserve_bytes = memory_reserve_bytes
        self.memory_cgroup = memory_cgroup
        self.memory_poll_seconds = max(0.05, float(memory_poll_seconds))
        self.benchmark_write_guard = benchmark_write_guard
        self._benchmark_inventory: tuple[Path, tuple[str, ...]] | None = None
        self.benchmark_source_root: Path | None = None
        # Deliberately process-local: no implicit cross-workflow restoration.
        self._sessions: dict[str, _SessionLease] = {}
        self._state_directories: set[Path] = set()

    def _resolve_native_launcher_chain(self, binary: Path | str | None = None,
                                       environment: dict[str, str] | None = None,
                                       state: Path | None = None) -> dict[str, Any]:
        """Resolve this worker's launcher chain for probe information only."""
        target = self.binary if binary is None else Path(binary).expanduser()
        return _resolve_native_launcher_chain(
            target, environment, cwd=state if state is not None else os.getcwd())

    def _native_startup(self, *, state: Path, read_only: bool,
                        environment: dict[str, str] | None = None) -> _NativeStartup:
        """Select the real native command and, separately, probe information.

        The writable command is exactly the baseline launch tuple
        ``[self.binary, "--run-json", "-"]`` regardless of an explicit
        ``native_python``, a reviewer policy interpreter or a controller
        fallback. The read-only command is exactly main 0cb95eb's validated
        Python companion invocation. Optional probe information comes only from
        the launcher's own shebang/symlink/environment/wrapper exec chain; that
        chain never supplies the read-only interpreter or its flags.
        """
        selected_environment = dict(os.environ if environment is None else environment)
        pycache_flags = ("-X", f"pycache_prefix={state / 'pycache'}")
        if read_only:
            # Match main 0cb95eb byte for byte: an explicit native_python wins,
            # otherwise use _native_interpreter(self.binary). Launcher-chain
            # resolution below is retained only as probe/evidence data and is
            # deliberately not allowed to select this interpreter or its flags.
            resolution = self._resolve_native_launcher_chain(
                self.binary, selected_environment, state=state)
            interpreter = (str(self.native_python) if self.native_python is not None
                           else _native_interpreter(self.binary))
            flags = pycache_flags
            command = (interpreter, *flags, "-m",
                       "jiuwenswarm.long_horizon.native_entry", "--run-json", "-")
            # Read-only stages bypass the launcher wrapper, so its environment
            # effects do not describe the real companion invocation. The probe
            # below uses this same interpreter, never the resolved chain's one.
            probe_environment = dict(selected_environment)
            return _NativeStartup(
                interpreter=interpreter, flags=tuple(flags), launcher=None,
                native_executable=None, read_only=True, real_command=command,
                probe_environment=probe_environment,
                script_dir=resolution.get("script_dir"),
                resolution=resolution,
                resolution_reason=resolution.get("reason"),
                chain=list(resolution.get("chain") or []),
            )

        # The binding writable launch invariant: no interpreter substitution.
        command = (str(self.binary), "--run-json", "-")
        resolution = self._resolve_native_launcher_chain(
            self.binary, selected_environment, state=state)
        if resolution.get("status") == "resolved":
            return _NativeStartup(
                interpreter=str(resolution["interpreter"]),
                flags=tuple(resolution.get("flags") or ()),
                launcher=str(self.binary), native_executable=str(self.binary),
                read_only=False, real_command=command,
                probe_environment=dict(resolution.get("environment") or selected_environment),
                script_dir=resolution.get("script_dir"), resolution=resolution,
                chain=list(resolution.get("chain") or []),
            )
        return _NativeStartup(
            interpreter=None, flags=(), launcher=str(self.binary),
            native_executable=str(self.binary), read_only=False,
            real_command=command, probe_environment=dict(selected_environment),
            resolution=resolution, resolution_reason=resolution.get("reason"),
            chain=list(resolution.get("chain") or []),
        )

    def _native_command(self, *, state: Path, read_only: bool,
                        reviewer_launch_policy=None,
                        startup: _NativeStartup | None = None,
                        environment: dict[str, str] | None = None) -> tuple[list[str], object | None]:
        """Build the real native command, independent of probe resolution.

        Writable stages always start ``self.binary`` exactly as on the
        baseline; read-only stages use the validated companion bootstrap. The
        reviewer policy wraps this same tuple without changing argv/env/cwd.
        """
        if startup is None:
            startup = self._native_startup(
                state=state, read_only=read_only, environment=environment)
        command = startup.command()
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
        if self.benchmark_write_guard:
            source = self.benchmark_source_root.resolve()
            if self._benchmark_inventory is None or self._benchmark_inventory[0] != source:
                self._benchmark_inventory = (source, public_test_paths(source))
            paths = self._benchmark_inventory[1]
            return {"schema": "jiuwenswarm-benchmark/workspace-policy/v3",
                    "source_root": str(source), "preserve_existing_tests": True,
                    "automatic_path_rewrite": False, "candidate_filtering": False,
                    "native_write_guard": NATIVE_WRITE_GUARD_POLICY, "protected_public_paths": list(paths),
                    "protected_public_directories": list(public_test_directories(paths)),
                    "public_directory_additions_allowed": False,
                    "public_inventory_sha256": _object_sha256(list(paths))}
        return {"schema": "jiuwenswarm-benchmark/workspace-policy/v1",
                "source_root": str(self.benchmark_source_root),
                "preserve_existing_tests": True, "automatic_path_rewrite": False}

    def _benchmark_prompt(self, prompt: str, workspace: Path, read_only: bool) -> str:
        if self.benchmark_source_root is None:
            if not read_only:
                return prompt
            return (
                "Read-only stage source boundary:\n"
                f"The authoritative frozen source view is the absolute path {workspace}.\n"
                "Resolve task source paths under that view. The process working directory is the "
                "writable stage state, not the source view; every diagnostic, cache, temporary and "
                "log write belongs to the stage state. Inspect the view without changing it.\n"
                "\nOriginal role request follows verbatim:\n" + prompt
            )
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
        if self.benchmark_write_guard:
            instruction += (
                "The controller enforces preservation of existing public tests and reserved namespaces "
                "for file tools, shells and scripts. Existing public test and fixture directories are "
                "read-only, including creation of new files or subdirectories inside them. Put new "
                "regression checks in a separate checks/ directory outside those public test trees. "
                "Leave controller-owned namespace guard entries "
                "untouched. Git mutations, checkpoints and canonical export belong to the controller; "
                "edit source files and report your changes without managing Git metadata.\n"
            )
        instruction += _PROCESS_KILL_GUIDANCE
        if read_only:
            instruction += (
                f"The authoritative frozen source view is the absolute path {workspace}. "
                "The process working directory is the writable stage state; inspect the view "
                "without changing it and keep every write in the stage state.\n"
                "This role is read-only: inspect evidence without changing any files.\n"
            )
        return instruction + "\nOriginal role request follows verbatim:\n" + prompt

    @staticmethod
    def _preflight_state(environment: dict[str, str], roots: list[Path] | None,
                         output_dir: Path | None) -> Path:
        raw = environment.get("JIUWENSWARM_DATA_DIR", "")
        if raw:
            return Path(raw)
        if output_dir is not None:
            return Path(output_dir)
        if roots:
            return Path(roots[0]).parent
        return Path.cwd()

    @staticmethod
    def _discovery_detail(completed, discovery: object) -> str:
        parts: list[str] = []
        if isinstance(discovery, dict):
            failures = discovery.get("failures")
            if isinstance(failures, list) and failures:
                parts.append(json.dumps(failures[:5], ensure_ascii=False))
        stderr = (getattr(completed, "stderr", "") or "").strip()
        if stderr:
            parts.append(stderr[-4000:])
        if not parts:
            parts.append((getattr(completed, "stdout", "") or "").strip()[-2000:])
        return "\n".join(parts)[-4000:]

    @staticmethod
    def _validate_discovery_evidence(discovery: object, *, output_dir: Path | None) -> None:
        """Reject absent, partial or malformed discovery evidence explicitly."""
        def fail(detail: str) -> None:
            raise WorkerExecutionError(
                "Native import discovery returned malformed or incomplete evidence: " + detail[-4000:],
                code="NATIVE_ACCESS_PREFLIGHT", output_dir=output_dir or Path("."),
            )

        if not isinstance(discovery, dict):
            fail("expected a JSON object")
        if discovery.get("schema") != "long-horizon/native-import-discovery/v1":
            fail("unexpected schema: " + repr(discovery.get("schema")))
        if discovery.get("status") not in {"ok", "failed"}:
            fail("invalid status: " + repr(discovery.get("status")))
        for name in ("executable", "cwd"):
            value = discovery.get(name)
            if not isinstance(value, str) or not value:
                fail(f"{name} must be a non-empty string")
        digest = discovery.get("environment_sha256")
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            fail("environment_sha256 must be a lowercase sha256 digest")
        path = discovery.get("path")
        if not isinstance(path, list) or any(not isinstance(value, str) for value in path):
            fail("path must be a list of strings")
        packages = discovery.get("packages")
        if not isinstance(packages, dict) or any(
                not isinstance(value, dict) for value in packages.values()):
            fail("packages must be an object of package evidence")
        sysconfig = discovery.get("sysconfig")
        if not isinstance(sysconfig, dict) or any(
                not isinstance(key, str) or not isinstance(value, str)
                for key, value in sysconfig.items()):
            fail("sysconfig must be an object of string paths")
        failures = discovery.get("failures")
        if not isinstance(failures, list):
            fail("failures must be a list")
        for name in ("uid", "gid", "euid", "egid"):
            value = discovery.get(name)
            if isinstance(value, bool) or not isinstance(value, int):
                fail(f"{name} must be an integer")
        if not isinstance(discovery.get("dont_write_bytecode"), bool):
            fail("dont_write_bytecode must be a boolean")
        pycache_prefix = discovery.get("pycache_prefix")
        if pycache_prefix is not None and not isinstance(pycache_prefix, str):
            fail("pycache_prefix must be a string or null")

    def _discover_native_imports(self, reviewer_launch_policy, *, state: Path,
                                 startup: _NativeStartup,
                                 environment: dict[str, str],
                                 output_dir: Path | None) -> dict[str, Any]:
        """Run import discovery through the shared startup selection.

        Discovery executes under the reviewer launch prefix with the same
        interpreter, Python flags, environment and ``cwd=state`` that the
        native child will use. A launch failure, nonzero exit, or malformed
        evidence is explicit; failed discoveries are never silently omitted.
        """
        probe = startup.probe()
        if probe is None:
            raise WorkerExecutionError(
                "Native import discovery has no resolved launcher probe: "
                + str(startup.resolution_reason or "unresolved launcher chain"),
                code="NATIVE_ACCESS_PREFLIGHT", output_dir=output_dir or Path("."),
            )
        prefix = reviewer_launch_policy._prefix()
        probe_environment = startup.environment() or dict(environment)
        try:
            completed = subprocess.run(
                [*prefix, *probe, "-c", _PREFLIGHT_DISCOVERY],
                capture_output=True, text=True, timeout=120, check=False,
                cwd=state, env=dict(probe_environment),
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise WorkerExecutionError(
                "Native import discovery could not run through the reviewer launch prefix: "
                + (str(error) or type(error).__name__),
                code="NATIVE_ACCESS_PREFLIGHT", output_dir=output_dir or Path("."),
            ) from None
        output = (completed.stdout or "").strip()
        try:
            discovery: object = json.loads(output.splitlines()[-1]) if output else {}
        except (ValueError, TypeError):
            discovery = {}
        if completed.returncode:
            raise WorkerExecutionError(
                "Native import discovery failed under the reviewer identity: "
                + self._discovery_detail(completed, discovery),
                code="NATIVE_ACCESS_PREFLIGHT", output_dir=output_dir or Path("."),
            )
        self._validate_discovery_evidence(discovery, output_dir=output_dir)
        assert isinstance(discovery, dict)
        # CPython replaces sys.path[0] with the console script's directory.
        # The -c probe reports the cwd as an empty entry instead; substitute
        # only that demonstrable launcher semantic for writable stages.
        if not startup.read_only and startup.script_dir:
            path = list(discovery.get("path") or [])
            if "" in path:
                path[path.index("")] = startup.script_dir
            elif startup.script_dir not in path:
                path.insert(0, startup.script_dir)
            discovery = {**discovery, "path": path}
        # A nonzero exit already failed closed above. A zero-exit status
        # failure (for example injected evidence) stays a reported discovery
        # failure and is rejected by the caller after traversal.
        return discovery

    def _native_access_roots(self, environment: dict[str, str], *,
                             state: Path, startup: _NativeStartup,
                             reviewer_launch_policy, output_dir: Path | None = None,
                             discovery: dict[str, Any] | None = None) -> list[Path]:
        """Derive readable roots from the native child's own import view.

        Discovery executes with the reviewer credentials, the exact shared
        startup selection, the stage environment and ``cwd=state``. Every
        existing directory reported through ``sys.path``, ``PYTHONPATH`` and
        the interpreter's sysconfig is traversed, resolving relative and empty
        entries against the child's cwd. Only the child's identified stdlib
        prefix is excluded; third-party roots found independently beneath it
        (purelib, platlib, package origins and extensions) are retained.
        """
        if discovery is None:
            discovery = self._discover_native_imports(
                reviewer_launch_policy, state=state, startup=startup,
                environment=environment, output_dir=output_dir)
        self._validate_discovery_evidence(discovery, output_dir=output_dir)
        candidates: list[Path] = []

        try:
            child_cwd = Path(str(discovery["cwd"])).expanduser().resolve()
        except (OSError, RuntimeError, ValueError) as error:
            raise WorkerExecutionError(
                "Native import discovery cwd could not be resolved: " + (str(error) or type(error).__name__),
                code="NATIVE_ACCESS_PREFLIGHT", output_dir=output_dir or state,
            ) from None

        def resolve(value: object) -> Path:
            try:
                path = Path(value).expanduser()
            except (TypeError, ValueError, RuntimeError) as error:
                raise WorkerExecutionError(
                    "Native import root is not a resolvable path: " + repr(value)
                    + " (" + (str(error) or type(error).__name__) + ")",
                    code="NATIVE_ACCESS_PREFLIGHT", output_dir=output_dir or state,
                ) from None
            if not path.is_absolute():
                path = child_cwd / path
            try:
                return path.resolve()
            except (OSError, RuntimeError) as error:
                raise WorkerExecutionError(
                    "Native import root could not be resolved: " + str(path)
                    + " (" + (str(error) or type(error).__name__) + ")",
                    code="NATIVE_ACCESS_PREFLIGHT", output_dir=output_dir or state,
                ) from None

        def add_resolved(resolved: Path) -> None:
            try:
                is_directory = resolved.is_dir()
            except OSError as error:
                raise WorkerExecutionError(
                    "Native import root could not be inspected: " + str(resolved)
                    + " (" + (str(error) or type(error).__name__) + ")",
                    code="NATIVE_ACCESS_PREFLIGHT", output_dir=output_dir or state,
                ) from None
            if is_directory and resolved not in candidates:
                candidates.append(resolved)

        def add(path: object) -> None:
            add_resolved(resolve(path))

        stdlib_prefixes: list[Path] = []
        for key, value in (discovery.get("sysconfig") or {}).items():
            if key not in {"stdlib", "platstdlib"}:
                continue
            resolved = resolve(value)
            if resolved not in stdlib_prefixes:
                stdlib_prefixes.append(resolved)

        def under_stdlib(path: Path) -> bool:
            return any(path == prefix or prefix in path.parents for prefix in stdlib_prefixes)

        def add_python_root(value: object) -> None:
            resolved = resolve(value)
            try:
                is_directory = resolved.is_dir()
            except OSError as error:
                raise WorkerExecutionError(
                    "Python import root could not be inspected: " + str(resolved)
                    + " (" + (str(error) or type(error).__name__) + ")",
                    code="NATIVE_ACCESS_PREFLIGHT", output_dir=output_dir or state,
                ) from None
            if not is_directory or under_stdlib(resolved):
                return
            add_resolved(resolved)

        # The companion package tree also contains jiuwenswarm/extensions.
        add(Path(__file__).resolve().parents[2])
        # CPython puts a writable console script's directory at sys.path[0];
        # the probe runs with -c, so add only that demonstrable semantics.
        if not startup.read_only and startup.script_dir:
            add_python_root(startup.script_dir)
        for value in discovery.get("path") or []:
            add_python_root(value)
        for package in (discovery.get("packages") or {}).values():
            origin = package.get("origin")
            if origin:
                add(Path(origin).parent)
            for location in package.get("locations") or []:
                add(location)
                add(Path(location).parent)
                add(Path(location) / "extensions")
                add(Path(location).parent / "extensions")
        for key, value in (discovery.get("sysconfig") or {}).items():
            # stdlib was excluded above; purelib/platlib may legitimately live
            # beneath it and are retained as independently discovered roots.
            if key in {"stdlib", "platstdlib"}:
                continue
            add(value)
        probe_environment = startup.environment() or environment
        for value in probe_environment.get("PYTHONPATH", "").split(os.pathsep):
            add_python_root(value)

        if startup.interpreter is None:
            raise WorkerExecutionError(
                "Native access roots require a resolved launcher probe.",
                code="NATIVE_ACCESS_PREFLIGHT", output_dir=output_dir or state,
            )
        interpreter_path = resolve(startup.interpreter)
        add(interpreter_path.parent)
        # Preserve venv launcher semantics: the interpreter's virtual
        # environment and its site-packages roots are part of its import view.
        version = f"python{interpreter_path.name.removeprefix('python')[0:4]}"
        for base in (interpreter_path.parent.parent, interpreter_path.parent.parent / "lib"):
            add(base / version / "site-packages")
            add(base / version / "dist-packages")
        for pattern in ("lib/python*/site-packages", "lib/python*/dist-packages",
                        "lib64/python*/site-packages", "lib64/python*/dist-packages"):
            try:
                matches = list(interpreter_path.parent.parent.glob(pattern))
            except OSError as error:
                raise WorkerExecutionError(
                    "Native interpreter site-packages could not be inspected: "
                    + str(interpreter_path.parent.parent)
                    + " (" + (str(error) or type(error).__name__) + ")",
                    code="NATIVE_ACCESS_PREFLIGHT", output_dir=output_dir or state,
                ) from None
            for path in matches:
                add(path)
        if self.binary is not None:
            resolved_binary = resolve(self.binary)
            add(resolved_binary.parent)
            # A native launcher may live in ``<install>/.venv/bin``. The
            # import package and its extension-discovery directory are owned
            # by the install root above the virtual environment.
            for parent in (resolved_binary.parent, *resolved_binary.parents):
                try:
                    is_install_root = (parent / "jiuwenswarm").is_dir()
                except OSError as error:
                    raise WorkerExecutionError(
                        "Native install root could not be inspected: " + str(parent)
                        + " (" + (str(error) or type(error).__name__) + ")",
                        code="NATIVE_ACCESS_PREFLIGHT", output_dir=output_dir or state,
                    ) from None
                if is_install_root:
                    add(parent)
                    add(parent / "jiuwenswarm" / "extensions")
                    add(parent / "extensions")
                    break
        if startup.read_only and self.native_python is not None:
            add(resolve(self.native_python).parent)
        # Traverse the resolved target of any symlink-shaped root as well.
        for candidate in list(candidates):
            add(candidate)
        if not candidates:
            raise WorkerExecutionError(
                "Native import discovery found no readable runtime roots.",
                code="NATIVE_ACCESS_PREFLIGHT", output_dir=output_dir or state,
            )
        return candidates

    def _native_traversal_roots(self, environment: dict[str, str], *,
                                state: Path, startup: _NativeStartup) -> list[Path]:
        """Collect launcher/install/declared roots when the chain is unresolved.

        These roots are used only for filesystem traversal under the reviewer
        uid. They are deliberately not presented as native-interpreter or import
        discovery evidence.
        """
        candidates: list[Path] = []
        probe_environment = startup.environment() or dict(environment)

        def add(value: object) -> None:
            try:
                path = Path(value).expanduser()
            except (TypeError, ValueError, RuntimeError):
                return
            if not path.is_absolute():
                path = state / path
            try:
                resolved = path.resolve()
            except (OSError, RuntimeError):
                return
            if resolved.exists() and resolved not in candidates:
                candidates.append(resolved)

        add(self.binary)
        add(self.binary.parent)
        # A fixture/install launcher commonly keeps its dependency tree beside
        # the bin directory. Never widen this to broad system roots.
        system_roots = {"/", "/usr", "/bin", "/sbin", "/opt", "/var", "/etc"}
        if str(self.binary.parent.parent) not in system_roots:
            add(self.binary.parent.parent)
        try:
            resolved_binary = Path(os.path.realpath(self.binary))
        except (OSError, RuntimeError):
            resolved_binary = self.binary
        add(resolved_binary.parent)
        if str(resolved_binary.parent.parent) not in system_roots:
            add(resolved_binary.parent.parent)
        for row in startup.chain:
            if isinstance(row, dict):
                add(row.get("path"))
                add(row.get("target"))
        # The install tree above a venv launcher, plus its virtual environment.
        for parent in (self.binary.parent, *self.binary.parents):
            try:
                install_root = (parent / "jiuwenswarm").is_dir()
            except OSError:
                install_root = False
            if install_root:
                add(parent)
                add(parent / "jiuwenswarm")
                add(parent / "jiuwenswarm" / "extensions")
                add(parent / "extensions")
                break
        for base in (self.binary.parent.parent,):
            for pattern in ("lib/python*/site-packages", "lib/python*/dist-packages",
                            "lib64/python*/site-packages", "lib64/python*/dist-packages"):
                try:
                    matches = list(base.glob(pattern))
                except OSError:
                    matches = []
                for match in matches:
                    add(match)
        # Declared import roots are traversable even without an interpreter.
        for key in ("PYTHONPATH",):
            value = probe_environment.get(key, "")
            if value == "" and key in probe_environment:
                add(state)
                continue
            for entry in value.split(os.pathsep):
                add(state if entry == "" else entry)
        for key in ("JIUWENSWARM_ROOT", "JW_DIR"):
            if probe_environment.get(key):
                add(probe_environment[key])
        return candidates

    def _preflight_native_access(self, reviewer_launch_policy, environment: dict[str, str],
                                 *, roots: list[Path] | None = None,
                                 output_dir: Path | None = None,
                                 state: Path | None = None,
                                 read_only: bool = True,
                                 startup: _NativeStartup | None = None) -> dict[str, Any]:
        """Prove the reviewer uid can traverse the native runtime.

        A resolved launcher chain supplies the native interpreter, flags,
        effective environment and script directory for discovery on writable
        stages; read-only stages probe the same interpreter and flags as their
        real command. An unresolved chain keeps the real writable command
        untouched and falls back to traversal-only filesystem checking of the
        launcher, install tree and declared import roots. Only a concrete
        ``EACCES`` fails that fallback; resolution uncertainty is recorded as a
        limitation.
        """
        if reviewer_launch_policy is None or getattr(reviewer_launch_policy, "mode", None) != "uid_drop":
            return {"status": "not_applicable"}
        selected_state = (Path(state) if state is not None
                          else self._preflight_state(environment, roots, output_dir))
        if startup is None:
            startup = self._native_startup(
                state=selected_state, read_only=read_only, environment=environment)
        discovery = None
        limitations: list[str] = []
        explicit_roots = roots is not None
        if explicit_roots:
            selected_paths = [Path(path) for path in roots]
        elif startup.resolved():
            discovery = self._discover_native_imports(
                reviewer_launch_policy, state=selected_state, startup=startup,
                environment=environment, output_dir=output_dir)
            selected_paths = self._native_access_roots(
                environment, state=selected_state, startup=startup,
                reviewer_launch_policy=reviewer_launch_policy, output_dir=output_dir,
                discovery=discovery)
        else:
            selected_paths = self._native_traversal_roots(
                environment, state=selected_state, startup=startup)
            limitations.append(
                "launcher chain unresolved: " + str(startup.resolution_reason or "unknown reason"))
            limitations.append(
                "preflight used traversal-only filesystem evidence; no native interpreter "
                "or import discovery was established")
            if not selected_paths:
                limitations.append("no readable launcher/install/declared roots were discovered")
        selected = [str(path) for path in selected_paths]
        payload = json.dumps({"roots": selected}, ensure_ascii=False)
        prefix = reviewer_launch_policy._prefix()
        probe_environment = startup.environment() or dict(environment)
        probe = startup.probe()
        traversal_helper_source: str | None = None
        if probe is None:
            # A filesystem helper is explicitly not evidence of the native
            # interpreter. It only observes traversal as the reviewer uid.
            helper = shutil.which("python3", path=probe_environment.get("PATH", os.defpath))
            traversal_helper_source = "PATH python3" if helper else "sys.executable"
            probe = [helper or sys.executable]
            limitations.append(
                "filesystem helper is not native interpreter evidence; "
                "traversal helper source: " + traversal_helper_source)
        try:
            completed = subprocess.run(
                [*prefix, *probe, "-c", _PREFLIGHT_ACCESS, payload],
                capture_output=True, text=True, timeout=120, check=False,
                cwd=selected_state, env=dict(probe_environment),
            )
        except (OSError, subprocess.SubprocessError) as error:
            if startup.resolved():
                raise WorkerExecutionError(
                    "Native access preflight could not run through the reviewer launch prefix: "
                    + (str(error) or type(error).__name__),
                    code="NATIVE_ACCESS_PREFLIGHT", output_dir=output_dir or Path("."),
                ) from None
            limitations.append(
                "traversal helper could not run: " + (str(error) or type(error).__name__))
            evidence: dict[str, Any] = {
                "schema": "long-horizon/native-access-preflight/v1",
                "status": "limited", "roots": selected,
                "uid": None, "gid": None, "euid": None, "egid": None, "failures": [],
            }
            completed = None
        else:
            output = (completed.stdout or "").strip()
            try:
                parsed = json.loads(output.splitlines()[-1]) if output else {}
            except (ValueError, TypeError):
                parsed = {}
            evidence = parsed if isinstance(parsed, dict) else {}
            failures = evidence.get("failures") if isinstance(evidence.get("failures"), list) else []
            concrete = [
                failure for failure in failures
                if isinstance(failure, dict)
                and (failure.get("error") == "PermissionError"
                     or "Permission denied" in str(failure.get("detail", "")))
            ]
            failed = (completed.returncode != 0 or evidence.get("status") != "ok")
            if failed and (startup.resolved() or concrete):
                detail = (completed.stderr or "").strip()
                if failures:
                    detail += "\n" + json.dumps(failures[:5], ensure_ascii=False)
                if not detail:
                    detail = output[-2000:]
                raise WorkerExecutionError(
                    "Reviewer uid cannot access the native runtime: " + detail[-4000:],
                    code="NATIVE_ACCESS_PREFLIGHT", output_dir=output_dir or Path("."),
                )
            if failed:
                # Inconclusive fallback evidence never rejects by itself.
                limitations.append(
                    "traversal-only helper reported inconclusive non-EACCES evidence")
                evidence = {
                    "schema": "long-horizon/native-access-preflight/v1",
                    "status": "limited", "roots": selected,
                    "uid": None, "gid": None, "euid": None, "egid": None,
                    "failures": failures,
                }
        if not isinstance(evidence.get("roots"), list):
            raise WorkerExecutionError(
                "Native access preflight returned malformed evidence: roots missing",
                code="NATIVE_ACCESS_PREFLIGHT", output_dir=output_dir or Path("."),
            )
        if discovery is not None and discovery.get("status") != "ok":
            failures = discovery.get("failures") or []
            detail = json.dumps(failures[:5], ensure_ascii=False) if failures else (
                "status=" + str(discovery.get("status")))
            raise WorkerExecutionError(
                "Native import discovery failed under the reviewer identity: " + detail[-4000:],
                code="NATIVE_ACCESS_PREFLIGHT", output_dir=output_dir or Path("."),
            )
        evidence.update({
            "interpreter": startup.interpreter,
            "python_flags": list(startup.flags),
            "launcher": startup.launched(),
            "native_command": startup.command(),
            "cwd": str(selected_state),
            "environment_sha256": _environment_digest(probe_environment),
            "launch_environment_sha256": _environment_digest(environment),
            "probe_environment_sha256": _environment_digest(probe_environment),
            "probe_status": "resolved" if startup.resolved() else "unresolved",
            "probe_resolution": startup.resolution,
            # Present only for the unresolved-chain traversal fallback: the
            # helper's own path is traversal-only evidence, never interpreter
            # evidence for the native stage.
            "traversal_helper_source": traversal_helper_source,
            "limitations": limitations,
            "expected_uid": getattr(reviewer_launch_policy, "uid", None),
            "expected_gid": getattr(reviewer_launch_policy, "gid", None),
        })
        if discovery is not None:
            evidence["discovery"] = {
                key: discovery.get(key) for key in (
                    "schema", "status", "executable", "prefix", "base_prefix", "cwd",
                    "uid", "gid", "euid", "egid", "pycache_prefix",
                    "dont_write_bytecode", "environment_sha256", "path",
                    "packages", "sysconfig",
                )
            }
        return evidence

    def _prepare_stage_state(self, state: Path, *, read_only: bool,
                             reviewer_launch_policy=None) -> list[str]:
        """Create/own every reviewer-writable state descendant before uid drop."""
        if not read_only:
            return []
        uid = gid = None
        if reviewer_launch_policy is not None and getattr(reviewer_launch_policy, "mode", None) == "uid_drop":
            uid, gid = reviewer_launch_policy.uid, reviewer_launch_policy.gid
        prepared: list[str] = []
        for relative in STAGE_STATE_DIRECTORIES:
            path = state
            for part in Path(relative).parts:
                path = path / part
                if path.is_symlink():
                    raise WorkerConfigurationError("Stage state descendant must not be a symlink.",
                                                   code="INVALID_STATE_PATH", output_dir=state.parent)
            path.mkdir(parents=True, exist_ok=True)
            if path.is_symlink() or not path.is_dir():
                raise WorkerConfigurationError("Stage state descendant is not a directory.",
                                               code="INVALID_STATE_PATH", output_dir=state.parent)
            path.chmod(0o700)
            if uid is not None and gid is not None:
                os.chown(path, uid, gid)
            prepared.append(str(path))
        state.chmod(0o700)
        if uid is not None and gid is not None:
            os.chown(state, uid, gid)
        return prepared

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
        config_dir.mkdir(parents=True, exist_ok=True)
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
                context_engine = _section(_section(config, "react"), "context_engine_config")
                context_engine["context_debug_dir"] = str(state / "context-debug")
                if identity["tool_policy"] == "none":
                    # The retained ContextProcessorRail registers
                    # recall_compressed_context as a tool when compression
                    # recall is enabled, before NativeToolFreeRail's original
                    # fail-closed guard runs. Disable only that tool-producing
                    # recall capability; context compression, actual-window
                    # recovery and every other configured key stay intact.
                    _section(context_engine, "compression_recall_config")["enabled"] = False
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
        stage_environment = _state_environment(state, read_only=read_only, views=views)
        if not read_only:
            # Binding launch invariant: a writable stage's child environment is
            # byte-identical to baseline 0cb95eb. The companion-wide bytecode
            # default is retained for read-only stages and for callers that
            # explicitly supplied it, but it is not injected into a writable
            # launcher that main started without it.
            stage_environment = dict(stage_environment)
            stage_environment.pop("PYTHONDONTWRITEBYTECODE", None)
        environment.update(stage_environment)
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
        if read_only:
            # Ownership is established before the reviewer credential drop.
            # Any later creation happens in the reviewer's own process.
            prepared = self._prepare_stage_state(
                state, read_only=True, reviewer_launch_policy=reviewer_launch_policy)
            metadata["stage_state_ownership"] = {
                "schema": "long-horizon/stage-state-ownership/v1",
                "state_dir": str(state),
                "uid": (reviewer_launch_policy.uid
                        if reviewer_launch_policy is not None
                        and getattr(reviewer_launch_policy, "mode", None) == "uid_drop" else os.geteuid()),
                "gid": (reviewer_launch_policy.gid
                        if reviewer_launch_policy is not None
                        and getattr(reviewer_launch_policy, "mode", None) == "uid_drop" else os.getegid()),
                "directories": prepared,
                "directory_mode": "0700",
                "file_mode": "0600",
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
        # The process cwd is the writable stage state by construction. A
        # read-only stage names the frozen view explicitly as project_dir so
        # source discovery and read tools stay bound to the view while every
        # relative diagnostic/cache write lands in state.
        request_workspace = (
            {"cwd": str(state), "project_dir": str(workspace),
             "trusted_dirs": [str(view) for view in views]}
            if read_only else
            {"cwd": str(workspace), "project_dir": str(workspace), "trusted_dirs": [str(workspace)]}
        )
        request: dict = {
            "schema_version": "0.1", "type": "run", "request_id": request_id,
            "input": prompt, "mode": "agent.code.normal", "timeout_seconds": timeout,
            "workspace": request_workspace,
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
        # The real command is selected independently of probe information.
        # Writable stages keep the baseline tuple; probes never mutate it.
        startup = self._native_startup(
            state=state, read_only=read_only, environment=environment)
        command, _proof = self._native_command(state=state, read_only=read_only, startup=startup)
        metadata.update(status="running", command=command)
        _json_write(output_dir / "metadata.json", metadata, redactor)
        compact = compact_role_events(stage)
        configured_limit = self.memory_limit_bytes or _memory_int(os.environ.get(MEMORY_ENV_LIMIT))
        configured_reserve = (self.memory_reserve_bytes if self.memory_reserve_bytes is not None
                              else _memory_int(os.environ.get(MEMORY_ENV_RESERVE)))
        configured_cgroup = self.memory_cgroup or os.environ.get(MEMORY_ENV_CGROUP)
        retained_budget, retained_basis = _retained_event_budget(
            stage, limit_bytes=configured_limit, reserve_bytes=configured_reserve, cgroup=configured_cgroup
        )
        retained = _RetainedEventStream(limit=retained_budget, compact=compact)
        metadata["capacity_profile"] = _capacity_receipt()
        metadata["retained_event_payload_limit"] = retained_payload_string_limit() if compact else None
        metadata["retained_event_total_limit"] = retained.limit
        metadata["retained_event_budget_basis"] = retained_basis
        terminal: dict | None = None
        protocol_error: str | None = None
        write_guard: BenchmarkWriteGuard | None = None
        write_guard_failed = False
        process: asyncio.subprocess.Process | None = None
        interrupted = False
        cancellation_observed_at_unix: float | None = None
        process_exit_observed_at_unix: float | None = None
        budget_observed = False
        budget_stop_task: asyncio.Task | None = None
        policy_stop_task: asyncio.Task | None = None
        memory_stop_task: asyncio.Task | None = None
        policy_proofs: list[int] = []
        native_session: str | None = session_id
        seen_records = 0
        started = time.monotonic()
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
            nonlocal terminal, protocol_error, budget_observed, budget_stop_task, policy_stop_task, native_session, seen_records, cancellation_observed_at_unix
            assert process is not None and process.stdout is not None
            spool_dir = output_dir / "stdout-spool"
            receipts: list[dict] = []
            chunk_receipts: list[dict] = []
            threshold = stream_spool_threshold()
            metadata["stream_spool_threshold_bytes"] = threshold
            chunk_reader = _RecordChunkReader(request_id, spool_dir, max_memory=threshold)
            metadata["record_chunk_reader"] = chunk_reader.reader_name
            native_protocol = _native_protocol()
            metadata["record_chunk_frame_bytes"] = getattr(
                native_protocol, "CHUNK_FRAME_BYTES", CHUNK_FRAME_BYTES)

            def handle_record(record: dict) -> dict | None:
                nonlocal terminal, protocol_error, budget_observed, budget_stop_task, policy_stop_task, native_session, seen_records, cancellation_observed_at_unix
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
                line = json.dumps(safe_record, ensure_ascii=False, allow_nan=False) + "\n"
                offset = event_log.tell()
                length = len(line.encode("utf-8"))
                if record.get("type") == "result":
                    terminal = safe_record
                    if (str(record.get("status", "")).lower() == "cancelled"
                            and cancellation_observed_at_unix is None):
                        cancellation_observed_at_unix = time.time()
                event_log.write(line)
                event_log.flush()
                if record.get("type") != "result":
                    retained.feed(
                        safe_record,
                        event_log_offset=offset,
                        event_log_length=length,
                    )
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
                    if record.get("type") == "event":
                        budget_stop_task = asyncio.create_task(_terminate_group(process, grace=0.2))
                return safe_record

            with (output_dir / "events.jsonl").open("w", encoding="utf-8") as event_log, \
                    (output_dir / "stdout.jsonl").open("w", encoding="utf-8") as raw_log:
                lines = _iter_stream_lines(
                    process.stdout, spool_dir=spool_dir, threshold=threshold)
                try:
                    async for item in lines:
                        chunk_receipt: dict | None = None
                        spool_receipt: dict | None = None
                        committed = False
                        try:
                            if isinstance(item, _SpooledRecord):
                                spool_receipt = {
                                    "path": str(item.path), "sha256": item.sha256,
                                    "bytes": item.size,
                                }
                                receipts.append(spool_receipt)
                                metadata["stream_spooled_records"] = receipts[-1000:]
                                metadata["stream_spooled_record_count"] = len(receipts)
                                try:
                                    raw = item.path.read_bytes()
                                except OSError as error:
                                    if protocol_error is None:
                                        protocol_error = "STREAM_READ_FAILED"
                                        metadata.update(_protocol_error_fields(error))
                                    continue
                            else:
                                raw = item
                            try:
                                record = _decode_object(raw)
                            except (UnicodeError, ValueError, TypeError, RecursionError) as error:
                                if _looks_like_chunk(raw):
                                    raw_log.write(_undecodable_chunk_note(len(raw)))
                                else:
                                    raw_log.write(_undecodable_record_note(len(raw)))
                                raw_log.flush()
                                if protocol_error is None:
                                    protocol_error = (
                                        "INVALID_CHUNK_STREAM" if _looks_like_chunk(raw)
                                        else "INVALID_JSONL")
                                    metadata.update(_protocol_error_fields(error))
                                continue
                            if record.get("type") == CHUNK_TYPE:
                                raw_log.write(_chunk_frame_note(record, len(raw)))
                                raw_log.flush()
                                try:
                                    logical = chunk_reader.accept(record)
                                except (OSError, UnicodeError, ValueError, TypeError, RuntimeError, RecursionError) as error:
                                    # A transport frame is never a business event.
                                    # A bad stream must not leave a half record in
                                    # events.jsonl; the next logical record still
                                    # follows the ordinary correlation checks.
                                    if protocol_error is None:
                                        protocol_error = "INVALID_CHUNK_STREAM"
                                        metadata.update(_protocol_error_fields(error))
                                    continue
                                if logical is None:
                                    continue
                                record = logical
                                chunk_receipt = chunk_reader.last_receipt
                            try:
                                safe = handle_record(record)
                            except (UnicodeError, ValueError, TypeError, RecursionError) as error:
                                raw_log.write(_rejected_record_note(error))
                                raw_log.flush()
                                if protocol_error is None:
                                    protocol_error = "INVALID_JSONL"
                                    metadata.update(_protocol_error_fields(error))
                                continue
                            if safe is None:
                                continue
                            safe_line = json.dumps(
                                safe, ensure_ascii=False, allow_nan=False) + "\n"
                            # stdout.jsonl is a published artifact.  A logical
                            # record that arrived unframed is written here only
                            # after the logical redactor runs.  A record_chunk
                            # frame already left a data-free transport note
                            # above; its logical record is published through
                            # events.jsonl and, when oversized, the committed
                            # spool.
                            if chunk_receipt is None:
                                raw_log.write(safe_line)
                                raw_log.flush()
                            if chunk_receipt is not None:
                                ledger_receipt = dict(chunk_receipt)
                                ledger_receipt["sequence"] = safe.get("sequence")
                                ledger_receipt["record_type"] = safe.get("type")
                                ledger_receipt["event_type"] = safe.get("event_type")
                                ledger_receipt["ledger_sha256"] = hashlib.sha256(
                                    safe_line.encode("utf-8")).hexdigest()
                                chunk_receipts.append(ledger_receipt)
                                metadata["record_chunk_receipts"] = chunk_receipts[-1000:]
                                metadata["record_chunk_receipt_count"] = len(chunk_receipts)
                            if spool_receipt is not None:
                                try:
                                    # Remove the raw physical intermediate
                                    # before publishing the sanitized logical
                                    # record under the same receipt path.
                                    item.path.unlink(missing_ok=True)
                                    item.path.write_text(safe_line, encoding="utf-8")
                                    os.chmod(item.path, 0o600)
                                    spool_receipt["stored_sha256"] = hashlib.sha256(
                                        item.path.read_bytes()).hexdigest()
                                    spool_receipt["disposition"] = "committed"
                                    committed = True
                                except OSError as error:
                                    metadata["stdout_spool_commit_failed"] = (
                                        metadata.get("stdout_spool_commit_failed", 0) + 1)
                                    metadata["stdout_spool_commit_error"] = (
                                        type(error).__name__[:128])
                        finally:
                            if spool_receipt is not None:
                                if not committed:
                                    try:
                                        item.path.unlink(missing_ok=True)
                                    except OSError:
                                        spool_receipt["disposition"] = "scrub_failed"
                                        metadata["stdout_spool_scrub_failed"] = (
                                            metadata.get("stdout_spool_scrub_failed", 0) + 1)
                                    else:
                                        spool_receipt["disposition"] = "removed"
                                metadata["stream_spooled_records"] = receipts[-1000:]
                    if chunk_reader.streams:
                        incomplete = len(chunk_reader.streams)
                        metadata["incomplete_chunk_streams"] = incomplete
                        if protocol_error is None:
                            protocol_error = "INCOMPLETE_CHUNK_STREAM"
                            metadata.update({
                                "protocol_error_detail": "chunk stream ended before its final frame",
                                "protocol_error_exception": "ChunkStreamError",
                            })
                except (OSError, ValueError) as error:
                    # Diagnostic/spool I/O failure must not convert a completed
                    # native turn into a fatal protocol error; retained bytes
                    # already written remain the authoritative evidence.
                    if protocol_error is None:
                        protocol_error = "STREAM_READ_FAILED"
                        metadata.update(_protocol_error_fields(error))
                finally:
                    try:
                        await _close_async_iterator(lines)
                    finally:
                        chunk_reader.close()

        async def read_stderr() -> None:
            assert process is not None and process.stderr is not None
            spool_dir = output_dir / "stderr-spool"
            threshold = stream_spool_threshold()
            receipts: list[dict] = []
            with (output_dir / "stderr.log").open("w", encoding="utf-8") as stream:
                lines = _iter_stream_lines(
                    process.stderr, spool_dir=spool_dir, threshold=threshold)
                try:
                    async for item in lines:
                        spool_receipt: dict | None = None
                        committed = False
                        try:
                            if isinstance(item, _SpooledRecord):
                                spool_receipt = {
                                    "path": str(item.path), "sha256": item.sha256,
                                    "bytes": item.size,
                                }
                                receipts.append(spool_receipt)
                                metadata["stderr_spooled_records"] = receipts[-1000:]
                                text = item.path.read_text(
                                    encoding="utf-8", errors="replace")
                                safe_text = redactor.text(text)
                                # The raw diagnostic must not remain durable
                                # while its sanitized replacement is written.
                                item.path.unlink(missing_ok=True)
                                item.path.write_text(safe_text, encoding="utf-8")
                                os.chmod(item.path, 0o600)
                                spool_receipt["stored_sha256"] = hashlib.sha256(
                                    item.path.read_bytes()).hexdigest()
                                spool_receipt["disposition"] = "committed"
                                committed = True
                                metadata["stderr_spooled_records"] = receipts[-1000:]
                                stream.write(
                                    f"[complete oversized diagnostic retained at {item.path}; "
                                    f"sha256={item.sha256}; bytes={item.size}]\n"
                                )
                            else:
                                stream.write(redactor.text(
                                    item.decode("utf-8", errors="replace")))
                            stream.flush()
                        finally:
                            if spool_receipt is not None:
                                if not committed:
                                    try:
                                        item.path.unlink(missing_ok=True)
                                    except OSError:
                                        spool_receipt["disposition"] = "scrub_failed"
                                        metadata["stderr_spool_scrub_failed"] = (
                                            metadata.get("stderr_spool_scrub_failed", 0) + 1)
                                    else:
                                        spool_receipt["disposition"] = "removed"
                                metadata["stderr_spooled_records"] = receipts[-1000:]
                except (OSError, ValueError):
                    # Keep the last successfully written diagnostic bytes.
                    stream.write("[diagnostic stream read stopped after an I/O error]\n")
                finally:
                    await _close_async_iterator(lines)

        async def communicate() -> None:
            assert process is not None and process.stdin is not None
            process.stdin.write(request_bytes)
            try:
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            process.stdin.close()
            await asyncio.gather(read_stdout(), read_stderr(), process.wait())

        def mark_cancelled() -> None:
            """Record the first cancellation observation, never collection time."""
            nonlocal interrupted, cancellation_observed_at_unix
            interrupted = True
            if cancellation_observed_at_unix is None:
                cancellation_observed_at_unix = time.time()

        async def finish_cleanup(awaitable):
            """Repeated task cancellation cannot interrupt owned-process cleanup."""
            cleanup = asyncio.ensure_future(awaitable)
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    mark_cancelled()
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
                # Establish reviewer-owned state and traversal before the
                # preflight executes with cwd=state under the dropped uid.
                reviewer_launch_policy.grant_traversal(output_dir)
                reviewer_launch_policy.own_tree(state)
                reviewer_launch_policy.check_managed(state)
                try:
                    preflight = self._preflight_native_access(
                        reviewer_launch_policy, environment, output_dir=output_dir,
                        state=state, read_only=read_only, startup=startup)
                except WorkerExecutionError as error:
                    metadata.update(status="access_preflight_failed",
                                    failure_code=error.code, access_preflight_error=str(error)[:4000])
                    _json_write(output_dir / "metadata.json", metadata, redactor)
                    raise
                metadata["native_access_preflight"] = preflight
            command, proof = self._native_command(
                state=state, read_only=read_only, reviewer_launch_policy=reviewer_launch_policy,
                startup=startup, environment=environment)
            metadata["command"] = command
            _json_write(output_dir / "metadata.json", metadata, redactor)
            if self.benchmark_write_guard and not read_only and identity["tool_policy"] != "none":
                policy = identity["benchmark_workspace_policy"]
                if policy is None or policy["native_write_guard"] != NATIVE_WRITE_GUARD_POLICY:
                    raise WriteGuardError("Benchmark write-guard source identity is unavailable.")
                journals = ()
                if environment.get("JIUWENSWARM_MEMGRAPH_ENABLED") == "1":
                    journal = environment.get("MEMGRAPH_CALL_AUDIT_PATH", "")
                    if not journal:
                        raise WriteGuardError("Configured graph MCP diagnostic journal is unavailable.")
                    journals = (Path(journal),)
                write_guard = BenchmarkWriteGuard(workspace, state, tuple(policy["protected_public_paths"]), journals)
                write_guard.prepare()
                environment = write_guard.environment(environment)
                command = [*write_guard.command_prefix, *command]
                metadata.update(command=command, native_write_guard=dict(write_guard.receipt),
                                requested_config_environment_sha256=_environment_sha256(
                                    config_path.read_text(encoding="utf-8"), environment))
                _json_write(output_dir / "native-write-guard.json", write_guard.receipt, redactor)
                _json_write(output_dir / "metadata.json", metadata, redactor)
            spawning = asyncio.create_task(asyncio.create_subprocess_exec(
                *command, cwd=state, env=environment,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, start_new_session=True, limit=DEFAULT_STREAM_SPOOL_BYTES,
            ))
            try:
                process = await asyncio.shield(spawning)
            except asyncio.CancelledError:
                mark_cancelled()
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
                    mark_cancelled()
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
                process_exit_observed_at_unix = time.time()
                metadata.update(actual_exit_code=actual_exit,
                                signal=-actual_exit if actual_exit is not None and actual_exit < 0 else None)
            if memory_stop_task is not None:
                if not memory_stop_task.done():
                    memory_stop_task.cancel()
                await asyncio.gather(memory_stop_task, return_exceptions=True)
            if memory_pressure_observed:
                metadata.update(memory_pressure_observed=True, memory_watchdog=memory_pressure)
            if write_guard is not None:
                receipt = write_guard.finish()
                metadata["native_write_guard"] = dict(receipt)
                write_guard_failed = receipt["status"] != "complete"
                _json_write(output_dir / "native-write-guard.json", receipt, redactor)
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
            terminal_cancelled = str((terminal or {}).get("status", "")).lower() == "cancelled"
            if interrupted or cancellation_observed_at_unix is not None or terminal_cancelled:
                context = _notice_context(environment)
                for name in ("token", "invocation_token", "notice_token"):
                    value = context.get(name)
                    if isinstance(value, str) and value:
                        context.setdefault(name + "_sha256", hashlib.sha256(value.encode()).hexdigest())
                # Only an observed cancellation supplies a cancellation time.
                # Process exit is retained separately, and no exit code or
                # signal is promoted into a cancellation observation.
                observed_cancel = cancellation_observed_at_unix
                metadata["cancellation"] = {
                    "schema": "long-horizon/native-cancellation/v1",
                    "invocation_id": context.get("invocation_id") or environment.get("JIUWENSWARM_INVOCATION_ID"),
                    "request_id": request_id,
                    "session_id": (terminal or {}).get("session_id"),
                    # The first observed cancellation is the cancellation time.
                    # Metadata collection later in this method never replaces it.
                    "cancellation_observed_at_unix": observed_cancel,
                    "process_exit_observed_at_unix": process_exit_observed_at_unix,
                    "terminal_timestamp_unix": _terminal_timestamp(terminal),
                    "terminal_sequence": (terminal or {}).get("sequence"),
                    "terminal_status": (terminal or {}).get("status"),
                    "terminal_result_sha256": _object_sha256(terminal) if terminal is not None else None,
                    "notice_context": context,
                    "notice_context_present": bool(context),
                }
            _json_write(output_dir / "metadata.json", metadata, redactor)

        if write_guard_failed:
            metadata.update(status="failed", failure_code="NATIVE_WRITE_GUARD_INTEGRITY_FAILED")
            _json_write(output_dir / "metadata.json", metadata, redactor)
            raise WorkerExecutionError("Native benchmark write guard failed its integrity check.",
                                       code="NATIVE_WRITE_GUARD_INTEGRITY_FAILED", output_dir=output_dir)
        actual_exit = process.returncode if process is not None else 1
        assert actual_exit is not None
        safe_records = retained.retained()
        partial_text, partial_usage, partial_session = retained.summary()
        final_text, final_provenance = (
            select_native_final_response(
                safe_records, terminal, final_event_dropped=retained.final_event_dropped,
                final_event_dropped_sequence=retained.final_event_dropped_sequence,
                event_log=output_dir / "events.jsonl",
                final_event_dropped_offset=retained.final_event_dropped_offset,
                final_event_dropped_length=retained.final_event_dropped_length,
                invalidating_sequence=retained.last_invalidating_sequence)
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
        elif result.status == "cancelled":
            # Cancellation comes only from the native terminal status. Exit
            # codes and signals, including 130/137/143 and SIGKILL, are not
            # proof that the task was cancelled.
            result.status = "cancelled"
            failure, code = WorkerExecutionError, "INTERRUPTED"
        elif protocol_error or terminal is None or terminal.get("exit_code") != actual_exit:
            result.status = "failed"
            failure, code = WorkerProtocolError, protocol_error or "MISSING_OR_INCONSISTENT_RESULT"
        elif result.status != "completed" or actual_exit != 0:
            failure, code = WorkerExecutionError, "NATIVE_FAILED"
        metadata.update(status=result.status, failure_code=code or None, usage=result.usage,
                        session_id=result.session_id, terminal_result_received=terminal is not None,
                        budget_stop_observed=budget_observed, final_response=final_provenance,
                        final_event_dropped=retained.final_event_dropped,
                        final_event_dropped_sequence=retained.final_event_dropped_sequence,
                        final_event_dropped_offset=retained.final_event_dropped_offset,
                        final_event_dropped_length=retained.final_event_dropped_length,
                        last_invalidating_sequence=retained.last_invalidating_sequence)
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
