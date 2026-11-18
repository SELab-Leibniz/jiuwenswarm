"""Native clarification/artifact boundary shared by the two existing plugins."""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
from typing import Any, Callable

from ..ports import Capabilities, ClarificationContext
from ..side_calls import SideCalls
from ..snapshot_resources import SnapshotResources, native_stage_record


class NativeArtifacts:
    def __init__(self, pact_package: str, clarification_package: str) -> None:
        self._evidence = importlib.import_module(f"{pact_package}.long_horizon_evidence")
        self._store = importlib.import_module(f"{clarification_package}.artifacts").ClarificationArtifactStore
        self.exhausted = importlib.import_module(f"{clarification_package}.budget").SideCallBudgetExhausted
        self.stopped = self._evidence.ClarificationStopped
        self._snapshot_resources = SnapshotResources()

    def configure_snapshot_resources(self, *, max_total_bytes: int) -> None:
        self._snapshot_resources = SnapshotResources(max_total_bytes)

    def create_store(self, session_dir: Path) -> Any:
        return self._store(session_dir, 1)

    def stage_record(self, **kwargs: Any) -> Any:
        return native_stage_record(self._evidence, self._snapshot_resources, **kwargs)

    def evidence_checkpoint(self, **kwargs: Any) -> Any:
        return self._evidence.evidence_checkpoint(**kwargs)

    def load_stage_snapshot(self, output: Path, record: dict[str, object]) -> Any:
        return self._evidence.load_stage_snapshot(output, record)

    def read_clarification_bundle(self, turn_root: Path, *, original_task: str) -> Any:
        return self._evidence.read_clarification_bundle(turn_root, original_task=original_task)

    def make_admission(self, budget: Any, **kwargs: Any) -> Any:
        return self._evidence.BenchSideCallAdmission(budget, **kwargs)


class NativeClarification(SideCalls):
    capabilities = Capabilities(True, True, True, True)
    verification_strategy = "frozen-public-checks"

    def __init__(self, pact_package: str, clarification_package: str, runner_factory: Callable | None) -> None:
        super().__init__()
        self.artifacts = NativeArtifacts(pact_package, clarification_package)
        self._service_module = importlib.import_module(f"{clarification_package}.service")
        self._service = self._service_module.ClarificationService
        self._types = importlib.import_module(f"{clarification_package}.types")
        self._revision = importlib.import_module(f"{clarification_package}.types").RequirementRevision
        self._runner_factory = runner_factory
        self._active_clarification: asyncio.Task | None = None

    def degraded_clarification(self, reason: str):
        return self._types.ClarificationResult(
            strategy_version=self._service_module.STRATEGY_VERSION,
            revision=1,
            delta="",
            selection=self._types.ClarificationSelection(),
            status="degraded",
            empty_reason="clarification_failed",
            warnings=("bounded planning: " + reason,),
        )

    async def generate_pact_input(
        self, context: ClarificationContext, result: Any, original_task: str, *, hints: str
    ) -> dict[str, Any]:
        runner = self._runner_factory(context) if self._runner_factory else self.default_runner(context)
        self._active_clarification = asyncio.current_task()
        try:
            pair, _usage = await self._service(runner).generate_pact_input(
                result=result,
                revision=self._revision(number=1, messages=(original_task,)),
                background="",
                snapshot=context.snapshot,
                localization_hints=hints,
                generate_public_verification=(
                    self._side_args.verify_command is None and self.verification_strategy == "frozen-public-checks"
                ),
            )
            payload = {
                "goal_contract": pair.goal_contract.model_dump(mode="json", by_alias=True),
                "initial_plan": pair.initial_plan.model_dump(mode="json", by_alias=True),
            }
            if self._side_args.verify_command is None and pair.public_verification is not None:
                payload["public_verification"] = pair.public_verification.model_dump(mode="json", by_alias=True)
            elif self.verification_strategy == "candidate-review":
                payload["public_verification"] = {
                    "status": "unavailable",
                    "reason": "Deferred until P1 exists: independent Reviewer must propose public behavioral "
                    "checks against the actual candidate. Process obligations need trajectory evidence; "
                    "unobservable obligations stay explicitly unverified. Only Harbor supplies an independent score.",
                    "checks": [],
                }
            return payload
        finally:
            self._active_clarification = None

    async def clarify(self, context: ClarificationContext, original_task: str) -> Any:
        runner = self._runner_factory(context) if self._runner_factory else self.default_runner(context)
        self._active_clarification = asyncio.current_task()
        try:
            return await self._service(runner).clarify(
                revision=self._revision(number=1, messages=(original_task,)), background="", snapshot=context.snapshot
            )
        finally:
            self._active_clarification = None

    async def cancel(self) -> None:
        task = self._active_clarification
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        await self._cancel_side_calls()

    def default_runner(self, context: ClarificationContext) -> Any:
        raise NotImplementedError
