"""ChrysAdapter: run one PACT turn through the ``chrys`` CLI.

Process boundary only — this module never imports a ``chrys`` internal
package (AGENTS.md invariant 7). It writes the prompt to a temp file and
invokes::

    <chrys_bin> run --task <prompt-file> -a <profile> -C <workdir> [-m <model>] --json

then parses stdout as JSON and reads the ``result`` field as the final
turn text (verified against ``chrys/app/cli/run.py:_write_result``, which
emits ``{"session_id": ..., "result": <text>, "duration": ...}`` on
``--json``).

Profile installation
---------------------
chrys's ``-a/--agent`` flag resolves a profile by id, name, or display
name via ``AgentProfileRegistry`` — it does not accept a bare file path
(see ``chrys/harness/session_host.py:_resolve_profile`` and
``chrys/service/profiles/agents/registry.py:resolve_selector``). The
registry loads built-ins plus every YAML under
``<chrys config dir>/agents/`` (``~/.chrys/agents`` on macOS/Linux,
``%APPDATA%/chrys/agents`` on Windows). Because there is no path-based
entry point, this adapter installs its own minimal worker/reviewer
profiles (bundled under ``pact_core/adapters/profiles/``) into that
directory before the first turn, then selects them by name. This is the
most robust of the two available options: it requires no manual setup
step and self-heals if the profile file is ever deleted, at the cost of
one small idempotent write to the chrys user config directory. The
bundled profiles strip all embedded PACT semantics (pact-core's
canonical prompt carries the full task and marker instructions for every
turn) and keep ``approval.default: auto`` with only ``shell``/write
overrides requiring approval — turns still run headless because
``chrys run`` always forces ``ApprovalMode.BYPASS`` regardless of profile
approval config (``chrys/app/cli/run.py:253``).
"""

from __future__ import annotations

import json
import math
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .base import resolve_model
from ..artifacts import atomic_write_text
from ..schemas import AdapterProbe, Role, TurnRequest, TurnResult

_TAIL_CHARS = 4000
_PROFILES_DIR = Path(__file__).parent / "profiles"

_DEFAULT_PROFILE: dict[Role, str] = {
    "worker": "PactCoreWorker",
    "reviewer": "PactCoreReviewer",
}


def _default_chrys_config_dir() -> Path:
    """Best-effort mirror of chrys's own platform config dir detection.

    Mirrors ``chrys/foundation/platform/__init__.py:detect_platform`` without
    importing the chrys package: ``%APPDATA%/chrys`` on Windows, ``~/.chrys``
    everywhere else.
    """
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        return base / "chrys"
    return Path.home() / ".chrys"


def _resolve_binary(binary: str | None) -> str:
    if binary:
        return binary
    env_bin = os.environ.get("PACT_CHRYS_BIN")
    if env_bin:
        return env_bin
    return "chrys"


class ChrysAdapter:
    """Run headless chrys turns for one PACT role."""

    id: str = "chrys"

    def __init__(
        self,
        *,
        role: Role,
        binary: str | None = None,
        profile: str | None = None,
        model: str | None = None,
        profile_dir: Path | None = None,
    ) -> None:
        self.role = role
        self._binary = _resolve_binary(binary)
        self._profile = profile or _DEFAULT_PROFILE[role]
        self._model, self._model_source = resolve_model(model, "PACT_CHRYS_MODEL")
        self._profile_dir = profile_dir or (_default_chrys_config_dir() / "agents")
        self._profile_installed = False

    def _ensure_profile_installed(self) -> None:
        """Copy the bundled profile YAML into chrys's profile directory.

        Idempotent: skips the write when the installed file already has the
        expected content. Called lazily so constructing an adapter never
        touches the filesystem.
        """
        if self._profile_installed:
            return
        source = _PROFILES_DIR / f"{self._profile}.yaml"
        if not source.is_file():
            # A caller passed a custom profile name that isn't bundled here;
            # assume it is already installed (or a chrys builtin) and let
            # chrys report AgentProfileNotFoundError via output_missing.
            self._profile_installed = True
            return
        content = source.read_text(encoding="utf-8")
        target = self._profile_dir / f"{self._profile}.yaml"
        if not target.is_file() or target.read_text(encoding="utf-8") != content:
            atomic_write_text(target, content)
        self._profile_installed = True

    def run_turn(self, request: TurnRequest) -> TurnResult:
        if request.deadline is not None:
            if not math.isfinite(request.deadline):
                return TurnResult(status="spawn_failed", final_text="", output_source="none", exit_code=None,
                                  stderr_tail="deadline must be finite UTC epoch seconds")
            if request.deadline <= time.time():
                return TurnResult(status="timeout", final_text="", output_source="none", exit_code=None,
                                  stderr_tail="budget_exhausted: deadline reached before agent setup")
        self._ensure_profile_installed()

        fd, tmp_name = tempfile.mkstemp(prefix="pact-chrys-prompt-", suffix=".md")
        prompt_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(request.prompt)
            return self._run_process(request, prompt_path)
        finally:
            prompt_path.unlink(missing_ok=True)

    def _run_process(self, request: TurnRequest, prompt_path: Path) -> TurnResult:
        if request.deadline is not None and request.deadline <= time.time():
            return TurnResult(status="timeout", final_text="", output_source="none", exit_code=None,
                              stderr_tail="budget_exhausted: deadline reached before agent spawn")
        argv = [
            self._binary,
            "run",
            "--task",
            str(prompt_path),
            "-a",
            self._profile,
            "-C",
            str(request.workdir),
        ]
        if self._model:
            argv.extend(["-m", self._model])
        argv.append("--json")
        started = time.monotonic()
        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        except OSError as exc:
            return TurnResult(
                status="spawn_failed",
                final_text="",
                output_source="stdout_json",
                exit_code=None,
                stderr_tail=str(exc)[-_TAIL_CHARS:],
                duration_seconds=time.monotonic() - started,
            )

        try:
            timeout = request.timeout_seconds
            if request.deadline is not None:
                timeout = min(timeout, request.deadline - time.time())
                if timeout <= 0:
                    raise subprocess.TimeoutExpired(argv, timeout=0)
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            stdout, stderr = process.communicate()
            return TurnResult(
                status="timeout",
                final_text="",
                output_source="stdout_json",
                exit_code=None,
                stdout_tail=(stdout or "")[-_TAIL_CHARS:],
                stderr_tail=(stderr or "")[-_TAIL_CHARS:],
                duration_seconds=time.monotonic() - started,
            )
        except BaseException:
            # The adapter owns the child lifecycle. In particular, a Ctrl-C
            # handled by the controller must not leave the agent running
            # against a worktree that the controller is about to remove.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                process.communicate(timeout=5.0)
            except BaseException:
                pass
            raise

        duration = time.monotonic() - started
        stdout_tail = (stdout or "")[-_TAIL_CHARS:]
        stderr_tail = (stderr or "")[-_TAIL_CHARS:]
        exit_code = process.returncode

        if exit_code != 0:
            return TurnResult(
                status="output_missing",
                final_text="",
                output_source="stdout_json",
                exit_code=exit_code,
                stdout_tail=stdout_tail,
                stderr_tail=stderr_tail,
                duration_seconds=duration,
            )

        final_text = _extract_result_field(stdout or "")
        if final_text is None:
            return TurnResult(
                status="output_missing",
                final_text="",
                output_source="stdout_json",
                exit_code=exit_code,
                stdout_tail=stdout_tail,
                stderr_tail=stderr_tail,
                duration_seconds=duration,
            )

        return TurnResult(
            status="completed",
            final_text=final_text,
            output_source="stdout_json",
            exit_code=exit_code,
            stdout_tail=stdout_tail,
            stderr_tail=stderr_tail,
            duration_seconds=duration,
        )

    def _declared_provenance(self) -> dict[str, str]:
        """Loop-start declared snapshot: what this adapter was configured
        with, independent of whether the CLI is actually reachable."""
        return {
            "binary_name": Path(self._binary).name,
            "model": self._model or "unavailable",
            "model_source": self._model_source,
            "profile": self._profile,
            "isolation_mode": "subprocess",
        }

    def probe(self) -> AdapterProbe:
        declared = self._declared_provenance()
        try:
            result = subprocess.run(
                [self._binary, "--version"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=30.0,
                check=False,
            )
        except OSError as exc:
            return AdapterProbe(adapter_id=self.id, available=False, detail=str(exc), **declared)
        except subprocess.TimeoutExpired as exc:
            return AdapterProbe(adapter_id=self.id, available=False, detail=str(exc), **declared)

        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()[-_TAIL_CHARS:]
            return AdapterProbe(adapter_id=self.id, available=False, detail=detail, **declared)

        version = (result.stdout or "").strip()
        return AdapterProbe(adapter_id=self.id, available=True, version=version, **declared)


def _extract_result_field(stdout: str) -> str | None:
    """Parse ``chrys run --json`` stdout and return the ``result`` field.

    Returns ``None`` when stdout is not valid JSON, is not a JSON object, or
    lacks a string ``result`` field — all treated as ``output_missing`` by
    the caller.
    """
    stripped = stdout.strip()
    if not stripped:
        return None
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    result = parsed.get("result")
    if not isinstance(result, str):
        return None
    return result


__all__ = ["ChrysAdapter"]
