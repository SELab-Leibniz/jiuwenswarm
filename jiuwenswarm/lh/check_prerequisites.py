# Copyright (c) 2026 Chrys. All rights reserved.
"""Observe prerequisites in Core's isolated check runner before its shell starts.

The runner still owns all command execution, output, timeouts and verdicts.
Core persists the observations in its ordinary candidate-bound check results.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import runpy
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Only execution tools supplied by the runtime. A missing candidate executable
# is implementation evidence and must remain eligible for a Worker.
_TOOLS = {"cargo", "rustc", "python3", "node", "npm", "make", "cmake", "chromium", "bash"}
_STRICT = r"(?:set[ \t]+-(?P<flags>[eu]+)[ \t]*;[ \t]*)?"
_FIRST_TOOL = re.compile(_STRICT + r"(?P<tool>[a-z0-9]+)(?=[ \t\n]|$)")
_PIPEFAIL = re.compile(r"set[ \t]+-(?P<flags>[eu]*)o[ \t]+pipefail[ \t]*(?:;|\n|$)")
_TMP = re.compile(r"(?:^|[;\n])[ \t]*(?:env[ \t]+)?TMPDIR=(/tmp/[A-Za-z0-9_./-]+)(?=[ \t])")


def _leading_tool(script, errexit):
    first = _FIRST_TOOL.match(script)
    if not first or first["tool"] not in _TOOLS:
        return None
    # A fallback, pipeline or command substitution may run the product
    # despite a missing tool. Require simple literal arguments and, for a
    # sequence, an unconditional leading errexit prologue.
    tail = re.split(r"([;\n])", script[first.end() :].rstrip(), maxsplit=1)
    if len(tail) > 1 and not errexit and "e" not in (first["flags"] or ""):
        return None
    if not re.fullmatch(r"(?:[ \t]+[-a-zA-Z0-9_./+:=]+)*(?:[ \t]*>/dev/null)?[ \t]*", tail[0]):
        return None
    return first["tool"]


def _observed_shell(command, opened):
    script = command.lstrip()
    # Only literal shell invocations are eligible for an inner-shell probe.
    nested = re.fullmatch(r"(?:exec )?(/bin/bash|bash) (-l?c) '([^']*)'\s*", script, re.S)
    if nested:
        binary, mode, script = nested.groups()
        if binary == "bash" and os.path.realpath(shutil.which(binary) or "") != os.path.realpath("/bin/bash"):
            return command, ()

    probes = []
    strict = _PIPEFAIL.match(script)
    if strict:
        probes.append(("shell_option", "pipefail", "(set -o pipefail) 2>/dev/null"))
    tool = _leading_tool(script[strict.end() :].lstrip() if strict else script, strict and "e" in strict["flags"])
    if tool:
        probes.append(("tool_lookup", tool, "command -v " + tool + " >/dev/null 2>&1"))
    temp = _TMP.search(script.split("<<", 1)[0])
    if temp and ".." not in Path(temp[1]).parts:
        probes.append(("temp_directory", temp[1], "test -d " + shlex.quote(temp[1])))
    if not probes:
        return command, ()
    marker = tempfile.TemporaryFile()
    fd = marker.fileno()
    if fd > 9 and nested is None:
        # Plain POSIX sh guarantees redirection only through descriptor 9.
        # No lookup evidence is preferable to changing the shell semantics.
        marker.close()
        return command, ()
    # The original shell (including its login files) starts exactly once.
    # Only the trusted prologue can write this private descriptor. Close it
    # before the registered command or any candidate code starts.
    # Evaluate reachable prerequisites in execution order, so an unsupported
    # prologue wins over a missing directory used by a later command.
    clauses = [f"! {predicate}; then printf {i} >&{fd};" for i, (_, _, predicate) in enumerate(probes)]
    prefix = "if " + " elif ".join(clauses) + f" fi; exec {fd}>&-; "
    if nested:
        binary, mode, _script = nested.groups()
        wrapped = shlex.join(["exec", binary, mode, prefix + script])
    else:
        wrapped = prefix + script
    opened.append((probes, marker))
    return wrapped, (fd,)


def _record_completed_check(payload, manifest, index, observation):
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != "pact-runtime/check-results/v1"
        or payload.get("candidate") != manifest["candidate"]
        or payload.get("checks_fingerprint") != manifest["checks_fingerprint"]
    ):
        return index
    rows = payload.get("results")
    if not isinstance(rows, list) or len(rows) != index + 1 or index >= len(manifest["checks"]):
        return index
    row = rows[index]
    registered = manifest["checks"][index]
    identity = ("name", "source", "command", "fingerprint")
    if not isinstance(row, dict) or any(row.get(k) != registered.get(k) for k in identity):
        return index
    code, stderr = row.get("exit_code"), row.get("stderr_tail")
    if (
        observation
        and isinstance(observation[0], dict)
        and row.get("status") == "failed"
        and row["source"] == "derived"
        and type(code) is int
        and isinstance(stderr, str)
    ):
        proof = observation[0]
        kind, value = proof["kind"], proof["value"]
        if (
            (kind == "shell_option" and code == 2 and value in stderr)
            or (kind == "tool_lookup" and code == 127 and value in stderr)
            or (kind == "temp_directory" and "ENOENT" in stderr and "mkdtemp" in stderr and value + "/" in stderr)
        ):
            row["method_prerequisite"] = proof
    # Core may publish a null exit code without launching a child (e.g. a
    # changed definition). Always advance this *slot*, even on duplicate commands.
    return index + 1


def run_observed(runner: Path, manifest_path: Path, manifest_digest: str):
    raw = manifest_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != manifest_digest:
        raise SystemExit("check_manifest_changed")
    manifest = json.loads(raw)
    checks = manifest["checks"]
    index = 0
    observation = []
    original_dumps = json.dumps
    original_popen = subprocess.Popen

    def finish_occurrence(occurrence):
        if occurrence and isinstance(occurrence[0], tuple):
            probes, marker = occurrence.pop()
            with marker:
                try:
                    marker.seek(0)
                    observed = marker.read(2)
                except OSError:
                    return
                for i, (kind, value, _) in enumerate(probes):
                    if observed == str(i).encode("ascii"):
                        occurrence.append({"kind": kind, "value": value})
                        break

    class ObservedPopen(original_popen):
        def __init__(self, args, *extra, **kwargs):
            nonlocal observation
            self._prerequisite_observation = []
            if (
                isinstance(args, str)
                and index < len(checks)
                and checks[index]["source"] == "derived"
                and checks[index]["command"] == args
                and kwargs.get("shell") is True
                and not extra
                and not any(kwargs.get(k) is not None for k in ("cwd", "env", "executable"))
                and not kwargs.get("pass_fds")
                and os.name == "posix"
            ):
                observation = self._prerequisite_observation
                try:
                    args, descriptors = _observed_shell(args, observation)
                except OSError:
                    descriptors = ()
                if descriptors:
                    kwargs["pass_fds"] = descriptors
            super().__init__(args, *extra, **kwargs)

        def wait(self, *args, **kwargs):
            result = super().wait(*args, **kwargs)
            finish_occurrence(self._prerequisite_observation)
            return result

    def append_observation(payload, *args, **kwargs):
        # Both pinned runners publish the completed row before starting the
        # next one. Preserve their streamed output and their atomic write,
        # including when the collection subsequently times out.
        nonlocal index, observation
        following = _record_completed_check(payload, manifest, index, observation)
        if following != index:
            observation = []
            index = following
        return original_dumps(payload, *args, **kwargs)

    sys.argv = [str(runner), str(manifest_path), manifest_digest]
    try:
        subprocess.Popen = ObservedPopen
        json.dumps = append_observation
        runpy.run_path(str(runner), run_name="__main__")
    finally:
        subprocess.Popen = original_popen
        json.dumps = original_dumps
        finish_occurrence(observation)


def observed_command(config, installed_runner):
    """Wrap only the exact Core-generated manifest invocation."""
    manifest_path = config.plan_file.with_suffix(".checks.json")
    if os.name != "posix" or not config.verify_command or not manifest_path.is_file():
        return config.verify_command
    digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    expected = [sys.executable, "-I", str(installed_runner), str(manifest_path), digest]
    if config.verify_command != shlex.join(expected):
        return config.verify_command
    return shlex.join([*expected[:2], str(Path(__file__).resolve()), *expected[2:]])


if __name__ == "__main__":
    run_observed(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3])
