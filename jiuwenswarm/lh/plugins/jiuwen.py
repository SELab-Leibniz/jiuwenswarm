"""Jiuwen execution plugin; all model calls remain in the native worker."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import time
from typing import Any
from uuid import uuid4

from pact_core.runtime import RuntimeContractError

from ..ports import ClarificationContext, ExecutionBinding, PluginSpec
from ..reviewer_privilege import ReviewerLaunchPolicy
from .native import NativeClarification
from .transport_recovery import RecoveringJiuwenWorker, recovery_configuration


class JiuwenPlugin(NativeClarification):
    spec = PluginSpec.for_agent("jiuwenswarm")
    resume_completed_planning = True
    # A separate native model/transport session per original criterion. The
    # controller only enables this for the real Jiuwen native suite.
    public_criterion_concurrency = 4

    def __init__(
        self,
        *,
        worker: Any,
        adapter_factory: Any = None,
        clarification_runner_factory: Any = None,
        transport_recovery: bool = False,
    ) -> None:
        super().__init__(
            "jiuwenswarm.long_horizon.pact", "jiuwenswarm.long_horizon.clarification", clarification_runner_factory
        )
        self._worker = worker
        self._recovering_worker = RecoveringJiuwenWorker(worker) if transport_recovery else None
        self._adapter_factory = adapter_factory

    def evidence_configuration(self) -> dict[str, str]:
        value = super().evidence_configuration()
        return {**value, "native_transport_recovery": recovery_configuration()} if self._recovering_worker else value

    def bind_trial_budget(self, budget: Any) -> None:
        if self._recovering_worker is not None:
            self._recovering_worker.budget = budget

    def transport_recovery_report(self) -> list[dict[str, str]] | None:
        if self._recovering_worker is None:
            return None
        return [
            {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            for path in self._recovering_worker.receipts
        ]

    def configuration(self, profile: str) -> dict[str, object]:
        from jiuwenswarm.long_horizon.pact.configuration import jiuwenswarm_configuration

        return jiuwenswarm_configuration(profile, workflow="benchmark")

    def resolve_execution(self, workspace: Any, args: Any) -> ExecutionBinding:
        from jiuwenswarm.long_horizon.pact.benchmark_native import NativeExecution

        if args.profile_name != "Code":
            raise ValueError("the pinned Jiuwen native adapter supports the Code role profile")
        self._worker.benchmark_source_root = workspace
        resolved = NativeExecution.from_worker(self._worker, args.profile_name)
        return ExecutionBinding(resolved.fingerprint, resolved.summary, resolved, resolved)

    def make_adapter(
        self, role: str, binding: ExecutionBinding, args: Any, *, loop: Any, invocation_id: str, abort: Any
    ) -> Any:
        from jiuwenswarm.long_horizon.pact.benchmark_native import NativeBenchmarkAdapter

        self._remember_execution(binding, args, {"loop": loop, "invocation_id": invocation_id, "abort": abort})

        options = {
            "semantic_role": role,
            "worker": self._recovering_worker
            if args.workflow == "suite" and self._recovering_worker is not None
            else self._worker,
            "outer_loop": loop,
            "invocation_id": invocation_id,
            "output_dir": args.output / "native-roles" / uuid4().hex,
            "abort_event": abort,
            "expected_fingerprint": binding.fingerprint,
        }
        if self._adapter_factory is None:
            # The controller's main Reviewer gets a private executable copy.
            # Read-only side calls select "read_only" on the same args object.
            options["executable_reviewer"] = (
                role == "reviewer"
                and getattr(args, "reviewer_execution", "isolated_executable") == "isolated_executable"
            )
            if role == "reviewer":
                options["reviewer_launch_policy"] = ReviewerLaunchPolicy.from_args(args)
        return (self._adapter_factory or NativeBenchmarkAdapter)(**options)

    def default_runner(self, context: ClarificationContext) -> Any:
        from jiuwenswarm.long_horizon.clarification.model import JiuwenClarificationModel

        if context.deadline is None:
            raise RuntimeContractError("native clarification requires the admitted trial deadline")
        remaining = context.deadline - time.time()
        if remaining <= 0:
            raise self.artifacts.exhausted("clarification has no remaining admitted trial time")
        runner = JiuwenClarificationModel(
            worker=self._worker,
            snapshot=context.snapshot,
            output_dir=context.session_dir / "native-attempts" / uuid4().hex,
            side_call_admission=context.admission,
            timeout_seconds=remaining,
            checkpoint_identity={"scope": "own-work-stages/v1", "work_scope": str(context.session_dir.absolute())},
        )
        runner.public_spec_shim = False
        return runner

