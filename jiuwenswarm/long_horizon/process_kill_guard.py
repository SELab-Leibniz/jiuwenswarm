# Copyright (c) 2026 Chrys. All rights reserved.

"""Reject broad process-kill commands before Jiuwen's shell tool executes them.

The Jiuwen shell tool may run in a frozen source tree, but this companion is
imported by ``native_entry`` before the native CLI.  Patching the tool class
there keeps the check inside the role process without editing the Jiuwen SDK.
"""

from __future__ import annotations

import re
import shlex
from typing import Any, Callable

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


def _option_args(args: list[str]) -> tuple[list[str], bool]:
    positional: list[str] = []
    parent = False
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
        if token.startswith("--parent=") or (token.startswith("-P") and len(token) > 2):
            parent = True
            index += 1
            continue
        if token.startswith("-"):
            index += 1
            continue
        positional.append(token)
        index += 1
    return positional, parent


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


def _xargs_kills(args: list[str]) -> bool:
    tokens = _strip_prefixes(args)
    for index, token in enumerate(tokens):
        if _basename(token) in ("kill", "killall"):
            return True
        if _basename(token) in _SHELLS and index + 1 < len(tokens) and tokens[index + 1] == "-c":
            return blocked_process_kill(" ".join(tokens[index + 2 :])) is not None
    return False


def _command_error(tokens: list[str], *, depth: int) -> str | None:
    tokens = _strip_prefixes(tokens)
    if not tokens:
        return None
    name = _basename(tokens[0])
    args = tokens[1:]
    if name == "pkill":
        _positional, parent = _option_args(args)
        return None if parent else PROCESS_KILL_ERROR
    if name == "killall":
        return PROCESS_KILL_ERROR
    if name == "xargs" and _xargs_kills(args):
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
        _positional, _parent = _option_args(pgrep[1:])
        full = any(arg in ("-f", "--full") or arg.startswith("--full=") for arg in pgrep[1:])
        if full and any(
            _basename(command[0]) == "xargs" and _xargs_kills(command[1:]) for command in commands
        ):
            return PROCESS_KILL_ERROR
    return None


def blocked_process_kill(command: str, *, depth: int = 0) -> str | None:
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
    if re.search(r"\bkill(?:all)?\b[^\n;|&]*\$\(\s*pgrep\b[^\n)]*(?:-f|--full)\b", command):
        return PROCESS_KILL_ERROR
    return None


def _parsed_command(instance: Any, inputs: dict[str, Any]) -> str:
    parse = getattr(instance, "_parse_inputs", None)
    if callable(parse):
        parsed = parse(inputs)
        command = getattr(parsed, "command", "")
        if isinstance(command, str):
            return command
    command = inputs.get("command", "") if isinstance(inputs, dict) else ""
    return command if isinstance(command, str) else ""


def _wrap_invoke(original: Callable[..., Any], output_factory: Callable[..., Any] | None) -> Callable[..., Any]:
    async def invoke(self: Any, inputs: dict[str, Any], **kwargs: Any) -> Any:
        error = blocked_process_kill(_parsed_command(self, inputs))
        if error is not None:
            factory = output_factory
            if factory is None:
                from openjiuwen.harness.tools.base_tool import ToolOutput as factory
            return factory(success=False, error=error)
        return await original(self, inputs, **kwargs)

    invoke.jiuwenswarm_process_kill_guard = True  # type: ignore[attr-defined]
    return invoke


def _wrap_stream(original: Callable[..., Any], output_factory: Callable[..., Any] | None) -> Callable[..., Any]:
    async def stream(self: Any, inputs: dict[str, Any], **kwargs: Any):
        error = blocked_process_kill(_parsed_command(self, inputs))
        if error is not None:
            factory = output_factory
            if factory is None:
                from openjiuwen.harness.tools.base_tool import ToolOutput as factory
            yield factory(success=False, error=error)
            return
        async for item in original(self, inputs, **kwargs):
            yield item

    stream.jiuwenswarm_process_kill_guard = True  # type: ignore[attr-defined]
    return stream


def _patch(tool_class: type, output_factory: Callable[..., Any] | None) -> None:
    if not getattr(tool_class.invoke, "jiuwenswarm_process_kill_guard", False):
        tool_class.invoke = _wrap_invoke(tool_class.invoke, output_factory)
    if not getattr(tool_class.stream, "jiuwenswarm_process_kill_guard", False):
        tool_class.stream = _wrap_stream(tool_class.stream, output_factory)


def install_shell_tool_guard(
    *, tool_class: type | None = None, output_factory: Callable[..., Any] | None = None
) -> bool:
    """Patch the native Bash/PowerShell tools; return whether any class was patched."""
    if tool_class is None:
        try:
            from openjiuwen.harness.tools.shell.bash._tool import BashTool as tool_class
        except ImportError:
            return False
    _patch(tool_class, output_factory)
    try:
        from openjiuwen.harness.tools.shell.powershell._tool import PowerShellTool
    except ImportError:
        PowerShellTool = None
    if PowerShellTool is not None:
        _patch(PowerShellTool, output_factory)
    return True


__all__ = [
    "PROCESS_KILL_ERROR",
    "PROCESS_KILL_GUIDANCE_VERSION",
    "blocked_process_kill",
    "install_shell_tool_guard",
    "process_kill_guidance",
]
