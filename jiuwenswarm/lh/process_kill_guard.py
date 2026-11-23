# Copyright (c) 2026 Chrys. All rights reserved.

"""Keep role processes from killing the Agent or framework by broad name match.

``pkill -f``, ``killall`` and ``pgrep -f ... | xargs kill`` match command lines
or process names, so the task text in an Agent's own argv/environment can make
them target the Agent or its controller.  Roles may only stop a process they
started and recorded, or that process's group.  This module carries the shared
guidance text, a command-level guard for shell-tool hooks, and a small POSIX
shell wrapper for runtimes whose frozen tree cannot be patched.
"""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path
from typing import Mapping

PROCESS_KILL_GUIDANCE_VERSION = "pid-scoped-kill/v1"
PROCESS_KILL_ERROR = (
    "Error: broad process termination is blocked (pid-scoped-kill/v1). "
    "Do not use pkill -f, killall, or pgrep -f ... | xargs kill: they match command lines or names and "
    "can kill this Agent or the framework. Stop only a process you started and whose PID you recorded, "
    "or its process group. For a background service, write its PID to a file and stop it later with that PID."
)

_SHELLS = frozenset({"bash", "dash", "sh", "zsh", "ksh"})
_WRAPPERS = frozenset({"command", "env", "exec", "nohup", "setsid", "sudo", "time"})
_SEPARATORS = frozenset({";", "&&", "||", "&", "\n", "(", ")"})
_REDIRECTS = frozenset({">", ">>", "<", "2>", "2>>", "&>", "&>>"})


def process_kill_guidance() -> str:
    """The short, task-independent clause added to Worker and Reviewer guidance."""
    return (
        "\n\n## Process termination (pid-scoped)\n"
        "Do not stop processes with broad command-line or name matching such as `pkill -f`, `killall`, or "
        "`pgrep -f ... | xargs kill`; these can match this Agent or the framework.\n"
        "Stop only a process you started and whose PID you recorded, or its process group.\n"
        "For a background service, write its PID to a file and stop it later using that PID.\n"
    )


def _tokens(command: str) -> list[str]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError:
        return command.split()


def _basename(value: str) -> str:
    return value.rsplit("/", 1)[-1]


def _option_args(args: list[str]) -> tuple[list[str], bool, bool]:
    """Return positional tokens, whether parent-scoped, and whether full-match."""
    positional: list[str] = []
    parent = False
    full = False
    index = 0
    while index < len(args):
        token = args[index]
        if token in _REDIRECTS:
            index += 2
            continue
        if token in ("-P", "--parent"):
            parent = True
            index += 2
            continue
        if token.startswith("--parent="):
            parent = True
            index += 1
            continue
        if token.startswith("-P") and len(token) > 2:
            parent = True
            index += 1
            continue
        if token in ("-f", "--full"):
            full = True
            index += 1
            continue
        if token.startswith("--full="):
            full = True
            index += 1
            continue
        if token.startswith("--"):
            index += 1
            continue
        if token.startswith("-"):
            # pkill accepts short option clusters. Parent scope remains the
            # only form that proves the target set is owned by this role.
            if token.startswith("-f") or ("f" in token[1:] and "P" not in token[1:]):
                full = True
            index += 1
            continue
        positional.append(token)
        index += 1
    return positional, parent, full


def _strip_prefixes(tokens: list[str]) -> list[str]:
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in _REDIRECTS:
            index += 2
            continue
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", token, re.S):
            index += 1
            continue
        if _basename(token) in _WRAPPERS:
            index += 1
            # Skip common wrapper options and their values.
            while index < len(tokens):
                option = tokens[index]
                if option in ("-u", "-g", "-h", "-p", "-C", "-T", "-n", "-o"):
                    index += 2
                    continue
                if option.startswith("-"):
                    index += 1
                    continue
                break
            continue
        break
    return tokens[index:]


def _pkill_error(args: list[str]) -> str | None:
    _positional, parent, _full = _option_args(args)
    if parent:
        return None
    # A bare pkill is name matching too.  Only an explicit parent scope proves
    # the candidate set belongs to this role.
    return PROCESS_KILL_ERROR


def _xargs_kills(args: list[str]) -> bool:
    tokens = _strip_prefixes(args)
    for index, token in enumerate(tokens):
        name = _basename(token)
        if name in ("kill", "killall"):
            return True
        if name in _SHELLS and index + 1 < len(tokens) and tokens[index + 1] == "-c":
            return bool(blocked_process_kill(" ".join(tokens[index + 2 :])))
    return False


def _command_error(tokens: list[str], *, depth: int) -> str | None:
    tokens = _strip_prefixes(tokens)
    if not tokens:
        return None
    name = _basename(tokens[0])
    args = tokens[1:]
    if name == "pkill":
        return _pkill_error(args)
    if name == "killall":
        return PROCESS_KILL_ERROR
    if name == "xargs" and _xargs_kills(args):
        # xargs kill is the execution half of pgrep -f ... | xargs kill.  It is
        # not parsed here; the pipeline check below supplies that context, but
        # a bare xargs kill is also an input-driven broad termination.
        return PROCESS_KILL_ERROR
    if name in _SHELLS:
        for index, value in enumerate(args):
            if value == "-c" and index + 1 < len(args) and depth < 4:
                nested = blocked_process_kill(args[index + 1], depth=depth + 1)
                if nested is not None:
                    return nested
    return None


def _pipeline_error(commands: list[list[str]], *, depth: int) -> str | None:
    for command in commands:
        error = _command_error(command, depth=depth)
        if error is not None:
            return error
    names = [_basename(_strip_prefixes(command)[0]) if _strip_prefixes(command) else "" for command in commands]
    if "pgrep" in names:
        pgrep = commands[names.index("pgrep")]
        _positional, _parent, full = _option_args(pgrep[1:])
        if full and any(_xargs_kills(command[1:]) for command in commands if _basename(command[0]) == "xargs"):
            return PROCESS_KILL_ERROR
    return None


def blocked_process_kill(command: str, *, depth: int = 0) -> str | None:
    """Return an ``Error: `` explanation when *command* uses a broad kill form.

    PID-scoped forms (``kill <pid>``, ``kill -TERM -<pgid>``, ``pkill -P
    <ppid>``) and quoted mentions are allowed.
    """
    if not isinstance(command, str) or not command.strip():
        return None
    tokens = _tokens(command)
    pipeline: list[list[str]] = []
    current: list[str] = []
    for token in tokens + [None]:
        if token is None or token in _SEPARATORS or token == "|":
            if current:
                pipeline.append(current)
                current = []
            if token == "|":
                continue
            error = _pipeline_error(pipeline, depth=depth)
            if error is not None:
                return error
            pipeline = []
        else:
            current.append(token)
    # Command substitution is tokenized into "$" "(" ... ")". The raw form is
    # easier to recognise and covers kill $(pgrep -f ...) without treating a
    # normal recorded-PID substitution as broad.
    if re.search(r"\bkill(?:all)?\b[^\n;|&]*\$\(\s*pgrep\b[^\n)]*(?:-f|--full)\b", command):
        return PROCESS_KILL_ERROR
    return None


_SHELL_GUARD = r'''# long-horizon pid-scoped process-kill guard (pid-scoped-kill/v1)
if [ "${LONG_HORIZON_PROCESS_KILL_GUARD_LOADED:-0}" != "1" ]; then
LONG_HORIZON_PROCESS_KILL_GUARD_LOADED=1
if [ -n "${LONG_HORIZON_BASE_BASH_ENV:-}" ] && [ -r "${LONG_HORIZON_BASE_BASH_ENV}" ]; then
  . "${LONG_HORIZON_BASE_BASH_ENV}"
fi
if [ -n "${LONG_HORIZON_BASE_ENV:-}" ] && [ -r "${LONG_HORIZON_BASE_ENV}" ]; then
  . "${LONG_HORIZON_BASE_ENV}"
fi
__lh_real_pkill="$(command -v pkill 2>/dev/null || true)"
__lh_real_killall="$(command -v killall 2>/dev/null || true)"
__lh_real_xargs="$(command -v xargs 2>/dev/null || true)"
__lh_process_kill_error() {
  printf '%s\n' 'Error: broad process termination is blocked (pid-scoped-kill/v1). Do not use pkill -f, killall, or pgrep -f ... | xargs kill: they match command lines or names and can kill this Agent or the framework. Stop only a process you started and whose PID you recorded, or its process group. For a background service, write its PID to a file and stop it later with that PID.' >&2
  return 1
}
__lh_pkill_parent_scoped() {
  for __lh_arg in "$@"; do
    case "$__lh_arg" in
      -P|--parent|-P*|--parent=*) return 0 ;;
    esac
  done
  return 1
}
pkill() {
  if __lh_pkill_parent_scoped "$@"; then
    if [ -n "$__lh_real_pkill" ]; then command "$__lh_real_pkill" "$@"; else return 127; fi
  else
    __lh_process_kill_error
  fi
}
killall() {
  __lh_process_kill_error
}
xargs() {
  for __lh_arg in "$@"; do
    case "$(basename -- "$__lh_arg" 2>/dev/null || printf '%s' "$__lh_arg")" in
      kill|killall) __lh_process_kill_error; return 1 ;;
    esac
  done
  if [ -n "$__lh_real_xargs" ]; then command "$__lh_real_xargs" "$@"; else return 127; fi
}
fi
'''


def shell_guard_script() -> str:
    return _SHELL_GUARD


def install_shell_guard(directory: Path) -> Path:
    """Write the sourced guard script and return its path."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ".lh-process-kill-guard.sh"
    text = shell_guard_script()
    try:
        if path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")
    except FileNotFoundError:
        path.write_text(text, encoding="utf-8")
    return path


def guarded_environment(
    environment: Mapping[str, str] | None,
    directory: Path,
    *,
    base_environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Copy *environment* and arrange for non-interactive shells to source the guard."""
    base = dict(os.environ if base_environment is None else base_environment)
    values = dict(base if environment is None else environment)
    script = install_shell_guard(directory)
    if values.get("BASH_ENV") and Path(values["BASH_ENV"]) != script:
        values["LONG_HORIZON_BASE_BASH_ENV"] = values["BASH_ENV"]
    if values.get("ENV") and Path(values["ENV"]) != script:
        values["LONG_HORIZON_BASE_ENV"] = values["ENV"]
    values["LONG_HORIZON_PROCESS_KILL_GUARD"] = PROCESS_KILL_GUIDANCE_VERSION
    values["BASH_ENV"] = str(script)
    values["ENV"] = str(script)
    return values


__all__ = [
    "PROCESS_KILL_ERROR",
    "PROCESS_KILL_GUIDANCE_VERSION",
    "blocked_process_kill",
    "guarded_environment",
    "install_shell_guard",
    "process_kill_guidance",
    "shell_guard_script",
]
