# Copyright (c) 2026 Chrys. All rights reserved.
# Adapted from Chrys 3f78185e298800b3e3f735f932a7ee2abb7bc25f; all inference uses native Jiuwen.

"""Trusted original-task long-horizon benchmark entry: ``python -m jiuwenswarm.long_horizon.pact.long_horizon_benchmark``.

One invocation owns one explicit Work operation on a fixed trial:

1. Create Work from the original task (verbatim Goal) and the fixed BenchBudget
   on S0 before any model call. S0 is Core's initial candidate, exported and
   frozen as a snapshot.
2. Run exactly one initial Worker (P0) in Core's ``begin_execution`` checkout
   and save its candidate. P0 is never replayed.
3. Clarify the original task against the frozen S0 only; every clarification
   side ``agent.run`` is admitted into the same trial budget before the call.
   The validated ``clarified-requirement.md`` becomes the acceptance brief.
4. Verify and review the saved P0 through the BenchDriver ``review_pending``
   progress record, then continue with ordinary BenchDriver Workers.
5. Export Core's latest candidate (accepted or not) and publish
   ``OUTPUT/selection.json`` (``jiuwenswarm-pact/benchmark-result/v1``). Independent
   grading is always ``not_run`` here.

Core owns verification, Gate, acceptance and delivery. This module never
generates public verification, a Goal Contract/Initial Plan or Missions, and
never sets ``allow_unverified``.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import logging
import math
import os
import re
import signal
import sys
import threading
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from pact_core.adapters.base import TurnRequest
from pact_core.artifacts import read_json
from pact_core.benchmark import review_progress
from pact_core.benchmark.budget import BenchBudget, BudgetedAdapter, BudgetWindow
from pact_core.benchmark.driver import BenchDriver
from pact_core.benchmark.types import BenchConfig, BenchResult
from pact_core.kernel import default_worktree_root
from pact_core.prompts import build_runtime_handoff_instructions
from pact_core.runtime import AcceptancePolicy, RuntimeContractError, RuntimeStateError, WorkRuntime, WorkSnapshot
from pact_core.runtime.export import ExportReceipt, LatestCandidateExport, verify_export
from pact_core.runtime.text_plan import read_text_plan

from jiuwenswarm.long_horizon.pact.benchmark import BenchmarkBinding, record_benchmark_result
from jiuwenswarm.long_horizon.pact.benchmark_native import record_work_budget
from jiuwenswarm.long_horizon.pact.configuration import jiuwenswarm_configuration
from jiuwenswarm.long_horizon.pact.long_horizon_evidence import (
    CLARIFIER_ADAPTER_ID,
    RESULT_SCHEMA,
    BenchSideCallAdmission,
    ClarificationStopped,
    contained,
    evidence_checkpoint,
    load_stage_snapshot,
    publish_selection,
    read_clarification_bundle,
    receipt_payload,
    sha256_bytes,
    stage_record,
    validate_output_root,
)
from jiuwenswarm.long_horizon.clarification.artifacts import ClarificationArtifactStore
from jiuwenswarm.long_horizon.clarification.budget import SideCallAdmission, SideCallBudgetExhausted
from jiuwenswarm.long_horizon.clarification.model import JiuwenClarificationModel, ClarificationModelRunner
from jiuwenswarm.long_horizon.clarification.service import ClarificationService
from jiuwenswarm.long_horizon.clarification.types import RequirementRevision

if TYPE_CHECKING:
    from jiuwenswarm.long_horizon.clarification.snapshot import WorkspaceSnapshot

logger = logging.getLogger(__name__)

INPUT_KEY = "jiuwenswarm_long_horizon_benchmark"
PROGRESS_KEY = "jiuwenswarm_long_horizon_progress"
INPUT_SCHEMA = "jiuwenswarm-pact/long-horizon-benchmark-input/v1"
PROGRESS_SCHEMA = "jiuwenswarm-pact/long-horizon-benchmark-progress/v1"
_BENCH_PROGRESS_SCHEMA = "pact-core/benchmark-progress/v2"
_REQUEST_PREFIX = "jiuwenswarm-long-horizon-benchmark"
_BRIEF_SOURCE = "jiuwenswarm long-horizon clarification of the original task against frozen S0"

Phase = Literal[
    "created",
    "s0_frozen",
    "p0_started",
    "p0_saved",
    "clarification_started",
    "brief_binding",
    "brief_bound",
    "first_review_started",
    "first_review_done",
]
_PHASES: tuple[Phase, ...] = (
    "created",
    "s0_frozen",
    "p0_started",
    "p0_saved",
    "clarification_started",
    "brief_binding",
    "brief_bound",
    "first_review_started",
    "first_review_done",
)


class BenchmarkFailure(RuntimeError):
    """An internal failure of this invocation, distinct from export or external grade."""


@dataclass(frozen=True, slots=True)
class BenchmarkArguments:
    """Trusted host inputs. The same values are required on every explicit resume."""

    task: Path
    workdir: Path
    output: Path
    task_id: str
    trial_id: str
    budget_seconds: float = 3600.0
    max_role_calls: int | None = 32
    max_turns: int = 3
    turn_timeout_seconds: float = 3600.0
    review_interval: int = 2
    review_each_round: bool = False
    window_seconds: float | None = None
    verify_command: str | None = None
    profile_name: str = "Code"
    model: str | None = "bench"
    worktree_root: Path | None = None
    work_id: str | None = None
    expected_revision: int | None = None

    def __post_init__(self) -> None:
        if (self.work_id is None) != (self.expected_revision is None):
            raise RuntimeContractError("explicit resume requires both --work-id and --expected-revision")
        if isinstance(self.max_turns, bool) or not isinstance(self.max_turns, int) or self.max_turns < 1:
            raise RuntimeContractError("max_turns must be a positive integer")
        if self.window_seconds is not None and (not math.isfinite(self.window_seconds) or self.window_seconds <= 0):
            raise RuntimeContractError("window_seconds must be positive and finite")
        if self.verify_command is not None and not self.verify_command.strip():
            raise RuntimeContractError("verify command must be nonempty")

    def config(self) -> BenchConfig:
        return BenchConfig(
            task_id=self.task_id,
            trial_id=self.trial_id,
            budget_seconds=float(self.budget_seconds),
            max_role_calls=self.max_role_calls,
            turn_timeout_seconds=float(self.turn_timeout_seconds),
            review_interval=self.review_interval,
            review_each_round=self.review_each_round,
        )

    @property
    def request_id(self) -> str:
        return f"{_REQUEST_PREFIX}:{self.task_id}:{self.trial_id}"

    @property
    def derived_work_id(self) -> str:
        return "jiuwenswarm-pact-" + hashlib.sha256(self.request_id.encode()).hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class ClarificationContext:
    """Inputs for one clarification model runner, never containing P0."""

    profile: ModelProfile
    snapshot: WorkspaceSnapshot
    session_id: str
    session_dir: Path
    admission: SideCallAdmission


ClarificationRunnerFactory = Callable[[ClarificationContext], ClarificationModelRunner]


def _default_clarification_runner(context: ClarificationContext) -> ClarificationModelRunner:
    return JiuwenClarificationModel(worker=context.profile.worker, snapshot=context.snapshot,
        output_dir=context.session_dir, side_call_admission=context.admission)


@dataclass(slots=True)
class _Invocation:
    """Mutable facts of this invocation for the report; Core state stays authoritative."""

    invocation_id: str
    state: WorkSnapshot | None = None
    stop_reason: str = "not_started"
    failure: dict[str, object] | None = None
    budget_detail: str | None = None
    p0_ran: bool = False
    clarification_calls: list[str] = field(default_factory=list)
    driver_result: BenchResult | None = None


@dataclass(frozen=True, slots=True)
class BenchmarkOutcome:
    """What the CLI prints; ``selection`` is the only machine contract."""

    selection: Path | None
    internal_failure: bool
    stop_reason: str
    work_id: str | None


async def _ignore_update(_update: object) -> None:
    return


class LongHorizonBenchmark:
    """Own one benchmark invocation, its Core thread and two role adapters."""

    def __init__(
        self,
        args: BenchmarkArguments,
        *,
        worker: Any,
        adapter_factory: RoleAdapterFactory | None = None,
        execution_configuration_resolver: ExecutionConfigurationResolver | None = None,
        clarification_runner_factory: ClarificationRunnerFactory | None = None,
    ) -> None:
        self._args = args
        self._worker = worker
        self._adapter_factory = adapter_factory
        self._resolver = execution_configuration_resolver
        self._clarification_factory = clarification_runner_factory or _default_clarification_runner
        self._abort = threading.Event()
        self._adapters: tuple[CancellableAgentAdapter, ...] = ()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._invocation = _Invocation(f"jiuwenswarm-lh-bench-{uuid.uuid4().hex}")
        self._cli_frozen: dict[str, object] | None = None
        self._started = False

    # -- lifecycle -----------------------------------------------------

    async def run(self) -> BenchmarkOutcome:
        """Execute, export and publish. Always attempts to publish a report."""
        if self._started:
            raise RuntimeError("LongHorizonBenchmark accepts exactly one run")
        self._started = True
        self._loop = asyncio.get_running_loop()
        args = self._args
        workdir = args.workdir.expanduser().resolve(strict=True)
        worktree_root = args.worktree_root.expanduser().resolve() if args.worktree_root else None
        output = validate_output_root(args.output, protected=(workdir, worktree_root or default_worktree_root()))
        runtime = WorkRuntime(workdir, worktree_root=worktree_root)
        frozen: dict[str, object] | None = None
        try:
            frozen = await asyncio.to_thread(self._execute, runtime, output)
        except asyncio.CancelledError:
            await self.cancel()
            raise
        except Exception as exc:
            detail = (str(exc) or type(exc).__name__)[:2000]
            reason = (
                "not_auto_resumable"
                if isinstance(exc, RuntimeStateError) and detail.startswith("not_auto_resumable")
                else "runtime_error"
            )
            self._invocation.failure = {"type": type(exc).__name__, "reason": reason, "detail": detail}
            self._invocation.stop_reason = reason
            # Export only for the trial these exact inputs describe.
            frozen = self._cli_frozen
        selection = await asyncio.to_thread(self._publish, runtime, output, frozen)
        state = self._invocation.state
        return BenchmarkOutcome(
            selection,
            self._invocation.failure is not None,
            self._invocation.stop_reason,
            state.work_id if state is not None else None,
        )

    async def cancel(self) -> None:
        """Request a cooperative stop; admitted calls are never refunded."""
        self._abort.set()
        for adapter in self._adapters:
            with contextlib.suppress(Exception):
                await adapter.cancel_current_turn()

    def _run_async(self, coroutine: Any) -> Any:
        assert self._loop is not None
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop).result()

    # -- execution (Core thread) --------------------------------------

    def _execute(self, runtime: WorkRuntime, output: Path) -> dict[str, object]:
        args = self._args
        task_bytes = args.task.expanduser().resolve(strict=True).read_bytes()
        task_text = task_bytes.decode("utf-8")
        resolved = self._resolve_execution(runtime.workspace)
        binding: dict[str, object] = {
            "schema": "jiuwenswarm-pact/execution-binding/v1",
            "fingerprint": resolved.fingerprint,
            "summary": resolved.summary,
        }
        frozen = self._frozen_input(task_bytes, output)
        self._cli_frozen = frozen
        state = self._open_work(runtime, frozen, binding, task_text)
        self._invocation.state = state
        if state.delivery is not None:
            self._invocation.stop_reason = "delivered"
            return frozen
        state = runtime.recover(state.work_id, expected_revision=state.revision)
        self._invocation.state = state
        if state.status == "paused" and not self._abort.is_set():
            state = runtime.resume(state.work_id, expected_revision=state.revision)
            self._invocation.state = state
        config = args.config()
        self._adapters = (self._make_adapter("worker", resolved), self._make_adapter("reviewer", resolved))
        worker, reviewer = self._adapters
        budget = BenchBudget(runtime.workspace / ".pact" / "bench" / "trials", config, state.work_id)
        work_inputs: dict[str, object] = {
            "contract": state.contract,
            "constraints": list(state.constraints),
            "policy": state.policy.payload(),
            "worker_adapter": worker.id,
            "reviewer_adapter": reviewer.id,
        }
        budget.bind_work_inputs(work_inputs)
        turns_used = 0
        window: BudgetWindow | None = None
        if _phase(state) != "first_review_done":
            with budget.execution():
                window = budget.window(args.window_seconds)
                state, turns_used = self._bootstrap(runtime, output, budget, window, task_text, resolved, state)
        self._invocation.state = state
        if self._abort.is_set() and self._invocation.stop_reason == "not_started":
            self._invocation.stop_reason = "invocation_stopped"
        if self._abort.is_set() or _phase(state) != "first_review_done":
            return frozen
        remaining_turns = args.max_turns - turns_used
        window_seconds = args.window_seconds
        if window is not None and window_seconds is not None:
            window_seconds = window.deadline - budget.clock()
            if window_seconds <= 0:
                self._invocation.stop_reason = "window_budget_exhausted"
                return frozen
        if remaining_turns < 1 and state.acceptance is None:
            self._invocation.stop_reason = "max_turns"
            return frozen
        result = BenchDriver(runtime, worker, reviewer, config=config).run(
            state.work_id,
            expected_revision=state.revision,
            max_turns=max(1, remaining_turns),
            window_seconds=window_seconds,
            stop_requested=self._abort.is_set,
        )
        self._invocation.driver_result = result
        self._invocation.state = result.snapshot
        self._invocation.stop_reason = result.stop_reason
        return frozen

    def _resolve_execution(self, workspace: Path) -> Any:
        from .benchmark_native import NativeExecution
        self._worker.benchmark_source_root = workspace
        return NativeExecution.from_worker(self._worker, self._args.profile_name)

    def _frozen_input(self, task_bytes: bytes, output: Path) -> dict[str, object]:
        args = self._args
        return {
            "schema": INPUT_SCHEMA,
            "request_id": args.request_id,
            "configuration": args.config().payload(),
            "max_turns": args.max_turns,
            "original_task_sha256": sha256_bytes(task_bytes),
            "original_task_bytes": len(task_bytes),
            "output_root": str(output),
            "profile_name": args.profile_name,
            "model": args.model,
            "verify_command": args.verify_command,
        }

    def _open_work(
        self, runtime: WorkRuntime, frozen: dict[str, object], binding: dict[str, object], task_text: str
    ) -> WorkSnapshot:
        args = self._args
        work_id = args.derived_work_id
        latch = runtime.workspace / ".pact-io" / "chrys-budget-suspensions" / f"{work_id}.json"
        if args.work_id is not None:
            if args.work_id != work_id:
                raise RuntimeContractError("--work-id does not belong to this task_id/trial_id")
            state = runtime.read_work(work_id)
            self._invocation.state = state
            if state.revision != args.expected_revision:
                raise RuntimeContractError("stale expected work revision")
            _require_same(state, frozen, binding, self._args.profile_name)
            return state
        contract, plan = read_text_plan(args.task.expanduser().resolve(strict=True), workspace=runtime.workspace)
        if contract.get("goal") != task_text:
            raise RuntimeContractError("the Goal must be the original task verbatim")
        advisory: dict[str, object] = {
            "profile": "benchmark",
            "jiuwenswarm_configuration": jiuwenswarm_configuration(args.profile_name, workflow="benchmark"),
            "jiuwenswarm_execution_configuration": binding,
            INPUT_KEY: frozen,
            PROGRESS_KEY: {
                "schema": PROGRESS_SCHEMA,
                "phase": "created",
                "stages": {},
            },
            "benchmark": {"schema": _BENCH_PROGRESS_SCHEMA},
        }
        state = runtime.create_work(
            request_id=args.request_id,
            work_id=work_id,
            contract=contract,
            policy=AcceptancePolicy(verify_command=args.verify_command, allow_unverified=False),
            constraints=plan.constraints,
            advisory=advisory,
        )
        self._invocation.state = state
        _require_same(state, frozen, binding, args.profile_name)
        if state.delivery is not None:
            return state
        if latch.exists():
            raise RuntimeStateError(f"work budget suspension requires explicit --work-id resume: {work_id}")
        if _phase(state) != "created":
            raise RuntimeStateError(
                f"work {work_id} already started (phase {_phase(state)}, revision {state.revision}); "
                "resume explicitly with --work-id and --expected-revision. No automatic restart."
            )
        return state

    def _make_adapter(self, role: Literal["worker", "reviewer"], resolved: Any) -> Any:
        from .benchmark_native import NativeBenchmarkAdapter
        assert self._loop is not None
        factory = self._adapter_factory or NativeBenchmarkAdapter
        return factory(semantic_role=role, worker=self._worker, outer_loop=self._loop,
            invocation_id=self._invocation.invocation_id, output_dir=self._args.output / "native-roles",
            abort_event=self._abort, expected_fingerprint=resolved.fingerprint)

    # -- bootstrap: S0 → P0 → clarification → first review -------------

    def _bootstrap(
        self,
        runtime: WorkRuntime,
        output: Path,
        budget: BenchBudget,
        window: BudgetWindow,
        task_text: str,
        resolved: ResolvedExecutionConfiguration,
        state: WorkSnapshot,
    ) -> tuple[WorkSnapshot, int]:
        turn_root = self._turn_root(output, state.work_id)
        store = ClarificationArtifactStore(turn_root.parent.parent, 1)
        turns = 0
        if _phase(state) == "created":
            state = self._freeze_s0(runtime, output, turn_root, store, task_text, state)
        if _phase(state) == "p0_started":
            state = self._reconcile_p0(runtime, budget, state)
        if _phase(state) == "s0_frozen":
            if self._abort.is_set():
                return state, turns
            state, ran = self._run_p0(runtime, output, turn_root, budget, window, state)
            turns += int(ran)
            if not ran or self._invocation.failure is not None or self._abort.is_set():
                return state, turns
        if _phase(state) in ("p0_saved", "clarification_started") and "p0" not in _stages(state):
            # Core recovered a saved P0 whose stage evidence was not yet sealed.
            state = self._seal_p0(runtime, output, turn_root, state)
        if _phase(state) in ("p0_saved", "clarification_started"):
            if self._abort.is_set():
                return state, turns
            state = self._clarify(runtime, output, turn_root, store, budget, window, task_text, resolved, state)
            if _phase(state) not in ("brief_binding", "brief_bound"):
                return state, turns
        if _phase(state) == "brief_binding":
            state = self._bind_brief(runtime, output, turn_root, state, task_text)
        if _phase(state) in ("brief_bound", "first_review_started") and not self._abort.is_set():
            state = self._first_review(runtime, budget, window, state)
        return state, turns

    def _turn_root(self, output: Path, work_id: str) -> Path:
        return output / "evidence" / work_id / "requirement_clarification" / "turn_1"

    def _freeze_s0(
        self,
        runtime: WorkRuntime,
        output: Path,
        turn_root: Path,
        store: ClarificationArtifactStore,
        task_text: str,
        state: WorkSnapshot,
    ) -> WorkSnapshot:
        store.save_requirement_input(revision=1, messages=(task_text,))
        receipt = runtime.export_candidate(state.candidate, output / "stages" / f"s0-{uuid.uuid4().hex}")
        record, _snapshot = stage_record(
            output_root=output,
            turn_root=turn_root,
            stage="s0",
            receipt=receipt,
            metadata={"work_id": state.work_id, "original_task_sha256": _frozen(state)["original_task_sha256"]},
        )
        record["baseline_commit"] = read_json(receipt.artifact).get("baseline_commit")
        record["source_head_commit"] = receipt.source_basis.get("head_commit")
        progress = {**_bench_progress(state), "initial_tree": state.candidate.tree}
        return self._advance(runtime, state, "s0_frozen", stages={"s0": record}, benchmark=progress)

    def _reconcile_p0(self, runtime: WorkRuntime, budget: BenchBudget, state: WorkSnapshot) -> WorkSnapshot:
        """After Core recovery, only a never-attempted P0 may run; never replay one."""
        calls = cast(list[dict[str, object]], budget.snapshot()["calls"])
        if any(call.get("purpose") == "implementation" for call in calls):
            raise RuntimeStateError(
                "not_auto_resumable: P0 was admitted but its candidate was not saved; "
                "inspect the Core operation and budget ledger. P0 is never replayed."
            )
        s0 = _stages(state).get("s0")
        if not isinstance(s0, dict) or s0.get("candidate_tree") != state.candidate.tree:
            raise RuntimeStateError("not_auto_resumable: candidate changed without a recorded P0 save")
        return self._advance(runtime, state, "s0_frozen")

    def _run_p0(
        self,
        runtime: WorkRuntime,
        output: Path,
        turn_root: Path,
        budget: BenchBudget,
        window: BudgetWindow,
        state: WorkSnapshot,
    ) -> tuple[WorkSnapshot, bool]:
        deadline = budget.purpose_deadline(window, "implementation")
        stop = budget.stop_reason(deadline, purpose="implementation")
        if stop is not None:
            self._invocation.stop_reason = stop
            return state, False
        prompt = _p0_prompt(state)
        state = self._advance(runtime, state, "p0_started")
        handle = runtime.begin_execution(state.work_id, expected_revision=state.revision, context=prompt, draft=True)
        wrapped = BudgetedAdapter(self._adapters[0], budget, purpose="implementation", deadline=deadline)
        try:
            result = wrapped.run_turn(
                TurnRequest(
                    role="worker",
                    prompt=prompt,
                    workdir=handle.workdir,
                    timeout_seconds=self._args.turn_timeout_seconds,
                    artifact_dir=handle.artifact_dir,
                )
            )
            executed = wrapped.stopped is None
            progress = _bench_progress(state)
            if executed:
                progress.update(
                    worker_turns=len(wrapped.results),
                    ready=result.status == "completed"
                    and re.search(r"(?im)^\s*PACT_REVIEW_READY\s*$", result.final_text) is not None,
                    milestone_ready=result.status == "completed"
                    and re.search(r"(?im)^\s*PACT_MILESTONE_READY\s*$", result.final_text) is not None,
                    last_worker_status=result.status,
                )
            p0 = {"status": result.status, "executed": executed, "stopped": wrapped.stopped}
            lh = _progress(state)
            # A refused P0 made no model call; its saved tree equals S0 and a
            # later explicit resume may still run the first implementation.
            lh.update(phase="p0_saved" if executed else "s0_frozen", p0=p0)
            state = runtime.save_candidate(
                handle,
                summary=result.final_text or result.stderr_tail or f"Worker {result.status}; partial files retained.",
                advisory={**state.advisory, "benchmark": progress, PROGRESS_KEY: lh},
            )
        finally:
            handle.close()
        self._invocation.state = state
        if not executed:
            self._invocation.stop_reason = wrapped.stopped or "p0_not_started"
            return state, False
        self._invocation.p0_ran = True
        self._note_role_failures()
        state = self._seal_p0(runtime, output, turn_root, state)
        if wrapped.stopped is not None or result.status != "completed":
            self._invocation.stop_reason = wrapped.stopped or f"worker_{result.status}"
        return state, True

    def _seal_p0(self, runtime: WorkRuntime, output: Path, turn_root: Path, state: WorkSnapshot) -> WorkSnapshot:
        receipt = runtime.export_candidate(state.candidate, output / "stages" / f"p0-{uuid.uuid4().hex}")
        p0 = _progress(state).get("p0")
        record, _snapshot = stage_record(
            output_root=output,
            turn_root=turn_root,
            stage="p0",
            receipt=receipt,
            metadata={"work_id": state.work_id, "worker": p0 if isinstance(p0, dict) else None},
        )
        return self._advance(runtime, state, "p0_saved", stages={"p0": record})

    def _clarify(
        self,
        runtime: WorkRuntime,
        output: Path,
        turn_root: Path,
        store: ClarificationArtifactStore,
        budget: BenchBudget,
        window: BudgetWindow,
        task_text: str,
        resolved: ResolvedExecutionConfiguration,
        state: WorkSnapshot,
    ) -> WorkSnapshot:
        s0 = cast(dict[str, object], _stages(state)["s0"])
        snapshot = load_stage_snapshot(output, s0)
        attempts = cast(list[object], _progress(state).get("clarification_attempts", []))
        state = self._advance(runtime, state, "clarification_started")
        admission = BenchSideCallAdmission(
            budget,
            deadline=budget.purpose_deadline(window, "clarification"),
            workdir=Path(snapshot.roots[0].view_root),
            adapter_id=f"{CLARIFIER_ADAPTER_ID}-{resolved.fingerprint}",
            stop_requested=self._abort.is_set,
        )
        context = ClarificationContext(
            profile=_selected_model(resolved),
            snapshot=snapshot,
            session_id=f"{self._invocation.invocation_id}-clarification",
            session_dir=turn_root.parent.parent / "clarification-session",
            admission=admission,
        )
        attempt: dict[str, object] = {"invocation_id": self._invocation.invocation_id}
        try:
            result = self._run_async(self._run_clarification(context, task_text))
        except SideCallBudgetExhausted as exc:
            self._invocation.clarification_calls.extend(admission.admitted)
            stopped = isinstance(exc, ClarificationStopped)
            attempt.update(
                status="stopped" if stopped else "budget_exhausted",
                detail=str(exc)[:1000],
                admitted_calls=admission.admitted,
                refused=admission.refused,
            )
            self._invocation.stop_reason = (
                "invocation_stopped" if stopped else admission.refused or "clarification_budget_exhausted"
            )
            # Clarification never touched Core source; an explicit resume may
            # clarify again from the same frozen S0. P0 is not repeated.
            return self._advance(runtime, state, "p0_saved", clarification_attempts=[*attempts, attempt])
        self._invocation.clarification_calls.extend(admission.admitted)
        store.save_result(result, requirement_messages=(task_text,))
        bundle = read_clarification_bundle(turn_root, original_task=task_text)
        checkpoint = evidence_checkpoint(
            output_root=output,
            turn_root=turn_root,
            stage="clarification",
            snapshot=snapshot,
            metadata={
                "work_id": state.work_id,
                "source": "frozen S0 only",
                "clarified_requirement_sha256": bundle.document_sha256,
                "manifest_sha256": bundle.manifest_sha256,
                "status": bundle.status,
            },
        )
        attempt.update(status=bundle.status, admitted_calls=admission.admitted)
        return self._advance(
            runtime,
            state,
            "brief_binding",
            stages={"clarification": {**bundle.record(output), **checkpoint}},
            clarification_attempts=[*attempts, attempt],
        )

    async def _run_clarification(self, context: ClarificationContext, task_text: str) -> Any:
        runner = self._clarification_factory(context)
        service = ClarificationService(runner)
        return await service.clarify(
            revision=RequirementRevision(number=1, messages=(task_text,)),
            background="",
            snapshot=context.snapshot,
        )

    def _bind_brief(
        self, runtime: WorkRuntime, output: Path, turn_root: Path, state: WorkSnapshot, task_text: str
    ) -> WorkSnapshot:
        stages = _stages(state)
        recorded = cast(dict[str, object], stages["clarification"])
        bundle = read_clarification_bundle(turn_root, original_task=task_text)
        if bundle.document_sha256 != recorded.get("document_sha256") or bundle.manifest_sha256 != recorded.get(
            "manifest_sha256"
        ):
            raise RuntimeStateError("clarification bundle changed after it was recorded")
        s0 = cast(dict[str, object], stages["s0"])
        source = (
            f"{_BRIEF_SOURCE}; original_task_sha256={_frozen(state)['original_task_sha256']}; "
            f"s0_tree={s0['candidate_tree']}; clarified_requirement_sha256={bundle.document_sha256}; "
            f"manifest_sha256={bundle.manifest_sha256}"
        )
        if state.acceptance_brief.get("text") != bundle.text or state.acceptance_brief.get("source") != source:
            state = runtime.set_acceptance_brief(
                state.work_id, expected_revision=state.revision, text=bundle.text, source=source
            )
        p0 = cast(dict[str, object], stages["p0"])
        handoff = evidence_checkpoint(
            output_root=output,
            turn_root=turn_root,
            stage="pact-handoff",
            snapshot=load_stage_snapshot(output, p0),
            metadata={
                "work_id": state.work_id,
                "revision": state.revision,
                "candidate_tree": state.candidate.tree,
                "candidate_checkpoint": state.candidate.checkpoint,
                "acceptance_brief_fingerprint": state.acceptance_brief.get("fingerprint"),
                "clarified_requirement_sha256": bundle.document_sha256,
            },
        )
        handoff.update(
            candidate_tree=state.candidate.tree,
            acceptance_brief_fingerprint=state.acceptance_brief.get("fingerprint"),
        )
        return self._advance(runtime, state, "brief_bound", stages={"handoff": handoff})

    def _first_review(
        self, runtime: WorkRuntime, budget: BenchBudget, window: BudgetWindow, state: WorkSnapshot
    ) -> WorkSnapshot:
        """Verify and review the saved P0 before any later Worker.

        The attempt is persisted in BenchDriver's own ``review_pending`` record
        before the review operation, so BenchDriver completes its bookkeeping
        (observations, check proposals, cadence) exactly as for its own reviews.
        """
        p0_tree = cast(dict[str, object], _stages(state)["p0"])["candidate_tree"]
        if _phase(state) == "first_review_started":
            attempt = _bench_progress(state).get("review_pending")
            if (
                isinstance(attempt, dict)
                and any(
                    call["purpose"] in {"diagnostic", "feedback", "acceptance"} and call["status"] != "started"
                    for call in cast(list[dict[str, object]], budget.snapshot()["calls"])
                )
                and state.review is not None
                and state.review.candidate.tree == p0_tree
                and str(state.review.artifact) != attempt.get("previous_review")
            ):
                return self._advance(runtime, state, "first_review_done")
        if state.candidate.tree != p0_tree:
            raise RuntimeStateError("first review target differs from the saved P0 candidate")
        execution_deadline = budget.purpose_deadline(window, "implementation")
        if state.verification is None or state.verification.status == "timeout":
            if budget.stop_reason(window.deadline):
                self._invocation.stop_reason = budget.stop_reason(window.deadline) or "window_budget_exhausted"
                return state
            state = runtime.verify_candidate(
                state.work_id, expected_revision=state.revision, deadline=execution_deadline
            )
            state = runtime.update_advisory(
                state.work_id,
                expected_revision=state.revision,
                advisory={**state.advisory, "benchmark": review_progress.observe(_bench_progress(state), state)},
            )
        progress = _bench_progress(state)
        passed = _public_passed(state)
        full = bool(progress.get("ready")) and passed
        purpose = "acceptance" if full else "feedback" if passed else "diagnostic"
        deadline = budget.purpose_deadline(window, purpose)
        stop = budget.stop_reason(deadline, purpose=purpose)
        if stop is not None:
            # Keep the phase: the first review must happen before Worker 2.
            self._invocation.stop_reason = stop
            return state
        basis = _acceptance_input(state)
        observations = review_progress.records(progress)
        keys = [
            key
            for key, value in observations.items()
            if value.get("pending")
            and (
                not full
                or (
                    state.verification is not None
                    and value.get("evidence") == str(state.verification.artifact)
                    and value.get("candidate_tree") == state.candidate.tree
                )
            )
        ]
        attempt = {
            "purpose": purpose,
            "full": full,
            "basis": basis,
            "feedback_basis": {**basis, "scope": purpose},
            "tree": state.candidate.tree,
            "previous_review": str(state.review.artifact) if state.review else None,
            "keys": keys,
        }
        progress = review_progress.mark_attempt(progress)
        progress["review_pending"] = attempt
        progress["review_attempts"] = int(cast(int, progress.get("review_attempts", 0))) + 1
        if purpose == "diagnostic" and progress.get("ready"):
            progress["last_ready_failure_input"] = basis
        state = self._advance(runtime, state, "first_review_started", benchmark=progress)
        wrapped = BudgetedAdapter(self._adapters[1], budget, purpose=purpose, deadline=deadline)
        if full:
            state = runtime.accept_candidate(
                state.work_id,
                expected_revision=state.revision,
                reviewer=wrapped,
                timeout_seconds=self._args.turn_timeout_seconds,
                deadline=deadline,
            )
        else:
            state = runtime.review_candidate(
                state.work_id,
                expected_revision=state.revision,
                reviewer=wrapped,
                timeout_seconds=self._args.turn_timeout_seconds,
                context=_first_review_context(state, purpose),
                deadline=deadline,
                verification_evidence=state.verification if purpose == "diagnostic" else None,
            )
        self._note_role_failures()
        if not wrapped.results:
            # Acceptance may re-run checks and stop before invoking Reviewer.
            # A synthetic refusal artifact cannot open the first-review gate.
            if (
                full
                and state.verification is not None
                and state.verification.status != "timeout"
                and not _public_passed(state)
                and budget.stop_reason(window.deadline) is None
            ):
                return self._first_review(runtime, budget, window, state)
            self._invocation.stop_reason = wrapped.stopped or "first_review_not_executed"
            return state
        if wrapped.stopped is not None:
            self._invocation.stop_reason = wrapped.stopped
        return self._advance(
            runtime,
            state,
            "first_review_done",
            first_review={"purpose": purpose, "full": full, "stopped": wrapped.stopped, "calls": len(wrapped.results)},
        )

    def _note_role_failures(self) -> None:
        failure = next((adapter.budget_failure for adapter in self._adapters if adapter.budget_failure), None)
        if failure is not None:
            self._invocation.budget_detail = failure
            self._invocation.stop_reason = "budget_exhausted"
        drift = next(
            (adapter.configuration_failure for adapter in self._adapters if adapter.configuration_failure), None
        )
        if drift is not None:
            self._invocation.stop_reason = "configuration_drift"

    # -- advisory helpers ----------------------------------------------

    def _advance(
        self,
        runtime: WorkRuntime,
        state: WorkSnapshot,
        phase: Phase,
        *,
        stages: dict[str, object] | None = None,
        benchmark: dict[str, object] | None = None,
        **updates: object,
    ) -> WorkSnapshot:
        progress = _progress(state)
        progress["phase"] = phase
        if stages:
            progress["stages"] = {**cast(dict[str, object], progress["stages"]), **stages}
        progress.update(updates)
        advisory = {**state.advisory, PROGRESS_KEY: progress}
        if benchmark is not None:
            advisory["benchmark"] = benchmark
        state = runtime.update_advisory(state.work_id, expected_revision=state.revision, advisory=advisory)
        self._invocation.state = state
        return state

    def _worktree_root(self) -> Path | None:
        return self._args.worktree_root.expanduser().resolve() if self._args.worktree_root else None

    # -- export and report ---------------------------------------------

    def _publish(self, runtime: WorkRuntime, output: Path, frozen: dict[str, object] | None) -> Path | None:
        invocation = self._invocation
        state = invocation.state
        export: LatestCandidateExport | None = None
        export_status = "not_attempted"
        export_error: str | None = None
        record: dict[str, object] | None = None
        record_path: Path | None = None
        final_stage: dict[str, object] | None = None
        budget_snapshot: dict[str, object] | None = None
        inputs_valid = frozen is not None and state is not None and state.advisory.get(INPUT_KEY) == frozen
        if state is not None and inputs_valid:
            try:
                state = runtime.read_work(state.work_id)
                invocation.state = state
                trials = runtime.workspace / ".pact" / "bench" / "trials"
                # Reading must never create a ledger: that would start the trial clock.
                if (trials / self._args.trial_id / "budget.json").is_file():
                    budget_snapshot = BenchBudget(trials, self._args.config(), state.work_id).snapshot()
                export = runtime.export_latest_candidate(
                    state.work_id, output / "exports" / invocation.invocation_id, expected_revision=state.revision
                )
                export_status = "exported" if export.receipt is not None else "no_exportable_candidate"
            except Exception as exc:
                export_status = "export_failed"
                export_error = f"{type(exc).__name__}: {exc}"[:2000]
            if export is not None and invocation.budget_detail is not None:
                # Adapter-owned latch: create cannot replay; explicit resume may continue.
                with contextlib.suppress(Exception):
                    record_work_budget(
                        workspace=runtime.workspace,
                        work_id=state.work_id,
                        revision=state.revision,
                        detail=invocation.budget_detail,
                        artifacts=state.artifacts,
                    )
            if export is not None:
                try:
                    record_path, record = self._record(runtime, output, state, export, budget_snapshot or {})
                except Exception as exc:
                    record_path, record = None, None
                    invocation.failure = invocation.failure or {
                        "type": type(exc).__name__,
                        "reason": "report_error",
                        "detail": str(exc)[:2000],
                    }
            if export is not None and export.receipt is not None:
                try:
                    final_stage, _snapshot = stage_record(
                        output_root=output,
                        turn_root=self._turn_root(output, state.work_id),
                        stage="final",
                        receipt=export.receipt,
                        metadata={"work_id": state.work_id, "invocation_id": invocation.invocation_id},
                    )
                except Exception as exc:
                    final_stage = {"stage": "final", "status": "capture_failed", "detail": str(exc)[:1000]}
        payload = self._report(
            state=state,
            output=output,
            frozen=frozen,
            record=record,
            record_path=record_path,
            export=export,
            export_status=export_status,
            export_error=export_error,
            final_stage=final_stage,
            budget_snapshot=budget_snapshot,
        )
        try:
            return publish_selection(output, payload)
        except Exception:
            logger.exception("failed to publish benchmark selection")
            return None

    def _record(
        self,
        runtime: WorkRuntime,
        output: Path,
        state: WorkSnapshot,
        export: LatestCandidateExport,
        budget: dict[str, object],
    ) -> tuple[Path, dict[str, object]]:
        """Write the per-invocation Chrys record, then validate it against the Core receipt."""
        records = output / "records"
        records.mkdir(parents=True, exist_ok=True)
        configuration_failure = next(
            (adapter.configuration_failure for adapter in self._adapters if adapter.configuration_failure), None
        )
        written = record_benchmark_result(
            BenchmarkBinding(self._args.config(), records, None, self._args.max_turns),
            BenchResult(state, self._invocation.stop_reason, budget, runtime.workspace / ".pact" / "bench"),
            export,
            invocation_id=self._invocation.invocation_id,
            configuration_failure=configuration_failure,
            workspace=runtime.workspace,
            worktree_root=self._worktree_root(),
        )
        record = read_json(written.artifact)
        _validate_record(record, state, export.receipt)
        return written.artifact, record

    def _report(
        self,
        *,
        state: WorkSnapshot | None,
        output: Path,
        frozen: dict[str, object] | None,
        record: dict[str, object] | None,
        record_path: Path | None,
        export: LatestCandidateExport | None,
        export_status: str,
        export_error: str | None,
        final_stage: dict[str, object] | None,
        budget_snapshot: dict[str, object] | None,
    ) -> dict[str, object]:
        invocation = self._invocation
        receipt = export.receipt if export is not None else None
        base: dict[str, object] = (
            dict(record)
            if record is not None
            else {
                "schema": RESULT_SCHEMA,
                "invocation_id": invocation.invocation_id,
                "work_id": state.work_id if state is not None else None,
                "revision": state.revision if state is not None else None,
                "task_id": self._args.task_id,
                "trial_id": self._args.trial_id,
                "stop_reason": invocation.stop_reason,
                "configuration_failure": None,
                "status": state.status if state is not None else "not_created",
                "internal_accepted": state is not None and state.acceptance is not None,
                "delivered": state is not None and state.delivery is not None,
                "candidate": asdict(state.candidate) if state is not None else None,
                "acceptance": asdict(state.acceptance) if state is not None and state.acceptance else None,
                "delivery": asdict(state.delivery) if state is not None and state.delivery else None,
                "public_verification": asdict(state.verification) if state is not None and state.verification else None,
                "export": None,
                "export_diagnostics": [],
                "budget": budget_snapshot,
                "external_grade": {"status": "not_run", "reward": None},
            }
        )
        progress = state.advisory.get(PROGRESS_KEY) if state is not None else None
        progress = progress if isinstance(progress, dict) else {}
        stages = dict(cast(dict[str, object], progress.get("stages", {})))
        if final_stage is not None:
            stages["final"] = final_stage
        s0 = stages.get("s0")
        export_baseline = read_json(receipt.artifact).get("baseline_commit") if receipt is not None else None
        base.update(
            {
                "schema": RESULT_SCHEMA,
                "workflow": "long-horizon-benchmark",
                "export": receipt_payload(receipt) if receipt is not None else None,
                "export_status": export_status,
                "export_error": export_error,
                "export_patch": contained(output, receipt.patch) if receipt is not None else None,
                "original_task_sha256": frozen.get("original_task_sha256") if frozen is not None else None,
                "baseline_commit": export_baseline
                if export_baseline is not None
                else (s0.get("baseline_commit") if isinstance(s0, dict) else None),
                # Core's S0 baseline commit is the base of ``export.candidate.patch``;
                # its parent is the source HEAD recorded in the receipt's source basis.
                "source_head_commit": receipt.source_basis.get("head_commit")
                if receipt is not None
                else (s0.get("source_head_commit") if isinstance(s0, dict) else None),
                "source_workspace": str(self._args.workdir.expanduser().resolve()),
                "execution_fingerprint": cast(
                    dict[str, object], state.advisory.get("jiuwenswarm_execution_configuration", {})
                ).get("fingerprint")
                if state is not None
                else None,
                "internal_failure": invocation.failure,
                "budget_suspension": invocation.budget_detail,
                "record": contained(output, record_path) if record_path is not None else None,
                "long_horizon": {
                    "phase": progress.get("phase"),
                    "p0": progress.get("p0"),
                    "p0_ran_this_invocation": invocation.p0_ran,
                    "clarification_attempts": progress.get("clarification_attempts", []),
                    "clarification_calls_this_invocation": invocation.clarification_calls,
                    "first_review": progress.get("first_review"),
                    "stages": stages,
                },
                "external_grade": {"status": "not_run", "reward": None},
            }
        )
        if record is None:
            base["stop_reason"] = invocation.stop_reason
        return base


def _in_process_adapter(**kwargs: Any) -> Any:
    raise RuntimeContractError("Only native Jiuwen benchmark adapters are supported")


def _validate_record(record: dict[str, object], state: WorkSnapshot, receipt: ExportReceipt | None) -> None:
    """The per-invocation record must reference exactly this work and export."""
    if record.get("schema") != RESULT_SCHEMA or record.get("work_id") != state.work_id:
        raise BenchmarkFailure("benchmark record does not reference this work")
    if receipt is None:
        if record.get("export") is not None:
            raise BenchmarkFailure("benchmark record claims an export that does not exist")
        return
    verify_export(receipt)
    exported = record.get("export")
    if not isinstance(exported, dict) or exported.get("artifact") != str(receipt.artifact):
        raise BenchmarkFailure("benchmark record export differs from the Core receipt")


def _require_same(state: WorkSnapshot, frozen: dict[str, object], binding: dict[str, object], profile: str) -> None:
    if state.advisory.get(INPUT_KEY) != frozen:
        raise RuntimeContractError("existing trial uses different task, configuration or output inputs")
    if state.advisory.get("jiuwenswarm_configuration") != jiuwenswarm_configuration(profile, workflow="benchmark"):
        raise RuntimeContractError("existing trial uses a different Chrys profile or workflow")
    stored = state.advisory.get("jiuwenswarm_execution_configuration")
    if not isinstance(stored, dict) or stored.get("fingerprint") != binding["fingerprint"]:
        raise RuntimeContractError("configuration_drift: existing trial uses a different execution configuration")


def _selected_model(resolved: Any) -> Any:
    return resolved


def _progress(state: WorkSnapshot) -> dict[str, object]:
    raw = state.advisory.get(PROGRESS_KEY)
    if not isinstance(raw, dict) or raw.get("schema") != PROGRESS_SCHEMA or raw.get("phase") not in _PHASES:
        raise RuntimeContractError("unsupported or missing long-horizon benchmark progress")
    return json.loads(json.dumps(raw))


def _stages(state: WorkSnapshot) -> dict[str, object]:
    return cast(dict[str, object], _progress(state)["stages"])


def _phase(state: WorkSnapshot) -> Phase:
    return cast(Phase, _progress(state)["phase"])


def _frozen(state: WorkSnapshot) -> dict[str, object]:
    raw = state.advisory.get(INPUT_KEY)
    if not isinstance(raw, dict) or raw.get("schema") != INPUT_SCHEMA:
        raise RuntimeContractError("unsupported or missing long-horizon benchmark input")
    return raw


def _bench_progress(state: WorkSnapshot) -> dict[str, object]:
    raw = state.advisory.get("benchmark", {"schema": _BENCH_PROGRESS_SCHEMA})
    if not isinstance(raw, dict) or raw.get("schema") != _BENCH_PROGRESS_SCHEMA:
        raise RuntimeContractError("unsupported benchmark progress schema")
    return json.loads(json.dumps(raw))


def _public_passed(state: WorkSnapshot) -> bool:
    return state.verification is not None and (
        state.verification.status == "passed"
        or (state.verification.status == "unavailable" and state.policy.allow_unverified)
    )


def _acceptance_input(state: WorkSnapshot) -> dict[str, object]:
    """The BenchDriver acceptance-input shape, so its dedupe sees this review."""
    return {
        "candidate_tree": state.candidate.tree,
        "contract_revision": state.contract_revision,
        "checks": list(state.checks),
        "brief": state.acceptance_brief,
        "verification_status": state.verification.status if state.verification else None,
        "questions": list(state.unresolved_assumptions),
        "decisions": list(state.user_decisions),
    }


def _p0_prompt(state: WorkSnapshot) -> str:
    return (
        "Implement the fixed benchmark task in this isolated checkout. This is the first implementation. "
        "The original requirement below is authoritative. No interactive user is available; document "
        "uncertainty without inventing an answer or weakening the task. Keep all changes in this checkout. "
        "When the full task is ready for independent acceptance, optionally put PACT_REVIEW_READY on its own "
        "line. For an intermediate functional milestone, optionally put PACT_MILESTONE_READY on its own line. "
        "Both hints are advisory; their absence is valid.\n\n"
        f"Original requirement:\n{json.dumps(state.contract, ensure_ascii=False, indent=2)}\n"
        f"Explicit constraints:\n{json.dumps(state.constraints, ensure_ascii=False)}\n\n"
        f"{build_runtime_handoff_instructions()}"
    )


def _first_review_context(state: WorkSnapshot, purpose: str) -> str:
    instruction = (
        "Diagnose the recorded public result for this first candidate and check method validity. "
        "Propose a concrete next experiment; this is feedback, not permission to waive supplied checks "
        "or the original task."
        if purpose == "diagnostic"
        else "Review this first candidate against the original task. Identify missing behavior and "
        "actionable next steps; this is feedback only."
    )
    return (
        f"{instruction}\nThe acceptance brief is a clarification of the original task derived only from the "
        "unmodified baseline (S0). It is advisory analysis, not a new user requirement, and it does not "
        f"answer open questions. Brief source: {state.acceptance_brief.get('source', 'none')}"
    )


# -- command line ---------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m jiuwenswarm.long_horizon.pact.long_horizon_benchmark",
        description="Run one original-task PACT long-horizon benchmark invocation and publish OUTPUT/selection.json.",
    )
    parser.add_argument("--task", type=Path, required=True, help="Original task file (UTF-8); kept verbatim")
    parser.add_argument("--workdir", type=Path, required=True, help="Source repository (S0); never modified")
    parser.add_argument(
        "--output", type=Path, required=True, help="Output root outside the source, e.g. /agent_out/pact-benchmark"
    )
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--trial-id", required=True)
    parser.add_argument("--budget-seconds", type=float, default=3600.0, help="Fixed total trial budget")
    parser.add_argument("--max-role-calls", type=int, default=32, help="Role and clarification side-call cap (0: none)")
    parser.add_argument("--max-turns", type=int, default=3, help="Worker turns per invocation, P0 included")
    parser.add_argument("--turn-timeout", type=float, default=3600.0)
    parser.add_argument("--review-interval", type=int, default=2)
    parser.add_argument("--review-each-round", action="store_true")
    parser.add_argument("--window-seconds", type=float, help="Bound this invocation within the original budget")
    parser.add_argument("--verify", metavar="COMMAND", help="Trusted public verification command (optional)")
    parser.add_argument("--agent", default="Code", help="Chrys role profile")
    parser.add_argument("--model", default="bench", help="Model profile id or name for roles and clarification")
    parser.add_argument("--worktree-root", type=Path)
    parser.add_argument("--work-id", help="Explicit resume: existing Work ID")
    parser.add_argument("--expected-revision", type=int, help="Explicit resume: last observed Work revision")
    return parser


def arguments_from_namespace(namespace: argparse.Namespace) -> BenchmarkArguments:
    return BenchmarkArguments(
        task=namespace.task,
        workdir=namespace.workdir,
        output=namespace.output,
        task_id=namespace.task_id,
        trial_id=namespace.trial_id,
        budget_seconds=namespace.budget_seconds,
        max_role_calls=namespace.max_role_calls or None,
        max_turns=namespace.max_turns,
        turn_timeout_seconds=namespace.turn_timeout,
        review_interval=namespace.review_interval,
        review_each_round=namespace.review_each_round,
        window_seconds=namespace.window_seconds,
        verify_command=namespace.verify.strip() if namespace.verify is not None else None,
        profile_name=namespace.agent,
        model=namespace.model or None,
        worktree_root=namespace.worktree_root,
        work_id=namespace.work_id,
        expected_revision=namespace.expected_revision,
    )




async def run_command(args: BenchmarkArguments) -> int:
    loaded = _prepare_runtime(args.model)
    benchmark = LongHorizonBenchmark(args, loaded_settings=loaded)
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(signum, lambda: loop.create_task(benchmark.cancel()))
    outcome = await benchmark.run()
    sys.stderr.write(
        f"work_id: {outcome.work_id}\nstop_reason: {outcome.stop_reason}\n"
        f"internal_failure: {str(outcome.internal_failure).lower()}\nselection: {outcome.selection}\n"
    )
    if outcome.selection is None:
        return 1
    return 3 if outcome.internal_failure else 0


async def _main(namespace: argparse.Namespace) -> int:
    from ..worker import JiuwenWorker
    args = arguments_from_namespace(namespace)
    worker = JiuwenWorker(namespace.worker_bin, namespace.config_template,
        source_commit=os.environ.get("JIUWENSWARM_SUITE_COMMIT", ""),
        default_timeout_seconds=args.turn_timeout_seconds)
    benchmark = LongHorizonBenchmark(args, worker=worker)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(benchmark.cancel()))
    try:
        outcome = await benchmark.run()
    finally:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)
    print(json.dumps(asdict(outcome), default=str))
    return 1 if outcome.internal_failure else 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    parser.add_argument("--worker-bin", type=Path, required=True)
    parser.add_argument("--config-template", type=Path, required=True)
    return asyncio.run(_main(parser.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
