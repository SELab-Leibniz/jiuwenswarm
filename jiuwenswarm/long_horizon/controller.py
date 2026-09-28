"""Fixed long-horizon workflow driven exclusively by native Jiuwen workers.

This module owns workflow state, not benchmark grading.  In particular, a
completed campaign is never converted into a hidden-test ``resolved`` value.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import hashlib
import inspect
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
from typing import Any, Awaitable, Callable


SCHEMA = "jiuwenswarm-long-horizon/v1"
BASE_AGENT_COMMIT = "49c9f5b4c66d193e4168bfa9c82c2df025c773f0"
SOURCE_WORKFLOW_COMMIT = "6fcfb3321892735631b49f33236b1e2a988ded2d"


class BudgetStop(RuntimeError):
    """The live workflow stopped without replaying or grading the attempt."""


class StageFailure(RuntimeError):
    pass


@dataclasses.dataclass(frozen=True)
class SuiteConfig:
    workspace: Path
    prompt_file: Path
    output_dir: Path = Path("/agent_out/long-horizon-suite")
    timeout_seconds: float = 48 * 3600
    p0_timeout_seconds: float = 6 * 3600
    repair_timeout_seconds: float = 6 * 3600
    clarification_timeout_seconds: float = 2 * 3600
    inputs_timeout_seconds: float = 2 * 3600
    localization_timeout_seconds: float = 1800
    role_timeout_seconds: float = 3 * 3600
    reserve_seconds: float = 2 * 3600
    cleanup_timeout_seconds: float = 30
    max_rounds: int = 2
    require_pact: bool = True


@dataclasses.dataclass(frozen=True)
class SuiteServices:
    """Injectable boundaries also permit model-free orchestration tests."""

    capture_workspace: Callable[..., Any]
    snapshot_workspace: Callable[..., Path]
    run_clarification: Callable[..., Awaitable[Any]]
    generate_pact_inputs: Callable[..., Awaitable[Any]]
    save_pact_inputs: Callable[..., Any]
    localize: Callable[..., Awaitable[dict[str, Any]]]
    run_campaign: Callable[..., Awaitable[dict[str, Any]]]
    budget_errors: tuple[type[BaseException], ...] = ()

    @classmethod
    def native(cls) -> "SuiteServices":
        from .clarification import (
            SideCallBudgetExhausted,
            capture_workspace,
            generate_pact_inputs,
            run_clarification,
            save_pact_inputs,
            snapshot_workspace,
        )
        from .localization import LocalizationBudgetExhausted, localize
        from .pact import RoleBudgetExhausted, run_campaign

        return cls(
            capture_workspace, snapshot_workspace, run_clarification,
            generate_pact_inputs, save_pact_inputs, localize, run_campaign,
            (SideCallBudgetExhausted, LocalizationBudgetExhausted, RoleBudgetExhausted),
        )


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _jsonable(dataclasses.asdict(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(_jsonable(value), indent=2, ensure_ascii=False) + "\n")
    temporary.replace(path)


def _field(value: Any, key: str, default: Any = None) -> Any:
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def _git(workspace: Path, *args: str, timeout: float = 20, env: dict[str, str] | None = None) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(workspace), *args], capture_output=True,
        timeout=timeout, env=env, check=False,
    )
    if result.returncode:
        raise RuntimeError(f"git {args[0]} failed: {result.stderr.decode(errors='replace')[-2000:]}")
    return result.stdout


def capture_recovery(workspace: Path, output_dir: Path, base_commit: str, timeout_seconds: float = 30) -> dict[str, Any]:
    """Keep history and the complete candidate without resetting the real index.

    Each subprocess is bounded, failures remain explicit, and no candidate files
    are deleted. Git-ignored runtime inputs are not force-added to the patch.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    record: dict[str, Any] = {"schema": "jiuwenswarm/recovery/v1", "status": "captured", "artifacts": {}}
    errors: list[str] = []
    deadline = time.monotonic() + timeout_seconds

    def git(*args: str, env: dict[str, str] | None = None) -> bytes:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("recovery capture time budget exhausted")
        return _git(workspace, *args, timeout=min(20, remaining), env=env)
    for name, args in (
        ("head.txt", ("rev-parse", "HEAD")),
        ("status.txt", ("status", "--porcelain=v1", "--untracked-files=all")),
        ("history.txt", ("log", "--all", "--format=%H %P %s", "-1000")),
    ):
        try:
            (output_dir / name).write_bytes(git(*args))
            record["artifacts"][name] = str(output_dir / name)
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    try:
        bundle = output_dir / "history.bundle"
        git("bundle", "create", str(bundle), "--all", "HEAD")
        record["artifacts"]["git_bundle"] = str(bundle)
    except Exception as exc:
        errors.append(f"bundle: {exc}")
    try:
        with tempfile.TemporaryDirectory(prefix="jiuwen-capture-", dir=output_dir) as temporary:
            env = dict(os.environ, GIT_INDEX_FILE=str(Path(temporary) / "index"))
            git("read-tree", base_commit, env=env)
            scope = (".", ":(exclude).pact", ":(exclude).pact-io", ":(exclude).semantic-search")
            git("add", "-A", "--", *scope, env=env)
            patch = git("diff", "--cached", "--binary", "--full-index", base_commit, "--", *scope, env=env)
            path = output_dir / "candidate.patch"
            path.write_bytes(patch)
            record["artifacts"]["patch"] = str(path)
            record["patch_sha256"] = hashlib.sha256(patch).hexdigest()
    except Exception as exc:
        errors.append(f"patch: {exc}")
    if errors:
        record.update(status="capture_failed", errors=errors)
    _write_json(output_dir / "receipt.json", record)
    return record


class RecordingWorker:
    """Record actual calls while preserving the native worker's failure semantics."""

    def __init__(self, worker: Any, controller: "SuiteController") -> None:
        self.worker, self.controller = worker, controller

    async def run(self, stage: str, prompt: str, workspace: Path, output_dir: Path,
                  read_only: bool = False, schema: Any = None,
                  timeout_seconds: float | None = None, session_id: str | None = None) -> Any:
        output_dir = Path(output_dir).resolve()
        if not output_dir.is_relative_to(self.controller.output):
            raise ValueError("worker artifacts must be inside the suite output directory")
        remaining = self.controller.remaining()
        if remaining <= 0:
            raise BudgetStop("total workflow time budget exhausted before worker admission")
        timeout = min(timeout_seconds if timeout_seconds is not None else remaining, remaining)
        call = {
            "stage": stage, "output_dir": str(output_dir),
            "workspace": str(workspace), "read_only": read_only,
            "timeout_seconds": timeout, "status": "running",
        }
        self.controller.summary["worker_calls"].append(call)
        self.controller.persist()
        try:
            result = await self.worker.run(
                stage=stage, prompt=prompt, workspace=workspace, output_dir=output_dir,
                read_only=read_only, schema=schema, timeout_seconds=timeout,
                session_id=session_id,
            )
        except BaseException as exc:
            result = getattr(exc, "result", None)
            call.update(status=_field(result, "status", "interrupted" if isinstance(exc, asyncio.CancelledError) else "failed"),
                        session_id=_field(result, "session_id"), exit_code=_field(result, "exit_code"),
                        error_type=type(exc).__name__, error=str(exc)[:2000])
            self.controller.persist()
            raise
        call.update(status=_field(result, "status"), session_id=_field(result, "session_id"),
                    exit_code=_field(result, "exit_code"), usage=_jsonable(_field(result, "usage")))
        self.controller.persist()
        return result


class SuiteController:
    def __init__(self, config: SuiteConfig, worker: Any, services: SuiteServices | None = None) -> None:
        self.config = config
        self.workspace = config.workspace.resolve()
        self.output = config.output_dir.resolve()
        self.services = services
        self.worker = RecordingWorker(worker, self)
        self.started = 0.0
        self.base_commit = ""
        self.summary: dict[str, Any] = {}
        self.final_text = ""

    def remaining(self) -> float:
        return max(0.0, self.config.timeout_seconds - (time.monotonic() - self.started))

    def persist(self) -> None:
        self.summary["elapsed_seconds"] = round(time.monotonic() - self.started, 3)
        _write_json(self.output / "summary.json", self.summary)

    def _snapshot_record(self, snapshot: Any) -> dict[str, Any]:
        assert self.services is not None
        return {
            **{name: _field(snapshot, name) for name in ("snapshot_id", "artifact_root", "manifest_hash", "total_bytes", "entry_count")},
            "view_root": str(self.services.snapshot_workspace(snapshot)),
        }

    def _budget_error(self, exc: BaseException) -> bool:
        return (
            isinstance(exc, (BudgetStop, TimeoutError, *(self.services.budget_errors if self.services else ())))
            or _field(getattr(exc, "result", None), "status") in {"budget_exhausted", "timed_out"}
            or getattr(exc, "code", "") in {"budget_exhausted", "output_budget_exhausted"}
        )

    @staticmethod
    def _interruption_error(exc: BaseException) -> bool:
        return (isinstance(exc, asyncio.CancelledError)
                or _field(getattr(exc, "result", None), "status") in {"cancelled", "interrupted"}
                or getattr(exc, "code", "") == "INTERRUPTED")

    async def _cancel(self, tasks: list[asyncio.Task[Any]]) -> None:
        for task in tasks:
            if not task.done():
                task.cancel()
        done, pending = await asyncio.wait(tasks, timeout=self.config.cleanup_timeout_seconds)
        for task in done:
            if not task.cancelled():
                task.exception()
        if pending:
            self.summary.setdefault("cleanup", []).append({"status": "incomplete", "pending_tasks": len(pending)})

    async def _phase(self, name: str, operation: Awaitable[Any], timeout: float | None = None) -> Any:
        stage = self.summary["stages"][name] = {"status": "running", "started_seconds": round(time.monotonic() - self.started, 3)}
        self.persist()
        task = asyncio.create_task(operation)
        try:
            allowance = self.remaining() if timeout is None else min(timeout, self.remaining())
            done, _ = await asyncio.wait([task], timeout=max(0, allowance))
            if not done:
                await self._cancel([task])
                raise BudgetStop(f"{name} exhausted its live time budget")
            result = task.result()
            stage["status"] = "completed"
            return result
        except BaseException as exc:
            if not task.done():
                await self._cancel([task])
            stage.update(status="budget_exhausted" if self._budget_error(exc) else "interrupted" if self._interruption_error(exc) else "failed",
                         error_type=type(exc).__name__, detail=str(exc)[:2000])
            raise
        finally:
            stage["finished_seconds"] = round(time.monotonic() - self.started, 3)
            self.persist()

    async def _solve(self, stage: str, prompt: str, limit: float) -> Any:
        result = await self.worker.run(stage, prompt, self.workspace, self.output / "workers" / stage,
                                       timeout_seconds=limit + self.config.reserve_seconds)
        status = _field(result, "status")
        if status in {"budget_exhausted", "timed_out"}:
            raise BudgetStop(f"{stage}: {status}")
        if status not in {"completed", "succeeded", "success"} or _field(result, "exit_code", 0) not in {0, None}:
            raise StageFailure(f"{stage} native worker returned {status!r}")
        return result

    async def _clarify_and_localize(self, snapshot: Any, requirement: str) -> tuple[Any, dict[str, Any]]:
        assert self.services is not None
        async def clarify() -> Any:
            try:
                result = await self._phase("clarification", self.services.run_clarification(
                    self.worker, snapshot, requirement, self.output / "clarification",
                    timeout_seconds=self.config.clarification_timeout_seconds + self.config.reserve_seconds),
                    self.config.clarification_timeout_seconds + self.config.reserve_seconds)
                self.summary["stages"]["clarification"].update(status=_field(result, "status", "degraded"),
                                                                    empty_reason=_field(result, "empty_reason"))
                return result
            except Exception as exc:
                if self._budget_error(exc) or self._interruption_error(exc):
                    raise
                self.summary["stages"]["clarification"]["status"] = "degraded"
                return None

        async def localization() -> dict[str, Any]:
            try:
                result = await self._phase("localization", self.services.localize(
                    self.worker, snapshot, requirement, self.output / "localization",
                    timeout_seconds=self.config.localization_timeout_seconds), self.config.localization_timeout_seconds)
                self.summary["stages"]["localization"].update(status=result.get("status", "degraded"), artifact=str(self.output / "localization"))
                return result
            except Exception as exc:
                if self._budget_error(exc) or self._interruption_error(exc):
                    raise
                self.summary["stages"]["localization"]["status"] = "degraded"
                return {"status": "degraded", "reason": str(exc)[:2000]}

        tasks = [asyncio.create_task(clarify()), asyncio.create_task(localization())]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            for task in done:
                if task.exception() is not None:
                    if pending:
                        await self._cancel(list(pending))
                    raise task.exception()
            return tasks[0].result(), tasks[1].result()
        finally:
            await self._cancel(tasks)

    async def _workflow(self, requirement: str) -> None:
        assert self.services is not None
        s0 = await self._phase("s0", asyncio.to_thread(self.services.capture_workspace, self.workspace, self.output / "snapshots" / "s0"))
        self.summary["stages"]["s0"].update(artifact=str(self.output / "snapshots" / "s0"), view_root=str(self.services.snapshot_workspace(s0)))
        self.summary["snapshots"]["s0"] = self._snapshot_record(s0)
        await self._phase("s0_recovery", asyncio.to_thread(capture_recovery, self.workspace, self.output / "recovery" / "s0", self.base_commit))
        try:
            baseline = await self._phase("p0", self._solve("p0", requirement, self.config.p0_timeout_seconds),
                                         self.config.p0_timeout_seconds + self.config.reserve_seconds)
            self.final_text = str(_field(baseline, "final_text", ""))
        except Exception as exc:
            if self._budget_error(exc) or self._interruption_error(exc):
                raise
            # The original workflow can clarify and repair an incomplete baseline.
            self.summary["stages"]["p0"]["partial_baseline"] = True
            self.final_text = str(_field(getattr(exc, "result", None), "final_text", ""))
        p0 = await self._phase("p0_snapshot", asyncio.to_thread(self.services.capture_workspace, self.workspace, self.output / "snapshots" / "p0"))
        self.summary["snapshots"]["p0"] = self._snapshot_record(p0)
        self.summary["baseline"] = "p0"
        await self._phase("p0_recovery", asyncio.to_thread(capture_recovery, self.workspace, self.output / "recovery" / "p0", self.base_commit))
        clarification, localization = await self._clarify_and_localize(s0, requirement)
        if clarification is None or _field(clarification, "status") != "completed" or not _field(clarification, "delta", "").strip():
            self.summary.update(status="degraded", baseline="p0", incomplete_reason="clarification_degraded_p0_preserved")
            return
        repair_prompt = (
            "[LONG_HORIZON_REPAIR]\nThe baseline implementation is already in this workspace. "
            "Inspect the current files and make the changes needed to meet the original requirement. "
            "The original requirement remains authoritative. Clarification and localization are guidance, "
            "not new acceptance criteria; verify localization before editing.\n\n"
            f"ORIGINAL REQUIREMENT\n{requirement}\n\nCLARIFICATION GUIDANCE\n{_field(clarification, 'delta')}"
            f"\n\nUNTRUSTED LOCALIZATION\n{json.dumps(_jsonable(localization), ensure_ascii=False)}"
        )
        repair = await self._phase("repair", self._solve("repair", repair_prompt, self.config.repair_timeout_seconds),
                                   self.config.repair_timeout_seconds + self.config.reserve_seconds)
        self.final_text = str(_field(repair, "final_text", ""))
        self.summary["baseline"] = "p1"
        async def prepare_inputs() -> tuple[dict[str, Any], dict[str, Any]]:
            inputs = await self.services.generate_pact_inputs(
                self.worker, s0, requirement, clarification, self.output / "pact-input-generation",
                localization_hints=json.dumps(_jsonable(localization), ensure_ascii=False),
                timeout_seconds=self.config.inputs_timeout_seconds + self.config.reserve_seconds)
            paths = self.services.save_pact_inputs(inputs, self.output / "pact-inputs")
            if inspect.isawaitable(paths):
                paths = await paths
            archived: dict[str, Any] = {}
            for name in ("contract_path", "plan_path", "public_verification_path"):
                value = paths.get(name)
                if value is None:
                    raise StageFailure(f"required PACT input unavailable: {name}")
                path = Path(value).resolve()
                if not path.is_relative_to(self.output) or not path.is_file():
                    raise StageFailure(f"invalid PACT input artifact: {name}")
                archived[name] = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            return paths, archived

        paths, archived = await self._phase("pact_inputs", prepare_inputs(), self.config.inputs_timeout_seconds + self.config.reserve_seconds)
        self.summary["stages"]["pact_inputs"]["artifacts"] = archived
        # Preserve the exact requirement and clearly subordinate guidance beside
        # the governed inputs so every native PACT role has the same authority.
        input_context = Path(paths["contract_path"]).parent
        (input_context / "requirement.md").write_bytes((self.output / "requirement.md").read_bytes())
        (input_context / "clarification.md").write_text(str(_field(clarification, "delta", "")), encoding="utf-8")
        self.summary["public_verification"] = {"status": "prepared", "spec_path": archived["public_verification_path"]["path"],
                                               "spec_sha256": archived["public_verification_path"]["sha256"], "receipt_paths": []}
        campaign = await self._phase("pact", self.services.run_campaign(
            self.worker, self.workspace, Path(paths["contract_path"]), Path(paths["plan_path"]),
            Path(paths["public_verification_path"]), self.output / "pact",
            max_rounds=self.config.max_rounds, role_timeout_seconds=self.config.role_timeout_seconds + self.config.reserve_seconds))
        self.summary["stages"]["pact"].update(status=campaign.get("status", "failed"), result=_jsonable(campaign))
        verification = campaign.get("public_verification", {})
        self.summary["public_verification"].update(_jsonable(verification))
        if campaign.get("status") == "budget_exhausted":
            raise BudgetStop("PACT role budget exhausted")
        receipts = verification.get("receipt_paths", [])
        receipt_files = [Path(path) if Path(path).is_absolute() else self.output / path for path in receipts]
        receipt_files_exist = bool(receipt_files) and all(path.is_file() and path.resolve().is_relative_to(self.output) for path in receipt_files)
        if hashlib.sha256(Path(paths["public_verification_path"]).read_bytes()).hexdigest() != archived["public_verification_path"]["sha256"]:
            raise StageFailure("accepted public verification bytes changed during PACT")
        if verification.get("spec_sha256") != archived["public_verification_path"]["sha256"]:
            raise StageFailure("PACT receipt spec identity differs from generated public verification")
        if campaign.get("status") != "completed" or verification.get("status") != "executed" or not receipt_files_exist:
            self.summary.update(status="degraded", incomplete_reason="required_pact_campaign_incomplete")
            return
        self.summary.update(status="completed", suite_complete=True)

    async def run(self) -> dict[str, Any]:
        timeouts = [value for name, value in dataclasses.asdict(self.config).items() if name.endswith("seconds")]
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0 for value in timeouts):
            raise ValueError("budgets must be finite nonnegative numbers")
        if self.config.timeout_seconds <= 0 or self.config.cleanup_timeout_seconds <= 0:
            raise ValueError("workflow and cleanup timeouts must be positive")
        if not self.workspace.is_dir():
            raise ValueError("workspace must be an existing directory")
        if self.output == self.workspace or self.output.is_relative_to(self.workspace):
            raise ValueError("suite output must be outside the candidate workspace")
        if self.output.exists() and any(self.output.iterdir()):
            raise ValueError("suite output must be fresh; automatic cross-process resume is unsupported")
        requirement_bytes = self.config.prompt_file.read_bytes()
        requirement = requirement_bytes.decode("utf-8")
        if not requirement.strip():
            raise ValueError("requirement must not be empty")
        self.base_commit = _git(self.workspace, "rev-parse", "HEAD").decode().strip()
        self.output.mkdir(parents=True, exist_ok=True)
        self.started = time.monotonic()
        code_root = Path(__file__).resolve().parents[2]
        try:
            implementation_commit = _git(code_root, "rev-parse", "HEAD").decode().strip()
        except Exception:
            implementation_commit = "unknown"
        implementation_commit = os.environ.get("JIUWENSWARM_SUITE_COMMIT", os.environ.get("JIUWENSWARM_AGENT_COMMIT", implementation_commit))
        (self.output / "requirement.md").write_bytes(requirement_bytes)
        self.summary = {
            "schema": SCHEMA, "status": "running", "suite_complete": False,
            "resolved": None, "route": "long_horizon", "router_enabled": False,
            "pact_required": self.config.require_pact, "baseline": None,
            "memory": {"enabled": False, "mode": "disabled", "writeback": False},
            "identities": {"agent_source_commit": implementation_commit, "base_agent_commit": BASE_AGENT_COMMIT,
                           "source_workflow_commit": SOURCE_WORKFLOW_COMMIT, "workspace_base_commit": self.base_commit,
                           "requirement_sha256": hashlib.sha256(requirement_bytes).hexdigest()},
            "budget_policy": _jsonable(dataclasses.asdict(self.config)),
            "protocol_differences": [
                "Fixed long-horizon entrypoint; no route classifier.",
                "Memory disabled; no cross-task graph recall or writeback.",
                "Each stage uses an independent native Jiuwen session; no cross-process continuation is implemented.",
                "Same-session malformed-output and evidence continuations are unsupported; no fresh-session replay substitutes for them.",
                "Base phase budget plus reserve is one live call; no model replay at the reserve boundary.",
                "A timed-out P0 suspends with recovery artifacts instead of admitting a new phase.",
                "A failed repair retains both P0 and the live candidate; no unverified Git rollback is claimed.",
            ],
            "stages": {}, "worker_calls": [], "snapshots": {},
            "public_verification": {"status": "not_started", "receipt_paths": []},
        }
        self.persist()
        try:
            if self.services is None:
                self.services = SuiteServices.native()
            await self._workflow(requirement)
        except asyncio.CancelledError:
            self.summary.update(status="interrupted", incomplete_reason="workflow_interrupted")
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            self.summary.update(status="budget_exhausted" if self._budget_error(exc) else "interrupted" if self._interruption_error(exc) else "failed",
                                incomplete_reason=str(exc)[:2000], error_type=type(exc).__name__)
        finally:
            self.summary["recovery"] = await asyncio.to_thread(capture_recovery, self.workspace, self.output / "recovery" / "current", self.base_commit, self.config.cleanup_timeout_seconds)
            self.summary["resolved"] = None
            self.summary["automatic_resume_supported"] = False
            self.summary["stages"]["report"] = {"status": "completed", "artifact": str(self.output / "report.md")}
            report = (
                f"# Jiuwen long-horizon suite\n\nStatus: {self.summary['status']}\n\n"
                f"Baseline: {self.summary.get('baseline') or 'incomplete'}\n\n"
                "The independent benchmark verifier owns resolved; this workflow reports no hidden-test score.\n\n"
                f"{self.summary.get('incomplete_reason', '')}\n\n## Last solver response\n\n{self.final_text}\n"
            )
            (self.output / "report.md").write_text(report, encoding="utf-8")
            (self.output / "final-response.md").write_text(self.final_text, encoding="utf-8")
            self.persist()
        return self.summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the fixed native Jiuwen long-horizon suite")
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--prompt-file", required=True, type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path("/agent_out/long-horizon-suite"))
    parser.add_argument("--worker-bin", required=True, type=Path)
    parser.add_argument("--config-template", required=True, type=Path)
    parser.add_argument("--timeout-seconds", type=float, default=48 * 3600)
    args = parser.parse_args(argv)
    from .worker import JiuwenWorker

    async def execute() -> dict[str, Any]:
        task = asyncio.current_task()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, task.cancel)
            except NotImplementedError:
                pass
        config = SuiteConfig(args.workspace, args.prompt_file, args.output_dir, args.timeout_seconds)
        worker = JiuwenWorker(binary=args.worker_bin, config_template=args.config_template,
                              source_commit=os.environ.get("JIUWENSWARM_SUITE_COMMIT", os.environ.get("JIUWENSWARM_AGENT_COMMIT", BASE_AGENT_COMMIT)))
        return await SuiteController(config, worker).run()

    try:
        result = asyncio.run(execute())
    except Exception as exc:
        print(json.dumps({"schema": SCHEMA, "status": "failed", "error_type": type(exc).__name__, "detail": str(exc)}))
        return 1
    print(json.dumps({"schema": SCHEMA, "status": result["status"], "summary_path": str(args.output_dir / "summary.json")}))
    return 0 if result["status"] == "completed" else 124 if result["status"] == "budget_exhausted" else 130 if result["status"] == "interrupted" else 1
