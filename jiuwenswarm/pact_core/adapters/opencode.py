"""OpenCode CLI adapter: subprocess boundary, no OpenCode-internal imports.

Invocation: ``<opencode_bin> run --auto --pure --agent pact-<role> --dir <workdir> --format json [-m <model>]``
with the prompt delivered on stdin (flags verified against the local OpenCode
1.18.14 binary; see ``opencode run --help``). ``--auto`` auto-approves
permissions that are not explicitly denied so the turn can run headless;
``--pure`` isolates external plugins so a user's local plugin set cannot
change PACT semantics.

Environment isolation (AGENTS.md invariant 7 — adapters own host state):
each adapter instance owns a private temp "home" directory and points
OpenCode's ``XDG_CONFIG_HOME`` / ``XDG_DATA_HOME`` / ``XDG_STATE_HOME`` /
``XDG_CACHE_HOME`` / ``TMPDIR`` / ``HOME`` and ``OPENCODE_DB`` at
subdirectories of it,
so parallel loops never share OpenCode databases, sessions, or config.
This is a direct, Python-side port of the environment-variable call paths
in the opencode PACT driver's ``spawnOpenCodeRun`` /
``isolatedOpenCode{Database,StateHome,DataHome,CacheHome,TempHome}``
family; the driver's process-orchestration logic itself is not ported.
``XDG_CONFIG_HOME`` isolation additionally lets this adapter install its own
minimal ``pact-worker`` / ``pact-reviewer`` agent profiles (3.2b) without
touching the user's real OpenCode config.

Output recovery is file-first with a stdout fallback (design §5.2): the
adapter appends a transport epilogue instructing the agent to write its
complete final output to ``.pact-io/<role>-output.md`` relative to the
workdir — this instruction is adapter-owned and never leaks into
``pact_core.prompts`` (AGENTS.md invariant 6). ``.pact-io/**`` is excluded
from checkpoints (see ``pact_core.checkpoint``) so it never pollutes a
promoted diff. The output file is read and deleted immediately after the
turn so each round starts from a clean transport slate regardless of
whatever checkpoint/cleanup happens afterward.

Failure normalization: a non-zero exit is always treated as a failed turn
(``status="output_missing"``, ``final_text=""``) even if the output file was
recovered — OpenCode's own exit code is the more trustworthy signal that
the turn did not complete cleanly, and a partially written output file is
not credible evidence of a real one. The file/stdout tails still land in
``stdout_tail``/``stderr_tail`` for human diagnostics only; they never feed
the gate.
"""

from __future__ import annotations

import json
import math
import os
import re
import signal
import subprocess
import tempfile
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from ._opencode_environment import OpenCodeEnvironment
from ._opencode_events import (
    json_event_lines as _json_event_lines,
    write_normalized_events,
)
from ._opencode_process import (
    OpenCodeProcessSpec,
    OpenCodeSpawnFailure,
    supervise_opencode_process,
)
from ._opencode_result import (
    normalize_process_outcome,
    recover_and_clear_output_file,
)
from .base import resolve_model
from ..review_decision import parse_review_decision
from ..schemas import (
    AdapterProbe,
    ReviewDecisionCapture,
    Role,
    TurnRequest,
    TurnResult,
)

_TAIL_LIMIT = 4000
_PROCESS_GROUP_WAIT_SECONDS = 5.0
_PROCESS_POLL_SECONDS = 1.0
_DEFAULT_NO_PROGRESS_TIMEOUT_SECONDS = 900.0
_DEFAULT_OPENCODE_MODEL = "deepseek/deepseek-v4-flash"
_DEFAULT_OPENCODE_VARIANT = "max"
_TRUSTED_DEEPSEEK_MODELS = {
    _DEFAULT_OPENCODE_MODEL: "DeepSeek V4 Flash 0731",
    "deepseek/deepseek-flash": "DeepSeek Flash",
}
_DEEPSEEK_API_KEY_FILE_ENV = "PACT_OPENCODE_DEEPSEEK_API_KEY_FILE"
_MAX_CAPTURE_BYTES = 16 * 1024 * 1024
_MIN_OPENCODE_VERSION = (1, 18, 14)
_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:[-+].*)?\Z")


def _tail(text: str, limit: int = _TAIL_LIMIT) -> str:
    return text[-limit:] if text else ""


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


class OpenCodeAdapter:
    """One OpenCode CLI role (worker or reviewer), reduced to one-turn semantics."""

    id = "opencode"

    def __init__(
        self,
        role: Role,
        *,
        model: str | None = None,
        variant: str | None = None,
        opencode_bin: str | None = None,
        home_root: Path | None = None,
        no_progress_timeout_seconds: float | None = None,
    ) -> None:
        self.role = role
        self._model, self._model_source = resolve_model(model, "PACT_OPENCODE_MODEL")
        self._variant, self._variant_source = resolve_model(variant, "PACT_OPENCODE_VARIANT")
        if self._model is None:
            self._model = _DEFAULT_OPENCODE_MODEL
            self._model_source = "adapter_default"
        if self._variant is None and self._model in _TRUSTED_DEEPSEEK_MODELS:
            self._variant = _DEFAULT_OPENCODE_VARIANT
            self._variant_source = "adapter_default"
        self._opencode_bin = opencode_bin or os.environ.get("PACT_OPENCODE_BIN") or "opencode"
        self._environment = OpenCodeEnvironment(home_root)
        configured_no_progress: float | str | None = no_progress_timeout_seconds
        if configured_no_progress is None:
            configured_no_progress = os.environ.get("PACT_OPENCODE_NO_PROGRESS_TIMEOUT")
        if configured_no_progress is None:
            configured_no_progress = _DEFAULT_NO_PROGRESS_TIMEOUT_SECONDS
        self._no_progress_config_error: str | None
        try:
            parsed_no_progress = float(configured_no_progress)
        except (TypeError, ValueError):
            parsed_no_progress = _DEFAULT_NO_PROGRESS_TIMEOUT_SECONDS
            self._no_progress_config_error = (
                "PACT_OPENCODE_NO_PROGRESS_TIMEOUT must be a positive finite number"
            )
        else:
            self._no_progress_config_error = None
            if not math.isfinite(parsed_no_progress) or parsed_no_progress <= 0:
                self._no_progress_config_error = (
                    "PACT_OPENCODE_NO_PROGRESS_TIMEOUT must be a positive finite number"
                )
        self._no_progress_timeout_seconds = parsed_no_progress

    def _ensure_home(self) -> Path:
        """Lazily create this instance's private isolated home directory."""
        return self._environment.ensure_home()

    def _isolation_env(self, home: Path) -> dict[str, str]:
        return self._environment.isolation_env(home)


    def _build_env(
        self, home: Path, *, data_home: Path | None = None
    ) -> dict[str, str]:
        env = self._environment.build_env(home, data_home=data_home)
        config_content = self._trusted_config_content()
        if config_content is not None:
            env["OPENCODE_CONFIG_CONTENT"] = config_content
        return env

    def _preflight_env(self, home: Path) -> dict[str, str]:
        """Build an isolated, credential-free environment for debug probes."""
        env = self._environment.preflight_env(home)
        config_content = self._trusted_config_content(include_credential=False)
        if config_content is not None:
            env["OPENCODE_CONFIG_CONTENT"] = config_content
        return env

    def _trusted_config_content(self, *, include_credential: bool = True) -> str | None:
        """Build the adapter-owned DeepSeek config for the isolated child.

        Host OpenCode config is not inherited: it may contain plugins, agents,
        and permissions that change PACT semantics. For the demo's canonical
        DeepSeek model, only a credential *reference* may cross that boundary;
        the provider/model shape itself remains owned by this adapter.
        """
        if self._model not in _TRUSTED_DEEPSEEK_MODELS:
            return None
        assert self._model is not None
        model_id = self._model.split("/", 1)[1]
        options: dict[str, object] = {
            "baseURL": "https://api.deepseek.com",
            "timeout": False,
            "chunkTimeout": 600000,
        }
        key_reference = self._deepseek_api_key_reference() if include_credential else None
        if key_reference is not None:
            options["apiKey"] = key_reference
        config = {
            "$schema": "https://opencode.ai/config.json",
            "provider": {
                "deepseek": {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": "DeepSeek Official",
                    "options": options,
                    "models": {
                        model_id: {
                            "name": _TRUSTED_DEEPSEEK_MODELS[self._model],
                            "reasoning": True,
                            # Auxiliary small-model calls do not necessarily
                            # carry the CLI variant. Keep their model defaults
                            # consistent with the requested reasoning effort.
                            "options": {"reasoningEffort": self._variant},
                            "variants": {
                                "low": {"reasoningEffort": "low"},
                                "high": {"reasoningEffort": "high"},
                                "max": {"reasoningEffort": "max"},
                            },
                        }
                    },
                }
            },
            "model": self._model,
            "small_model": self._model,
        }
        return json.dumps(config, separators=(",", ":"), sort_keys=True)

    def _deepseek_api_key_reference(self) -> str | None:
        explicit_file = os.environ.get(_DEEPSEEK_API_KEY_FILE_ENV)
        if explicit_file:
            return f"{{file:{Path(explicit_file).expanduser().resolve()}}}"
        if os.environ.get("DEEPSEEK_API_KEY"):
            return "{env:DEEPSEEK_API_KEY}"

        config_home = os.environ.get("XDG_CONFIG_HOME")
        host_config_dir = (
            Path(config_home).expanduser() / "opencode"
            if config_home
            else Path.home() / ".config" / "opencode"
        )
        for filename in ("opencode.jsonc", "opencode.json"):
            try:
                host_config = json.loads(
                    (host_config_dir / filename).read_text(encoding="utf-8")
                )
                reference = host_config["provider"]["deepseek"]["options"]["apiKey"]
            except (KeyError, OSError, TypeError, json.JSONDecodeError):
                continue
            if isinstance(reference, str) and (
                reference.startswith("{file:") or reference.startswith("{env:")
            ):
                return reference
        return None

    def _install_agent_profile(self, home: Path) -> None:
        """Write minimal pact-worker/pact-reviewer agent files into the isolated config dir."""
        self._environment.install_agent_profiles(home)

    def _agent_name(self) -> str:
        return f"pact-{self.role}"

    def _build_args(self, workdir: Path) -> list[str]:
        args = [
            self._opencode_bin,
            "run",
            "--auto",
            "--pure",
            "--agent",
            self._agent_name(),
            "--dir",
            str(workdir),
            "--format",
            "json",
        ]
        if self._model:
            args.extend(["-m", self._model])
        if self._variant:
            args.extend(["--variant", self._variant])
        return args

    def _transport_epilogue(self, *, feedback_only: bool = False) -> str:
        """Adapter-owned file-protocol instruction; never part of the canonical prompt."""
        output_contract = (
            "\n\n---\n"
            f"Write your complete final output to the file `.pact-io/{self.role}-output.md`,\n"
            "relative to the current working directory (create the `.pact-io` directory\n"
            "if it does not exist). Only that file's content is read as your output for\n"
            "this round; anything printed elsewhere is ignored.\n"
        )
        if self.role != "reviewer" or feedback_only:
            return output_contract
        return output_contract + (
            "Also write the machine-readable decision to\n"
            "`.pact-io/reviewer-decision.json` as exactly one JSON object. It must\n"
            "contain `verdict` with value `complete`, `continue`, or `failed`. If the\n"
            "review identifies a material route deviation or otherwise concludes that\n"
            "the current route must be reconsidered, `verdict` must be `continue` and\n"
            "the object must also contain exactly one `plan_challenge` object with\n"
            "non-empty `reason` and `gap_signature`\n"
            "strings plus `recommended_action` set to `replan`, `retry`,\n"
            "`request_user`, or `block`. A verdict-only example is\n"
            "`{\"verdict\":\"continue\"}`. A challenge example is\n"
            "`{\"verdict\":\"continue\",\"plan_challenge\":{"
            "\"reason\":\"dependency unavailable\","
            "\"gap_signature\":\"dependency:unavailable\","
            "\"recommended_action\":\"replan\"}}`.\n"
            "Do not add Markdown fences, prose, or other\n"
            "fields to that JSON file. The review file remains the human-readable\n"
            "evidence; the decision file is the control signal.\n"
        )

    def _output_path(self, workdir: Path) -> Path:
        return workdir / ".pact-io" / f"{self.role}-output.md"

    def _review_decision_path(self, workdir: Path) -> Path:
        return workdir / ".pact-io" / "reviewer-decision.json"

    def _capture_review_decision(self, request: TurnRequest) -> ReviewDecisionCapture:
        if self.role != "reviewer" or request.review_mode == "feedback":
            return ReviewDecisionCapture(
                verdict_status="not_applicable",
                plan_challenge_status="not_applicable",
            )
        raw_text, existed = recover_and_clear_output_file(
            self._review_decision_path(request.workdir)
        )
        if raw_text and request.artifact_dir is not None:
            raw_path = request.artifact_dir / "reviewer-decision-raw.txt"
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_text(raw_text, encoding="utf-8")
        return parse_review_decision(raw_text, existed=existed)

    def _trust_decision_for_turn(
        self, capture: ReviewDecisionCapture, *, turn_status: str
    ) -> ReviewDecisionCapture:
        if turn_status == "completed" or capture.verdict_status != "valid":
            return capture
        return ReviewDecisionCapture(
            verdict_status="malformed",
            plan_challenge_status="unavailable",
            raw_text=capture.raw_text,
            error=f"decision is not credible because turn status was {turn_status}",
            plan_challenge_error=(
                "plan challenge is not credible because the reviewer turn did not complete"
            ),
        )


    def _kill_process_group(self, proc: subprocess.Popen[str]) -> bool:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError as exc:
            raise RuntimeError("OpenCode process group could not terminate after SIGKILL") from exc
        try:
            proc.wait(timeout=_PROCESS_GROUP_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            raise RuntimeError("OpenCode process group did not terminate after SIGKILL") from None
        return True

    def run_turn(self, request: TurnRequest) -> TurnResult:
        start = time.monotonic()
        if request.deadline is not None and not math.isfinite(request.deadline):
            return TurnResult(status="spawn_failed", final_text="", output_source="none", exit_code=None,
                              stderr_tail="deadline must be finite UTC epoch seconds")
        if self._no_progress_config_error is not None:
            return TurnResult(
                status="spawn_failed",
                final_text="",
                output_source="none",
                exit_code=None,
                stderr_tail=self._no_progress_config_error,
                duration_seconds=time.monotonic() - start,
            )
        if request.deadline is None and self._no_progress_timeout_seconds >= request.timeout_seconds:
            return TurnResult(
                status="spawn_failed",
                final_text="",
                output_source="none",
                exit_code=None,
                stderr_tail=(
                    "PACT_OPENCODE_NO_PROGRESS_TIMEOUT must be shorter than role timeout"
                ),
                duration_seconds=time.monotonic() - start,
            )

        if request.deadline is not None and request.deadline <= time.time():
            return TurnResult(status="timeout", final_text="", output_source="none", exit_code=None,
                              stderr_tail="budget_exhausted: deadline reached before agent setup",
                              duration_seconds=time.monotonic() - start)
        home = self._ensure_home()
        self._install_agent_profile(home)
        native_store_path: Path | None = None
        events_path: Path | None = None
        if request.artifact_dir is not None:
            request.artifact_dir.mkdir(parents=True, exist_ok=True)
            native_store_path = request.artifact_dir / "opencode-native"
            native_store_path.mkdir(parents=True, exist_ok=True)
            events_path = request.artifact_dir / "agent-events.jsonl"
        timeout_seconds = request.timeout_seconds
        idle_timeout = self._no_progress_timeout_seconds
        if request.deadline is not None:
            elapsed = time.monotonic() - start
            # Supervisor timeout is measured from this turn's original start.
            # Charge setup once while keeping both relative and absolute caps.
            timeout_seconds = min(timeout_seconds, elapsed + request.deadline - time.time())
            remaining = timeout_seconds - elapsed
            if remaining <= 0:
                return TurnResult(status="timeout", final_text="", output_source="none", exit_code=None,
                                  stderr_tail="budget_exhausted: deadline reached before agent spawn",
                                  duration_seconds=time.monotonic() - start)
            idle_timeout = min(idle_timeout, remaining * 0.9)
        outcome = supervise_opencode_process(
            OpenCodeProcessSpec(
                args=self._build_args(request.workdir),
                workdir=request.workdir,
                prompt=request.prompt + self._transport_epilogue(feedback_only=request.review_mode == "feedback"),
                env=self._build_env(home, data_home=native_store_path),
                timeout_seconds=timeout_seconds,
                no_progress_timeout_seconds=idle_timeout,
                capture_limit_bytes=_MAX_CAPTURE_BYTES,
                poll_seconds=_PROCESS_POLL_SECONDS,
            ),
            started_monotonic=start,
            popen=subprocess.Popen,
            monotonic=time.monotonic,
            utc_now=_utc_now,
            parse_json_events=_json_event_lines,
            kill_process_group=self._kill_process_group,
        )
        if isinstance(outcome, OpenCodeSpawnFailure):
            decision_capture = self._trust_decision_for_turn(
                self._capture_review_decision(request), turn_status="spawn_failed"
            )
            if events_path is not None:
                write_normalized_events(events_path, [])
            return TurnResult(
                status="spawn_failed",
                final_text="",
                output_source="none",
                exit_code=None,
                stderr_tail=_tail(outcome.detail),
                duration_seconds=outcome.duration_seconds,
                events_path=events_path,
                native_store_path=native_store_path,
                review_decision=decision_capture,
            )
        if events_path is not None:
            write_normalized_events(events_path, list(outcome.normalized_events))
        result = normalize_process_outcome(
            outcome,
            output_path=self._output_path(request.workdir),
            no_progress_timeout_seconds=idle_timeout,
            native_store_path=native_store_path,
            events_path=events_path,
        )
        capture = self._capture_review_decision(request)
        return replace(
            result,
            review_decision=self._trust_decision_for_turn(
                capture, turn_status=result.status
            ),
        )

    def _declared_provenance(self) -> dict[str, str]:
        """Loop-start declared snapshot: what this adapter was configured
        with, independent of whether the CLI is actually reachable."""
        provider = "unavailable"
        if self._model and "/" in self._model:
            provider = self._model.split("/", maxsplit=1)[0]
        return {
            "binary_name": Path(self._opencode_bin).name,
            "model": self._model or "unavailable",
            "model_source": self._model_source,
            "provider": provider,
            "reasoning_variant": self._variant or "unavailable",
            "reasoning_variant_source": self._variant_source,
            "profile": self._agent_name(),
            "isolation_mode": "isolated-home",
        }

    def probe(self) -> AdapterProbe:
        declared = self._declared_provenance()
        if self._no_progress_config_error is not None:
            return AdapterProbe(
                adapter_id=self.id,
                available=False,
                detail=self._no_progress_config_error,
                **declared,
            )
        try:
            result = subprocess.run(
                [self._opencode_bin, "--version"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return AdapterProbe(adapter_id=self.id, available=False, detail=str(exc), **declared)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
            return AdapterProbe(adapter_id=self.id, available=False, detail=detail, **declared)
        version = result.stdout.strip()
        match = _VERSION_RE.fullmatch(version)
        required = ".".join(str(part) for part in _MIN_OPENCODE_VERSION)
        if match is None:
            return AdapterProbe(
                adapter_id=self.id,
                available=False,
                version=version,
                detail=f"unable to verify OpenCode version; {required} or newer is required",
                **declared,
            )
        parsed = tuple(int(part) for part in match.groups())
        if parsed < _MIN_OPENCODE_VERSION:
            return AdapterProbe(
                adapter_id=self.id,
                available=False,
                version=version,
                detail=f"OpenCode {required} or newer is required for config isolation",
                **declared,
            )
        home = self._ensure_home()
        self._install_agent_profile(home)
        preflight_args = [
            self._opencode_bin,
            "debug",
            "agent",
            self._agent_name(),
            "--pure",
        ]
        try:
            # Keep ``debug agent`` out of potentially enormous experiment
            # trees. OpenCode materializes cwd-dependent external-directory
            # rules in the resolved profile and truncates its JSON output at
            # 64 KiB; a large cwd ancestry can therefore make an otherwise
            # valid headless contract unparsable. Project config remains
            # disabled and the real run still uses the requested workdir.
            with tempfile.TemporaryDirectory(prefix="pact-opencode-preflight-cwd-") as cwd:
                profile_result = subprocess.run(
                    preflight_args,
                    cwd=Path(cwd),
                    env=self._preflight_env(home),
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return AdapterProbe(
                adapter_id=self.id,
                available=False,
                version=version,
                detail=f"headless permission contract failed: {exc}",
                **declared,
            )
        if profile_result.returncode != 0:
            detail = (
                profile_result.stderr.strip()
                or profile_result.stdout.strip()
                or f"exit code {profile_result.returncode}"
            )
            return AdapterProbe(
                adapter_id=self.id,
                available=False,
                version=version,
                detail=f"headless permission contract failed: {detail}",
                **declared,
            )
        try:
            profile = json.loads(profile_result.stdout)
        except json.JSONDecodeError as exc:
            return AdapterProbe(
                adapter_id=self.id,
                available=False,
                version=version,
                detail=f"headless permission contract failed: invalid resolved profile JSON: {exc}",
                **declared,
            )
        if not isinstance(profile, dict) or profile.get("mode") != "primary":
            return AdapterProbe(
                adapter_id=self.id,
                available=False,
                version=version,
                detail="headless permission contract failed: resolved profile is not primary",
                **declared,
            )
        tools = profile.get("tools")
        if not isinstance(tools, dict) or tools.get("task") is not False:
            return AdapterProbe(
                adapter_id=self.id,
                available=False,
                version=version,
                detail="headless permission contract failed: resolved TaskTool must be disabled",
                **declared,
            )
        return AdapterProbe(adapter_id=self.id, available=True, version=version, **declared)
