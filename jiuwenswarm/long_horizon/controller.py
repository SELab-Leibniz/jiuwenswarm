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
from uuid import uuid4


SCHEMA = "jiuwenswarm-long-horizon/v1"
BASE_AGENT_COMMIT = "49c9f5b4c66d193e4168bfa9c82c2df025c773f0"
SOURCE_WORKFLOW_COMMIT = "0561cf0fa584fd56fdcd98c90cca6a5bc1890d95"


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
    localization_timeout_seconds: float = 120
    role_timeout_seconds: float = 3 * 3600
    reserve_seconds: float = 2 * 3600
    cleanup_timeout_seconds: float = 30
    max_rounds: int = 3
    require_pact: bool = True
    recovery_directory: Path | None = None
    recovery_package_sha256: str | None = None
    public_runtime_capabilities: Path | None = None


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
                  timeout_seconds: float | None = None, session_id: str | None = None,
                  tool_policy: str = "configured") -> Any:
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
            "session_continuation": bool(session_id), "requested_session_id": session_id,
            "tool_policy": tool_policy,
        }
        self.controller.summary["worker_calls"].append(call)
        self.controller.persist()
        try:
            kwargs = dict(
                stage=stage, prompt=prompt, workspace=workspace, output_dir=output_dir,
                read_only=read_only, schema=schema, timeout_seconds=timeout,
                session_id=session_id,
            )
            if tool_policy != "configured":
                kwargs["tool_policy"] = tool_policy
            result = await self.worker.run(**kwargs)
        except BaseException as exc:
            result = getattr(exc, "result", None)
            call.update(status=_field(result, "status", "interrupted" if isinstance(exc, asyncio.CancelledError) else "failed"),
                        session_id=_field(result, "session_id"), exit_code=_field(result, "exit_code"),
                        usage=_jsonable(_field(result, "usage", {})),
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
        self.runtime_evidence = ""
        self.recovery_payload: dict[str, Any] | None = None

    async def _checkpoint(self, stage: str, status: str, *, snapshot: Any = None) -> bool:
        """Checkpoint capture failures never invalidate an already completed solver."""
        from .checkpoints import save_stage_checkpoint
        artifacts = [self.output / "requirement.md", self.output / "summary.json"]
        artifacts += [self.output / name for name in ("clarification", "localization", "pact-inputs", "pact-input-generation",
                       "pact/campaign", "pact/result.json", "pact/public-verification", "report.md", "final-response.md",
                       "recovery-origin.json", "recovery-restoration.json", "public-runtime-capabilities.json")
                      if (self.output / name).exists()]
        try:
            path = await asyncio.to_thread(save_stage_checkpoint, self.output, stage=stage, status=status,
                workspace=self.workspace, metadata={"identities": self.summary["identities"],
                    "config_fingerprints": self.summary.get("config_fingerprints", {}),
                    "evaluation_kind": self.summary.get("evaluation_kind"), "resolved": None,
                    "native_session_resume": False}, native_calls=list(self.summary["worker_calls"]),
                artifact_paths=artifacts, snapshot=snapshot)
            self.summary.setdefault("checkpoints", []).append({"stage": stage, "status": status,
                "record": str(path.relative_to(self.output)), "record_sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
            return True
        except Exception as exc:
            failure = {"schema": "jiuwenswarm/stage-checkpoint-failure/v1", "stage": stage,
                       "status": "capture_failed", "solver_status": status, "error_type": type(exc).__name__, "detail": str(exc)[:2000]}
            self.summary.setdefault("checkpoint_failures", []).append(failure)
            try:
                _write_json(self.output / f"checkpoint-failure-{stage}-{uuid4().hex}.json", failure)
            except OSError:
                pass
            return False
        finally:
            self.persist()

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
        if self._integrity_error(exc):
            return False
        return (
            isinstance(exc, (BudgetStop, TimeoutError, *(self.services.budget_errors if self.services else ())))
            or _field(getattr(exc, "result", None), "status") in {"budget_exhausted", "timed_out"}
            or getattr(exc, "code", "") in {"budget_exhausted", "output_budget_exhausted"}
        )

    @staticmethod
    def _wall_timeout(exc: BaseException) -> bool:
        return (isinstance(exc, TimeoutError)
                or _field(getattr(exc, "result", None), "status") in {"timed_out", "timeout"})

    @staticmethod
    def _integrity_error(exc: BaseException) -> bool:
        from .worker import WorkerProtocolError

        return (isinstance(exc, WorkerProtocolError)
                or getattr(exc, "code", "") in {"SNAPSHOT_CHANGED", "RUNTIME_IDENTITY_INVALID"})

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
                if self._budget_error(exc) or self._interruption_error(exc) or self._integrity_error(exc):
                    raise
                self.summary["stages"]["clarification"]["status"] = "degraded"
                from .clarification.types import ClarificationResult, ClarificationSelection
                return ClarificationResult(strategy_version="0561cf0f-jiuwen", revision=1, delta="",
                    selection=ClarificationSelection(), status="degraded", empty_reason="clarification_failed")

        async def localization() -> dict[str, Any]:
            try:
                result = await self._phase("localization", self.services.localize(
                    self.worker, snapshot, requirement, self.output / "localization",
                    timeout_seconds=self.config.localization_timeout_seconds))
                self.summary["stages"]["localization"].update(status=result.get("status", "degraded"), artifact=str(self.output / "localization"))
                return result
            except Exception as exc:
                if self._integrity_error(exc):
                    raise
                if self._wall_timeout(exc) and self.remaining() > 0:
                    self.summary["stages"]["localization"].update(status="degraded", reason="localization_time_budget")
                    return {"status": "degraded", "reason": "localization_time_budget"}
                if self._budget_error(exc) or self._interruption_error(exc) or self._integrity_error(exc):
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
        await self._checkpoint("snapshot", "completed", snapshot=s0)
        await self._phase("s0_recovery", asyncio.to_thread(capture_recovery, self.workspace, self.output / "recovery" / "s0", self.base_commit))
        if self.config.recovery_directory is not None:
            from .recovery import restore_p0_recovery
            payload, receipt = await self._phase("p0_restore", asyncio.to_thread(
                restore_p0_recovery, self.config.recovery_directory, workspace=self.workspace,
                requirement_bytes=(self.output / "requirement.md").read_bytes(),
                expected_sha256=self.config.recovery_package_sha256))
            _write_json(self.output / "recovery-origin.json", payload)
            _write_json(self.output / "recovery-restoration.json", receipt)
            self.summary["recovery_origin"] = {**receipt, "origin_path": "recovery-origin.json", "restoration_path": "recovery-restoration.json"}
            self.summary["stages"]["p0"] = {"status": "restored", "model_execution": "skipped",
                "recovery_package_sha256": self.config.recovery_package_sha256}
            self.final_text = "Restored the verified P0 source working set; no P0 model invocation was made."
        else:
            try:
                # The worker owns this phase's timeout and drains its native process.
                # Only the total workflow deadline may cancel this coroutine.
                baseline = await self._phase("p0", self._solve("p0", requirement, self.config.p0_timeout_seconds))
                self.final_text = str(_field(baseline, "final_text", ""))
            except Exception as exc:
                local_timeout = self._wall_timeout(exc) and self.remaining() > 0
                if (self._budget_error(exc) and not local_timeout) or self._interruption_error(exc) or self._integrity_error(exc):
                    raise
                self.summary["stages"]["p0"].update(status="failed", partial_baseline=True,
                    partial_reason="stage_time_budget" if local_timeout else "native_failure")
                self.final_text = str(_field(getattr(exc, "result", None), "final_text", ""))
        p0 = await self._phase("p0_snapshot", asyncio.to_thread(self.services.capture_workspace, self.workspace, self.output / "snapshots" / "p0"))
        self.summary["snapshots"]["p0"] = self._snapshot_record(p0)
        self.summary["baseline"] = "p0"
        try:
            p0_symbolic_head = _git(self.workspace, "symbolic-ref", "-q", "HEAD").decode().strip()
        except RuntimeError:
            p0_symbolic_head = ""
        p0_recovery = await self._phase("p0_recovery", asyncio.to_thread(capture_recovery, self.workspace, self.output / "recovery" / "p0", self.base_commit))
        await self._checkpoint("initial_implementation", self.summary["stages"]["p0"]["status"], snapshot=p0)
        baseline_text = self.final_text
        clarification, localization = await self._clarify_and_localize(s0, requirement)
        await self._checkpoint("clarification", _field(clarification, "status", "degraded"), snapshot=p0)

        async def prepare_inputs() -> tuple[dict[str, Any], dict[str, Any]]:
            inputs = await self.services.generate_pact_inputs(
                self.worker, s0, requirement, clarification, self.output / "pact-input-generation",
                localization_hints=json.dumps(_jsonable(localization), ensure_ascii=False),
                timeout_seconds=self.config.inputs_timeout_seconds + self.config.reserve_seconds,
                runtime_evidence=self.runtime_evidence,
                checkpoint_identity={"identities": self.summary["identities"], "config_fingerprints": self.summary["config_fingerprints"]})
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
            context = Path(paths["contract_path"]).parent
            (context / "requirement.md").write_bytes((self.output / "requirement.md").read_bytes())
            (context / "clarification.md").write_text(str(_field(clarification, "delta", "")), encoding="utf-8")
            return paths, archived

        paths, archived = None, {}
        try:
            paths, archived = await self._phase("pact_inputs", prepare_inputs(), self.config.inputs_timeout_seconds + self.config.reserve_seconds)
            self.summary["stages"]["pact_inputs"]["artifacts"] = archived
        except Exception as exc:
            if self._budget_error(exc) or self._interruption_error(exc) or self._integrity_error(exc):
                raise
            self.summary["stages"]["pact_inputs"]["required_campaign_unavailable"] = True
        await self._checkpoint("pact_input", self.summary["stages"]["pact_inputs"]["status"], snapshot=p0)

        needs_repair = _field(clarification, "status") == "completed" and bool(_field(clarification, "delta", "").strip())
        if needs_repair:
            repair_prompt = (
                "[LONG_HORIZON_REPAIR]\nThe baseline implementation is already in this workspace. "
                "Inspect the current files and make the changes needed to meet the original requirement. "
                "The original requirement remains authoritative. Clarification and localization are guidance, "
                "not new acceptance criteria; verify localization before editing.\n\n"
                f"ORIGINAL REQUIREMENT\n{requirement}\n\nCLARIFICATION GUIDANCE\n{_field(clarification, 'delta')}"
                f"\n\nUNTRUSTED LOCALIZATION\n{json.dumps(_jsonable(localization), ensure_ascii=False)}"
            )
            try:
                repair = await self._phase("repair", self._solve("repair", repair_prompt, self.config.repair_timeout_seconds))
                self.final_text = str(_field(repair, "final_text", ""))
                self.summary["baseline"] = "p1"
            except Exception as exc:
                if self._budget_error(exc) or self._interruption_error(exc) or self._integrity_error(exc):
                    raise
                await self._checkpoint("repair_failed_candidate", "failed")
                failed = await asyncio.to_thread(capture_recovery, self.workspace, self.output / "recovery" / "failed-repair", self.base_commit)
                if failed["status"] != "captured":
                    raise StageFailure("failed repair could not be preserved before P0 fallback") from exc
                from .recovery import restore_snapshot_in_place
                fallback = await asyncio.to_thread(restore_snapshot_in_place, p0, self.workspace, p0_symbolic_head)
                restored = await asyncio.to_thread(capture_recovery, self.workspace, self.output / "recovery" / "repair-fallback", self.base_commit)
                if restored["status"] != "captured" or restored.get("patch_sha256") != p0_recovery.get("patch_sha256"):
                    raise StageFailure("restored P0 patch differs from the preserved baseline") from exc
                fallback["patch_sha256"] = restored["patch_sha256"]
                _write_json(self.output / "repair-fallback.json", fallback)
                self.summary["stages"]["repair"].update(baseline_restored=True, fallback_receipt="repair-fallback.json")
                self.final_text = baseline_text
                self.summary["baseline"] = "p0"
        else:
            self.summary["stages"]["repair"] = {"status": "skipped", "reason": "clarification_degraded_or_empty", "baseline": "p0"}
        await self._checkpoint("repair", self.summary["stages"]["repair"]["status"], snapshot=p0 if self.summary["baseline"] == "p0" else None)
        if paths is None:
            self.summary.update(status="degraded", incomplete_reason="required_pact_inputs_unavailable")
            return
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
        if bool(self.config.recovery_directory) != bool(self.config.recovery_package_sha256):
            raise ValueError("explicit recovery requires both directory and package SHA-256")
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
        from .contextgraph import selection as memory_selection
        selected_memory = memory_selection()
        self.summary = {
            "schema": SCHEMA, "status": "running", "suite_complete": False,
            "resolved": None, "route": "long_horizon", "router_enabled": False,
            "pact_required": self.config.require_pact, "baseline": None,
            "evaluation_kind": "recovered_attempt_not_fresh_pass_at_1" if self.config.recovery_directory else "fresh_attempt",
            "memory": selected_memory,
            "identities": {"agent_source_commit": implementation_commit, "base_agent_commit": BASE_AGENT_COMMIT,
                           "source_workflow_commit": SOURCE_WORKFLOW_COMMIT, "workspace_base_commit": self.base_commit,
                           "requirement_sha256": hashlib.sha256(requirement_bytes).hexdigest()},
            "budget_policy": _jsonable(dataclasses.asdict(self.config)),
            "config_fingerprints": {"suite_config_sha256": hashlib.sha256(json.dumps(_jsonable(dataclasses.asdict(self.config)), sort_keys=True).encode()).hexdigest()},
            "protocol_differences": [
                "Clarification proposals combine investigation and synthesis with read/grep throughout; bounded one format and one evidence follow-up do not reproduce Chrys's separate tool-disabled proposer synthesis.",
                "Fixed long-horizon entrypoint; no route classifier.",
                ("Read-only frozen ContextGraph through native MCP; no automatic prior injection or writeback."
                 if selected_memory["enabled"] else "Memory disabled; no cross-task graph recall or writeback."),
                "Stages use separate native Jiuwen sessions; bounded structured-output corrections resume the same recorded native session.",
                "Public verifier investigation and synthesis are separate; synthesis uses a verified tool-free native profile.",
                "Base phase budget plus reserve is one cumulative session deadline; correction calls do not reset it or replay the task.",
                "Native P0 phase wall-clock timeout preserves a partial baseline; total deadline, output budget, and interruption stop the workflow.",
                "Failed repair candidates are preserved before verified P0 source, HEAD, index and patch restoration.",
                "The fixed controller launches PACT directly, without Chrys's model delegation and delivery handoff calls.",
                "Explicit P0 recovery reconstructs source in a fresh clean baseline; archived session IDs are evidence, not a model-resume guarantee.",
            ],
            "stages": {}, "worker_calls": [], "snapshots": {},
            "public_verification": {"status": "not_started", "receipt_paths": []},
        }
        self.persist()
        try:
            if self.services is None:
                self.services = SuiteServices.native()
            for name, attribute in (("worker_config_template_sha256", "config_template"), ("worker_binary_sha256", "binary")):
                path = getattr(self.worker.worker, attribute, None)
                if path is not None:
                    self.summary["config_fingerprints"][name] = hashlib.sha256(Path(path).read_bytes()).hexdigest()
            if self.config.public_runtime_capabilities is not None:
                from .clarification.runtime_capabilities import read_public_runtime_capabilities
                self.runtime_evidence = read_public_runtime_capabilities(str(self.config.public_runtime_capabilities))
                path = self.output / "public-runtime-capabilities.json"
                path.write_text(self.runtime_evidence, encoding="utf-8")
                self.summary["config_fingerprints"]["public_runtime_capabilities_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            if self.config.recovery_directory is not None:
                from .recovery import load_p0_recovery, validate_recovery_target
                def admit_recovery() -> dict[str, Any]:
                    payload, original, _p0 = load_p0_recovery(self.config.recovery_directory,
                        requirement_bytes=requirement_bytes, expected_sha256=self.config.recovery_package_sha256)
                    validate_recovery_target(self.workspace, payload, original)
                    return payload
                self.recovery_payload = await self._phase("recovery_admission", asyncio.to_thread(admit_recovery))
            await self._workflow(requirement)
        except asyncio.CancelledError:
            self.summary.update(status="interrupted", incomplete_reason="workflow_interrupted")
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            self.summary.update(status="budget_exhausted" if self._budget_error(exc) else "interrupted" if self._interruption_error(exc) else "failed",
                                incomplete_reason=str(exc)[:2000], error_type=type(exc).__name__)
        finally:
            try:
                self.summary["recovery"] = await asyncio.to_thread(capture_recovery, self.workspace, self.output / "recovery" / "current", self.base_commit, self.config.cleanup_timeout_seconds)
            except Exception as exc:
                self.summary["recovery"] = {"status": "capture_failed", "error_type": type(exc).__name__, "detail": str(exc)[:2000]}
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
            await self._checkpoint("delivery", self.summary["status"])
        return self.summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the fixed native Jiuwen long-horizon suite")
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--prompt-file", required=True, type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path("/agent_out/long-horizon-suite"))
    parser.add_argument("--worker-bin", required=True, type=Path)
    parser.add_argument("--config-template", required=True, type=Path)
    parser.add_argument("--timeout-seconds", type=float, default=48 * 3600)
    parser.add_argument("--recovery-directory", type=Path, default=os.environ.get("JIUWENSWARM_RECOVERY_DIRECTORY"))
    parser.add_argument("--recovery-package-sha256", default=os.environ.get("JIUWENSWARM_RECOVERY_PACKAGE_SHA256"))
    parser.add_argument("--public-runtime-capabilities", type=Path, default=os.environ.get("JIUWENSWARM_PUBLIC_RUNTIME_CAPABILITIES"))
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
        config = SuiteConfig(args.workspace, args.prompt_file, args.output_dir, args.timeout_seconds,
            recovery_directory=args.recovery_directory, recovery_package_sha256=args.recovery_package_sha256,
            public_runtime_capabilities=args.public_runtime_capabilities)
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
