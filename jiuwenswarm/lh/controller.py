# Copyright (c) 2026 Chrys. All rights reserved.

"""Trusted original-task long-horizon benchmark entry: ``python -m long_horizon``.

One invocation owns one explicit Work operation on a fixed trial:

1. Create Work from the original task (verbatim Goal) and the fixed BenchBudget
   on S0 before any model call. S0 is Core's initial candidate, exported and
   frozen as a snapshot.
2. Run exactly one initial Worker (P0) in Core's ``begin_execution`` checkout
   and save its candidate. P0 is never replayed.
3. The default suite runs clarification, recall and localization on frozen S0,
   generates the structured contract/plan and performs a separate P1 repair.
   Every model call uses the same trial budget. The explicit benchmark mode
   retains clarification followed by review of P0.
4. Review the saved candidate through Core with independent management advice
   in suite mode, then continue with plan-guided BenchDriver Workers.
5. Export Core's latest candidate (accepted or not) and publish
   ``OUTPUT/selection.json`` (a plugin-compatible result schema). Independent
   grading is always ``not_run`` here.

Core owns verification, Gate, acceptance and delivery. Suite planning artifacts
are advisory to the original verbatim task. Generated public checks require
Reviewer admission; neither workflow sets ``allow_unverified``.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import hashlib
import json
import logging
import math
import os
import re
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import CancelledError as FutureCancelledError
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

from jiuwenswarm.pact_core.adapters.base import TurnRequest
from jiuwenswarm.pact_core.artifacts import read_json, write_json_atomic
from jiuwenswarm.pact_core.benchmark import review_progress
from jiuwenswarm.pact_core.benchmark.budget import BenchBudget, BudgetWindow
from jiuwenswarm.pact_core.benchmark.driver import BenchDriver  # noqa: F401 - compatibility seam for driver tests
from jiuwenswarm.pact_core.benchmark.types import BenchConfig, BenchResult
from jiuwenswarm.pact_core.kernel import default_worktree_root
from jiuwenswarm.pact_core.prompts import build_runtime_handoff_instructions
from jiuwenswarm.pact_core.runtime import AcceptancePolicy, RuntimeContractError, RuntimeStateError, WorkRuntime, WorkSnapshot
from jiuwenswarm.pact_core.runtime.export import ExportReceipt, LatestCandidateExport, verify_export
from jiuwenswarm.pact_core.runtime.text_plan import read_text_plan

from .acceptance_timing import AcceptanceTimings, VerificationTimeUnavailable
from .budget_boundary import BudgetedAdapter, LedgerStatusBoundary
from .budget_origin import create_budget, validate_cutoff, validate_origin
from .candidate_gate import PublicCandidateGate
from .capacity import (
    DEFAULT_CAPACITY_SCALE,
    capacity_receipt,
    capacity_scale,
    model_content_limit,
    scaled,
)
from .check_outputs import read_check_outputs
from .checkpoint_guard import CandidateTooLarge, ControllerMemoryPressure, sha256_file
from .control_policy import PhasePolicy, TurnWindowSpent, official_residual
from .early_acceptance_audit import (
    SCHEMA as EARLY_AUDIT_SCHEMA,
)
from .early_acceptance_audit import (
    AuditReport,
    DeferredDeliveryRuntime,
    EarlyAuditPolicy,
    parse_audit_report,
)
from .evidence import (
    contained,
    contained_in_roots,
    publish_selection,
    receipt_payload,
    sha256_bytes,
    validate_output_root,
)
from .harbor_unified.candidate_artifacts import default_archive_root, make_evidence_runtime
from .harbor_unified.public_check_outcome import (
    TrustedCheckReceipts,
    decode_public_check_outcome,
    is_rpc_public_check_command,
    receipts_for_public_check_command,
)
from .method_routing import method_diagnosis
from .ports import ArtifactStore, ClarificationContext, ExecutionBinding, Plugin, RoleAdapter, RoleBoundary
from .process_termination import terminate_check_processes
from .progress import ReviewProgressGuard
from .public_reference_evidence import recorded_reference_checks
from .public_review import PublicReviewProgress, public_review_failure
from .review_admissions import (
    MAX_WORKER_REPAIR_ROUNDS,
    WORKER_REPAIR_SCHEMA,
    ReviewAdmissions,
    acceptance_fuse_reached,
    default_review_limits,
    worker_repair_allowed,
    worker_repair_round_seconds,
)
from .review_governance import recent_acceptance_durations, time_admission
from .review_limits import ReviewLimitedDriver, _pinned_budget_window
from .reviewer_execution import (
    REVIEWER_EXECUTION_GUIDANCE_VERSION,
    IsolatedExecutableReviewer,
    NonDegradableIntegrityError,
    RecoveredViewHandoffAdapter,
    configure_side_call_integrity_state,
    release_recovered_views,
    side_call_integrity_state_path,
    verify_recovered_views,
)
from .reviewer_privilege import ReviewerLaunchPolicy, ReviewerProtectionError
from .side_calls import (
    SideCallIntegrityError,
    SideCallRecovered,
    recover_side_call_integrity,
)
from .worker_evidence import (
    ANYTIME_DELIVERABLE_GUIDANCE_VERSION,
    GENERALIZATION_CHECK_GUIDANCE_VERSION,
    REFERENCE_SPEC_GUIDANCE_VERSION,
    UI_STATE_EVIDENCE_GUIDANCE_VERSION,
    WORKER_EVIDENCE_GUIDANCE_VERSION,
    audit_generalization_check_guidance,
    reviewer_delivery_hygiene_guidance,
    reviewer_ui_state_evidence_guidance,
    worker_evidence_guidance,
)

logger = logging.getLogger(__name__)

_BENCH_PROGRESS_SCHEMA = "pact-core/benchmark-progress/v2"


def finish_reserve_seconds(total_seconds: float) -> float:
    """Keep enough of the original budget to stop roles and publish a delivery."""
    if (
        isinstance(total_seconds, bool)
        or not isinstance(total_seconds, (int, float))
        or not math.isfinite(total_seconds)
        or total_seconds <= 0
    ):
        raise RuntimeContractError("original budget must be a positive finite duration")
    return min(float(total_seconds) / 2.0, max(120.0, float(total_seconds) * 0.05))


def _restored_phase_history(path: Path) -> tuple[list[float], list[float]]:
    """Read the durable seal/check and acceptance durations from a prior invocation."""
    try:
        payload = read_json(path)
    except (OSError, ValueError, TypeError):
        return [], []
    if not isinstance(payload, dict):
        return [], []

    def durations(key: str, legacy_key: str | None = None) -> list[float]:
        values = payload.get(key)
        if not isinstance(values, list) and legacy_key is not None:
            values = payload.get(legacy_key)
        if not isinstance(values, list):
            return []
        result: list[float] = []
        for value in values:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            if not math.isfinite(float(value)) or float(value) < 0:
                continue
            result.append(float(value))
        return result

    return durations("finish_observations_seconds", "finish_observations"), durations(
        "recent_acceptance_seconds", "recent_acceptance"
    )


def finish_grace_seconds(total_seconds: float) -> float:
    """Publication/cleanup grace inside the finish reserve."""
    return min(30.0, finish_reserve_seconds(total_seconds) * 0.25)


Phase = Literal[
    "created",
    "s0_frozen",
    "p0_started",
    "p0_saved",
    "clarification_started",
    "brief_binding",
    "brief_bound",
    "p1_started",
    "p1_saved",
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
    "p1_started",
    "p1_saved",
    "first_review_started",
    "first_review_done",
)


def has_saved_external_worker_failure(progress: object) -> bool:
    """Check for an executed, sealed P0 that recorded its own native failure."""
    if not isinstance(progress, dict) or progress.get("phase") != "p0_saved":
        return False
    initial = progress.get("p0")
    stages = progress.get("stages")
    return (
        isinstance(initial, dict)
        and initial.get("native_status") == "failed"
        and initial.get("status") == "output_missing"
        and initial.get("executed") is True
        and initial.get("stopped") is None
        and isinstance(stages, dict)
        and "p0" in stages
        and not progress.get("clarification_attempts")
        and progress.get("draft_recovery_refusals") != 2
        and "prior_failed_draft" not in progress
    )


@dataclass
class _NativeTurnRefusalGuard:
    """Restore a native refusal before Core consumes the returned role result."""

    native: RoleAdapter
    refuse: Callable[[], None]

    @property
    def id(self) -> str:
        return self.native.id

    def probe(self) -> Any:
        return self.native.probe()

    def run_turn(self, request: TurnRequest) -> Any:
        result = self.native.run_turn(request)
        if self.native.budget_failure == "TurnBudgetExhausted":
            self.refuse()
        return result


@dataclass
class _WorkerDeadlineGuard:
    """Deliver the reserved Worker deadline to every backend invocation.

    Core's driver builds its own implementation deadline from the admitted
    window, which is the soft model boundary. The controller's phase policy
    subtracts the observed finish reserve and the minimum Reviewer reserve;
    capping the actual TurnRequest here makes that deduction real for P0, P1,
    later repair Workers and both backends' BenchDriver calls.
    """

    native: RoleAdapter
    controller: Any
    regression_context: str = ""

    def __getattr__(self, name: str) -> Any:
        return getattr(self.native, name)

    @property
    def id(self) -> str:
        return self.native.id

    def probe(self) -> Any:
        return self.native.probe()

    def run_turn(self, request: TurnRequest) -> Any:
        policy = getattr(self.controller, "_phase_policy", None)
        if policy is not None:
            deadline = policy.deadline_for("worker")
            current = getattr(request, "deadline", None)
            capped = deadline if current is None else min(float(current), deadline)
            if current is None or float(current) != capped:
                request = replace(request, deadline=capped)
        if self.regression_context:
            request = replace(request, prompt=f"{request.prompt}\n\n{self.regression_context}")
        return self.native.run_turn(request)

    async def cancel_current_turn(self) -> None:
        await self.native.cancel_current_turn()


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
    budget_seconds: float = 172800.0
    max_role_calls: int | None = None
    max_turns: int | None = 64
    turn_timeout_seconds: float = 21600.0
    review_interval: int = 2
    review_each_round: bool = False
    reviewer_uid: int | None = None
    reviewer_gid: int | None = None
    reviewer_launch_policy: Any = None
    review_diagnosis_limit: int | None = None
    review_acceptance_limit: int | None = None
    window_seconds: float | None = None
    verify_command: str | None = None
    profile_name: str = "Code"
    model: str | None = "bench"
    worktree_root: Path | None = None
    work_id: str | None = None
    expected_revision: int | None = None
    workflow: Literal["suite", "benchmark"] = "suite"
    # The main Reviewer executes in a disposable copy.  Read-only side calls
    # explicitly select "read_only" so they keep their frozen-view guarantee.
    reviewer_execution: Literal["isolated_executable", "read_only"] = "isolated_executable"
    request_namespace: str = "long-horizon"
    work_id_prefix: str = "long-horizon-"
    budget_started_at_unix: float | None = None
    finish_reserve_override_seconds: float | None = None
    control_profile: Literal["legacy", "bounded"] = "legacy"
    snapshot_max_total_bytes: int = 1024 * 1024 * 1024
    deadline_only_budget_seconds: float | None = None
    transport_recovery_attempts: int = 3
    early_audit_max_rounds: int = 2
    early_audit_min_remaining_ratio: float = 0.40
    early_audit_min_remaining_seconds: float = 45 * 60.0
    early_audit_max_seconds: float | None = None
    allow_user_interaction: bool = False
    clarification_max_rounds: int = 3
    clarification_budget_ratio: float = 0.20
    cutoff_at_unix: float | None = None
    external_prompt_verbatim: str | None = None
    external_regression_context: str = ""
    external_live_workdir: Path | None = None
    external_question_channel: Any = None
    clock: Callable[[], float] = time.time
    resume_failed_external_draft: bool = False
    resume_rotated_external_session: bool = False
    on_external_work_ready: Callable[[], None] | None = None
    on_external_verification: Callable[[WorkSnapshot], None] | None = None

    def __post_init__(self) -> None:
        from .snapshot_resources import SnapshotResources

        SnapshotResources(self.snapshot_max_total_bytes)
        if self.budget_started_at_unix is not None:
            validate_origin(self.budget_started_at_unix)
        reserve = self.finish_reserve_override_seconds
        if reserve is not None and (
            isinstance(reserve, bool)
            or not isinstance(reserve, (int, float))
            or not math.isfinite(reserve)
            or reserve <= 0
            or reserve >= self.budget_seconds
        ):
            raise RuntimeContractError("finish reserve must be positive, finite and below the original budget")
        validate_cutoff(self.cutoff_at_unix)
        if self.control_profile not in ("legacy", "bounded"):
            raise RuntimeContractError("control_profile must be legacy or bounded")
        if self.control_profile == "bounded" and (self.workflow != "suite" or not self.verify_command):
            raise RuntimeContractError("bounded control requires suite workflow and a supplied --verify command")
        if self.workflow not in ("suite", "benchmark"):
            raise RuntimeContractError("workflow must be suite or benchmark")
        if self.reviewer_execution not in ("isolated_executable", "read_only"):
            raise RuntimeContractError("reviewer_execution must be isolated_executable or read_only")
        if (self.reviewer_uid is None) != (self.reviewer_gid is None):
            raise RuntimeContractError("reviewer uid and gid must be supplied together")
        if self.reviewer_uid is not None and (
            isinstance(self.reviewer_uid, bool) or isinstance(self.reviewer_gid, bool)
            or self.reviewer_uid <= 0 or self.reviewer_gid <= 0
        ):
            raise RuntimeContractError("reviewer uid and gid must be positive nonzero IDs")
        if (self.work_id is None) != (self.expected_revision is None):
            raise RuntimeContractError("explicit resume requires both --work-id and --expected-revision")
        if self.max_turns is None:
            total = self.deadline_only_budget_seconds
            if (
                self.workflow != "suite"
                or self.budget_started_at_unix is None
                or isinstance(total, bool)
                or not isinstance(total, (int, float))
                or not math.isfinite(total)
                or total <= 0
                or isinstance(self.budget_seconds, bool)
                or isinstance(self.turn_timeout_seconds, bool)
                or total != self.budget_seconds
                or self.turn_timeout_seconds != total
            ):
                raise RuntimeContractError(
                    "deadline-only turns require suite, original origin and explicit matching finite task/role budget"
                )
        elif isinstance(self.max_turns, bool) or not isinstance(self.max_turns, int) or self.max_turns < 1:
            raise RuntimeContractError("max_turns must be a positive integer or null for deadline-only suite")
        elif self.deadline_only_budget_seconds is not None:
            raise RuntimeContractError("deadline_only_budget_seconds requires max_turns=null")
        if (
            isinstance(self.transport_recovery_attempts, bool)
            or not isinstance(self.transport_recovery_attempts, int)
            or not 0 <= self.transport_recovery_attempts <= 10
        ):
            raise RuntimeContractError("transport_recovery_attempts must be an integer from 0 to 10")
        try:
            self.early_audit_policy()
        except ValueError as exc:
            raise RuntimeContractError(str(exc)) from exc
        if not isinstance(self.allow_user_interaction, bool):
            raise RuntimeContractError("allow_user_interaction must be boolean")
        if (
            isinstance(self.clarification_max_rounds, bool)
            or not isinstance(self.clarification_max_rounds, int)
            or self.clarification_max_rounds < 1
        ):
            raise RuntimeContractError("clarification_max_rounds must be a positive integer")
        if (
            isinstance(self.clarification_budget_ratio, bool)
            or not isinstance(self.clarification_budget_ratio, (int, float))
            or not math.isfinite(self.clarification_budget_ratio)
            or not 0 < self.clarification_budget_ratio <= 1
        ):
            raise RuntimeContractError("clarification_budget_ratio must be greater than 0 and at most 1")
        for name in ("review_diagnosis_limit", "review_acceptance_limit"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
                raise RuntimeContractError(f"{name} must be a positive integer or null")
        if self.window_seconds is not None and (not math.isfinite(self.window_seconds) or self.window_seconds <= 0):
            raise RuntimeContractError("window_seconds must be positive and finite")
        if self.verify_command is not None and not self.verify_command.strip():
            raise RuntimeContractError("verify command must be nonempty")

    def early_audit_policy(self) -> EarlyAuditPolicy:
        """Bind the configurable early-acceptance audit thresholds."""
        return EarlyAuditPolicy(
            max_rounds=self.early_audit_max_rounds,
            min_remaining_ratio=self.early_audit_min_remaining_ratio,
            min_remaining_seconds=self.early_audit_min_remaining_seconds,
            max_seconds=self.early_audit_max_seconds,
            finish_reserve_seconds=self.finish_reserve_seconds,
        )

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

    def review_limits(self) -> tuple[int, int]:
        """Return the effective diagnosis and acceptance review allowances."""
        diagnosis, acceptance = default_review_limits(self.budget_seconds)
        return (
            self.review_diagnosis_limit if self.review_diagnosis_limit is not None else diagnosis,
            self.review_acceptance_limit if self.review_acceptance_limit is not None else acceptance,
        )

    @property
    def request_id(self) -> str:
        return f"{self.request_namespace}:{self.task_id}:{self.trial_id}"

    @property
    def derived_work_id(self) -> str:
        return self.work_id_prefix + hashlib.sha256(self.request_id.encode()).hexdigest()[:32]

    @property
    def finish_reserve_seconds(self) -> float:
        return (
            finish_reserve_seconds(self.budget_seconds)
            if self.finish_reserve_override_seconds is None
            else float(self.finish_reserve_override_seconds)
        )

    @property
    def original_deadline_unix(self) -> float | None:
        if self.budget_started_at_unix is None:
            return None
        return self.budget_started_at_unix + self.budget_seconds

    @property
    def soft_deadline_unix(self) -> float | None:
        original = self.original_deadline_unix
        return None if original is None else original - self.finish_reserve_seconds

    @property
    def finish_deadline_unix(self) -> float | None:
        # The outer clock may not abort an already admitted seal/check before
        # the original hard deadline. The soft deadline now stops only new
        # model work; it is not the process teardown boundary.
        return self.original_deadline_unix


@dataclass(slots=True)
class _Invocation:
    """Mutable facts of this invocation for the report; Core state stays authoritative."""

    invocation_id: str
    state: WorkSnapshot | None = None
    stop_reason: str = "not_started"
    failure: dict[str, object] | None = None
    budget_detail: str | None = None
    public_review_basis_sha256: str | None = None
    p0_ran: bool = False
    clarification_calls: list[str] = field(default_factory=list)
    driver_result: BenchResult | None = None
    fixed_window_wait_revision: int | None = None


class _AuditDeadlineExceeded(RuntimeError):
    """A synchronous audit write outlived the bounded audit deadline."""


def _run_before_deadline(deadline: float, function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run one blocking audit write without letting it consume the publish reserve.

    Python cannot kill a thread, so the controller abandons a call that outlives
    the audit deadline and publishes the already accepted fallback. The call's
    own unique output directory is never promoted after the timeout.
    """
    remaining = float(deadline) - time.time()
    if remaining <= 0:
        raise _AuditDeadlineExceeded("audit deadline expired before I/O")
    outcome: dict[str, Any] = {}
    finished = threading.Event()

    def invoke() -> None:
        try:
            outcome["value"] = function(*args, **kwargs)
        except BaseException as exc:  # pragma: no cover - re-raised in caller
            outcome["error"] = exc
        finally:
            finished.set()

    threading.Thread(target=invoke, name="lh-audit-io", daemon=True).start()
    if not finished.wait(remaining):
        raise _AuditDeadlineExceeded("audit I/O exceeded the audit deadline")
    error = outcome.get("error")
    if isinstance(error, BaseException):
        raise error
    return outcome.get("value")


@dataclass(frozen=True, slots=True)
class BenchmarkOutcome:
    """What the CLI prints; ``selection`` is the only machine contract."""

    selection: Path | None
    internal_failure: bool
    stop_reason: str
    work_id: str | None
    candidate_available: bool = False
    fixed_window_wait_revision: int | None = None
    public_review_basis_sha256: str | None = None


async def _ignore_update(_update: object) -> None:
    return


class LongHorizonBenchmark:
    """Own one benchmark invocation, its Core thread and two role adapters."""

    def __init__(
        self,
        args: BenchmarkArguments,
        *,
        plugin: Plugin,
    ) -> None:
        self._args = replace(
            args,
            request_namespace=plugin.spec.request_namespace + (":suite" if args.workflow == "suite" else ""),
            work_id_prefix=plugin.spec.work_id_prefix,
        )
        self._plugin = plugin
        self._artifacts = copy.copy(plugin.artifacts)
        configure_resources = getattr(self._artifacts, "configure_snapshot_resources", None)
        if not callable(configure_resources):
            raise RuntimeContractError("artifact plugin must implement bounded configure_snapshot_resources")
        configure_resources(max_total_bytes=self._args.snapshot_max_total_bytes)
        self._abort = threading.Event()
        self._adapters: tuple[RoleAdapter, ...] = ()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._invocation = _Invocation(f"{plugin.spec.id}-lh-{uuid.uuid4().hex}")
        self._cli_frozen: dict[str, object] | None = None
        self._started = False
        self._suite_preparation: dict[str, object] | None = None
        self._gate: PublicCandidateGate | None = None
        self._acceptance_timings: AcceptanceTimings | None = None
        self._phase_policy: PhasePolicy | None = None
        self._budget: BenchBudget | None = None
        self._window: BudgetWindow | None = None
        self._candidate_available = False
        self._external_question_cancelled = asyncio.Event()
        self._archive_root: Path | None = None
        self._original_deadline: float | None = None
        self._soft_deadline: float | None = None
        self._finish_deadline: float | None = None
        self._join_timed_out = False
        self._cancel_timed_out = False
        self._check_deadline: float | None = None
        self._rpc_public_check = is_rpc_public_check_command(self._args.verify_command)
        self._check_receipts: TrustedCheckReceipts | None = None
        self._public_check_halted = False
        self._public_check_fallback_checkpoint: str | None = None
        self._early_audit_fallback: dict[str, object] | None = None
        self._early_audit_fallback_receipt: ExportReceipt | None = None
        self._early_audit_acceptance: dict[str, object] | None = None
        self._early_audit_verification: dict[str, object] | None = None
        self._early_audit_evidence: dict[str, object] | None = None
        self._early_audit_replaced = False
        self._early_audit_finished = False
        self._early_audit_closed = False
        diagnosis_limit, acceptance_limit = self._args.review_limits()
        self._review_admissions: ReviewAdmissions = ReviewAdmissions(
            Path(self._args.workdir).expanduser()
            / ".pact"
            / "bench"
            / "trials"
            / self._args.trial_id
            / "review-admissions.json",
            diagnosis_limit=diagnosis_limit,
            acceptance_limit=acceptance_limit,
            trial_id=self._args.trial_id,
            work_id=self._args.derived_work_id,
        )
        # Delivered Work is immutable in Core. Read-only host routes observed
        # after delivery stay invocation-local and are reported, never saved.
        self._post_delivery_host_checks: dict[str, dict[str, object]] = {}
        # A worker-only repair round cannot create acceptance. If its last
        # candidate fails the public check, retain the last passing candidate.
        self._review_quota_passed_receipt: ExportReceipt | None = None
        # Time-and-progress governance evidence is metadata only, never a
        # substitute for Core acceptance.
        self._review_governance: dict[str, object] = {}
        self._public_check_durations: list[float] = []
        self._last_public_check_started_at: float | None = None
        self._public_check_evidence: list[dict[str, object]] = []
        self._finish_observations: list[float] = []
        self._pending_seal_seconds = 0.0
        self._rejected_check_checkpoints: set[str] = set()
        self._integrity_hard_failure = False

    # -- lifecycle -----------------------------------------------------

    async def run(self) -> BenchmarkOutcome:
        """Execute, export and publish. Always attempts to publish a report."""
        if self._started:
            raise RuntimeError("LongHorizonBenchmark accepts exactly one run")
        self._started = True
        self._plugin.capabilities.require_long_horizon()
        self._loop = asyncio.get_running_loop()
        args = self._args
        workdir = args.workdir.expanduser().resolve(strict=True)
        worktree_root = args.worktree_root.expanduser().resolve() if args.worktree_root else None
        output = validate_output_root(args.output, protected=(workdir, worktree_root or default_worktree_root()))
        self._archive_root = default_archive_root(output)
        self._original_deadline = args.original_deadline_unix
        self._soft_deadline = args.soft_deadline_unix
        self._finish_deadline = args.finish_deadline_unix
        # Model and check admission ends at the soft deadline through the phase
        # policy. Host-check receipts retain the original Harbor clock identity.
        self._check_deadline = self._original_deadline
        if self._rpc_public_check and self._check_deadline is not None:
            self._check_receipts = receipts_for_public_check_command(
                args.verify_command, deadline_unix=self._check_deadline
            )
        runtime = make_evidence_runtime(
            workdir,
            worktree_root=worktree_root,
            archive_root=self._archive_root,
            deadline_unix=self._original_deadline,
            max_total_bytes=args.snapshot_max_total_bytes,
            sequence_root=output,
        )
        runtime.on_verification = args.on_external_verification
        diagnosis_limit, acceptance_limit = args.review_limits()
        self._review_admissions = ReviewAdmissions(
            runtime.workspace / ".pact" / "bench" / "trials" / args.trial_id / "review-admissions.json",
            diagnosis_limit=diagnosis_limit,
            acceptance_limit=acceptance_limit,
            trial_id=args.trial_id,
            work_id=args.derived_work_id,
        )
        if args.control_profile == "bounded":
            self._gate = PublicCandidateGate(
                output / "candidate-gates" / (self._invocation.invocation_id + ".json"),
                sequence_root=output,
            )
        frozen: dict[str, object] | None = None
        try:
            frozen = await self._execute_with_finish_boundary(runtime, output)
        except FutureCancelledError:
            self._invocation.stop_reason = "invocation_stopped"
            frozen = self._cli_frozen
        except VerificationTimeUnavailable as refused:
            if (args.external_live_workdir is not None and args.expected_revision == refused.unchanged_revision
                    and refused.deadline == args.original_deadline_unix):
                # A fresh continuation sees the same immutable hard cutoff and
                # durable timing sample. The adapter verifies revision and usage.
                self._invocation.fixed_window_wait_revision = refused.unchanged_revision
            self._invocation.stop_reason = "review_time_admission"
            frozen = self._cli_frozen
        except TurnWindowSpent as refused:
            self._invocation.stop_reason = "turn_window_spent"
            write_json_atomic(output / "turn-window-stop.json", refused.receipt())
            frozen = self._cli_frozen
        except CandidateTooLarge as exc:
            # A refused candidate is retained as evidence; the previous
            # checkpoint is still exportable and the controller stays alive.
            self._invocation.stop_reason = "candidate_too_large"
            logger.warning("candidate capture refused before checkpoint: %s", exc)
            frozen = self._cli_frozen
        except ControllerMemoryPressure as exc:
            # Publish retained evidence and let the existing fallback delivery
            # path apply an already sealed candidate instead of waiting for OOM.
            self._invocation.failure = {
                "type": type(exc).__name__,
                "reason": "memory_pressure",
                "detail": str(exc)[:2000],
            }
            self._invocation.stop_reason = "memory_pressure"
            frozen = self._cli_frozen
        except Exception as exc:
            detail = (str(exc) or type(exc).__name__)[:2000]
            if isinstance(exc, NonDegradableIntegrityError):
                self._integrity_hard_failure = True
                # Prefer the per-adapter integrity record. It carries the
                # evidence path and distinguishes reviewer_mutation from
                # reviewer_restore_failed, as required by the #46 latch.
                self._note_role_failures()
            if self._invocation.failure is None:
                reason = (
                    "integrity_failure"
                    if isinstance(exc, NonDegradableIntegrityError)
                    else "not_auto_resumable"
                    if isinstance(exc, RuntimeStateError) and detail.startswith("not_auto_resumable")
                    else "reviewer_live_workdir_unprotected" if isinstance(exc, ReviewerProtectionError)
                    else "runtime_error"
                )
                self._invocation.failure = {"type": type(exc).__name__, "reason": reason, "detail": detail}
                self._invocation.stop_reason = reason
            # Export only for the trial these exact inputs describe.
            frozen = self._cli_frozen
        if self._join_timed_out:
            # A timed-out join means the writer may still be running. Do not
            # publish or return a clean teardown as if it had stopped.
            if self._invocation.failure is None:
                self._record_join_timeout()
            state = self._invocation.state
            return BenchmarkOutcome(
                None,
                True,
                self._invocation.stop_reason,
                state.work_id if state is not None else None,
                False,
            )
        if args.external_live_workdir is not None:
            state = self._invocation.state
            return BenchmarkOutcome(
                None,
                self._invocation.failure is not None,
                self._invocation.stop_reason,
                state.work_id if state is not None else None,
                state is not None and state.candidate is not None,
                self._invocation.fixed_window_wait_revision,
                self._invocation.public_review_basis_sha256,
            )
        publish = asyncio.create_task(asyncio.to_thread(self._publish, runtime, output, frozen))
        try:
            selection = await asyncio.shield(publish)
        except asyncio.CancelledError:
            # Publication is the last writer. Join it before the caller tears
            # down the owned arena, even when the outer Harbor clock fires.
            if not await self._join_execution(publish, deadline=self._cancellation_join_deadline()):
                raise RuntimeStateError(
                    "execution_stop_timeout: publisher did not stop before the finish deadline"
                ) from None
            raise
        state = self._invocation.state
        return BenchmarkOutcome(
            selection,
            self._invocation.failure is not None,
            self._invocation.stop_reason,
            state.work_id if state is not None else None,
            self._candidate_available,
        )

    async def _execute_with_finish_boundary(self, runtime: WorkRuntime, output: Path) -> dict[str, object] | None:
        """Let admitted finish work run to the hard deadline; stop only new work at soft.

        The soft deadline is enforced by every model admission and scheduler
        gate, not by aborting the execution thread. An already admitted host
        check or sealed export may therefore finish before the original hard
        deadline. Explicit cancellation and the hard deadline still tear the
        execution down immediately.
        """
        if self._abort.is_set():
            self._invocation.stop_reason = "invocation_stopped"
            return self._cli_frozen
        execution = asyncio.create_task(asyncio.to_thread(self._execute, runtime, output))
        hard_deadline = getattr(self, "_original_deadline", None) or getattr(self, "_finish_deadline", None)
        if hard_deadline is None:
            try:
                return await asyncio.shield(execution)
            except asyncio.CancelledError:
                deadline = self._cancellation_join_deadline()
                await self._cancel_uninterruptible(deadline=deadline)
                if not await self._join_execution(execution, deadline=deadline):
                    raise RuntimeStateError(
                        "execution_stop_timeout: worker did not stop before the finish deadline"
                    ) from None
                raise
        remaining = hard_deadline - time.time()
        if remaining <= 0:
            self._invocation.stop_reason = "window_budget_exhausted"
            await self._cancel_uninterruptible(deadline=self._cancellation_join_deadline())
            if not await self._join_execution(execution, deadline=self._cancellation_join_deadline()):
                raise RuntimeStateError(
                    "execution_stop_timeout: worker did not stop before the finish deadline"
                ) from None
            return self._cli_frozen
        try:
            async with asyncio.timeout(remaining) as limit:
                return await asyncio.shield(execution)
        except TurnWindowSpent:
            # A native admission refusal is its own typed outcome, not the
            # outer hard-deadline timeout.
            raise
        except TimeoutError:
            if not limit.expired():
                # A role/transport TimeoutError is not the finish boundary.
                raise
            deadline = self._cancellation_join_deadline()
            self._invocation.stop_reason = "window_budget_exhausted"
            await self._cancel_uninterruptible(deadline=deadline)
            if not await self._join_execution(execution, deadline=deadline):
                raise RuntimeStateError(
                    "execution_stop_timeout: worker did not stop before the finish deadline"
                ) from None
            return self._cli_frozen
        except asyncio.CancelledError:
            # Explicit external cancellation still propagates, but no controller
            # thread may keep writing after the caller owns teardown.
            deadline = self._cancellation_join_deadline()
            await self._cancel_uninterruptible(deadline=deadline)
            if not await self._join_execution(execution, deadline=deadline):
                raise RuntimeStateError(
                    "execution_stop_timeout: worker did not stop before the finish deadline"
                ) from None
            raise

    async def _cancel_uninterruptible(self, *, deadline: float | None = None) -> None:
        """Run cooperative cancellation until it returns or the shared deadline expires."""
        while True:
            remaining = None if deadline is None else deadline - time.time()
            if remaining is not None and remaining <= 0:
                self._cancel_timed_out = True
                return
            limit: asyncio.Timeout | None = None
            try:
                if remaining is None:
                    await self.cancel()
                else:
                    async with asyncio.timeout(remaining) as limit:
                        await self.cancel()
                return
            except asyncio.CancelledError:
                # Repeated caller cancellation must not interrupt cleanup.
                continue
            except TimeoutError:
                if limit is not None and limit.expired():
                    self._cancel_timed_out = True
                    return
                raise

    def _cancellation_join_deadline(self) -> float:
        now = time.time()
        boundary = now + 30.0
        shutdown = getattr(self, "_shutdown_deadline_monotonic", None)
        if shutdown is not None:
            # SIGINT latches one absolute grace; never extend it for cleanup.
            boundary = min(boundary, now + max(0.0, shutdown - time.monotonic()))
            floor = now
        else:
            # A normal hard-deadline teardown still allows one short join.
            floor = now + 1.0
        if self._finish_deadline is not None:
            boundary = min(boundary, self._finish_deadline)
        return max(floor, boundary)

    def _absolute_join_deadline(self) -> float | None:
        """The trial's hard clock, beyond which no writer may keep teardown open."""
        original = getattr(self, "_original_deadline", None)
        shutdown = getattr(self, "_shutdown_deadline_monotonic", None)
        if shutdown is not None:
            shutdown_wall = time.time() + max(0.0, shutdown - time.monotonic())
            return min(original, shutdown_wall) if original is not None else shutdown_wall
        if original is not None:
            return original
        return getattr(self, "_finish_deadline", None)

    async def _join_execution(self, execution: asyncio.Future, *, deadline: float | None) -> bool:
        """Join a writer before returning control to a caller that may tear down.

        ``deadline`` bounds only the cooperative wait. A writer that is still
        alive after it must keep this coroutine blocked until it exits or the
        trial's absolute deadline is reached. Python cannot kill a thread, so
        returning at the soft deadline would let the outer arena be torn down
        underneath a live writer. If the writer does exit before the absolute
        deadline, the provisional stop marker is cleared so the controller can
        publish normally. Only a writer still alive at the absolute deadline
        makes the controller skip publication; run_lh's existing fallback
        delivery remains the safety net for that known limitation.
        """
        absolute = self._absolute_join_deadline()
        hard_deadline = absolute if absolute is not None else deadline
        if (
            hard_deadline is not None
            and deadline is not None
            and hard_deadline <= time.time()
            and deadline > hard_deadline
        ):
            # The hard clock already fired and cancellation was requested. Allow
            # only the caller's short cleanup join, never another work admission.
            hard_deadline = deadline
        soft_deadline = deadline
        if soft_deadline is not None and hard_deadline is not None and soft_deadline >= hard_deadline:
            soft_deadline = None
        was_join_timed_out = getattr(self, "_join_timed_out", False)
        invocation = getattr(self, "_invocation", None)
        previous_stop_reason = getattr(invocation, "stop_reason", None)
        previous_failure = getattr(invocation, "failure", None)
        soft_timed_out = False

        def joined() -> bool:
            # A soft timeout is only provisional. Once the writer has really
            # exited before the absolute deadline, roll back the stop marker
            # and let run publish the result it now owns.
            if soft_timed_out and not was_join_timed_out:
                self._clear_join_timeout(previous_stop_reason, previous_failure)
            return True

        while not execution.done():
            now = time.time()
            if soft_deadline is not None and not soft_timed_out and now >= soft_deadline:
                self._record_join_timeout()
                soft_timed_out = True
            if hard_deadline is not None and now >= hard_deadline:
                if execution.done():
                    break
                self._record_join_timeout()
                return False
            wait_until = hard_deadline if soft_timed_out or soft_deadline is None else soft_deadline
            remaining = None if wait_until is None else wait_until - now
            if remaining is not None and remaining <= 0:
                continue
            limit: asyncio.Timeout | None = None
            try:
                if remaining is None:
                    await asyncio.shield(execution)
                else:
                    async with asyncio.timeout(remaining) as limit:
                        await asyncio.shield(execution)
            except asyncio.CancelledError:
                # The caller already asked once; keep cleaning up and re-raise
                # the original cancellation after the writer is joined.
                continue
            except TimeoutError:
                if limit is not None and limit.expired():
                    # The soft deadline only ends cooperative cancellation.
                    # Loop until the writer exits or the hard deadline arrives.
                    continue
                # The execution itself raised TimeoutError; it is no longer a
                # background writer.
                return joined()
            except Exception:
                return joined()
        if execution.cancelled():
            return joined()
        with contextlib.suppress(Exception):
            execution.result()
        return joined()

    def _record_join_timeout(self) -> None:
        self._join_timed_out = True
        invocation = getattr(self, "_invocation", None)
        if invocation is None:
            return
        invocation.stop_reason = "execution_stop_timeout"
        invocation.failure = invocation.failure or {
            "type": "RuntimeStateError",
            "reason": "execution_stop_timeout",
            "detail": "worker did not stop before the finish deadline",
        }

    def _clear_join_timeout(self, stop_reason: str | None, failure: dict[str, object] | None) -> None:
        """Undo a provisional soft-timeout marker after the writer really exits."""
        self._join_timed_out = False
        invocation = getattr(self, "_invocation", None)
        if invocation is None:
            return
        invocation.stop_reason = stop_reason
        invocation.failure = failure

    async def control_planning_call(self, command):
        """Dispatch one exact inspector command without changing the Work budget."""
        if not callable(getattr(self._plugin, "control_planning_call", None)):
            raise RuntimeContractError("plugin has no native planning call controller")
        return await self._plugin.control_planning_call(command)

    async def cancel(self) -> None:
        """Request a cooperative stop; admitted calls are never refunded.

        Model adapters stop cooperatively. An admitted public check is a Core
        subprocess in its own session, so it receives TERM immediately and KILL
        after a short grace instead of outliving explicit cancellation, memory
        pressure or the hard deadline.
        """
        self.request_shutdown(getattr(self, "_shutdown_deadline_monotonic", None))
        await asyncio.to_thread(terminate_check_processes)
        plugin_error = None
        try:
            await self._plugin.cancel()
        except Exception as exc:
            plugin_error = exc
        adapter_results = await asyncio.gather(
            *(adapter.cancel_current_turn() for adapter in self._adapters), return_exceptions=True
        )
        if plugin_error is not None:
            raise plugin_error
        for result in adapter_results:
            if isinstance(result, Exception):
                raise result

    def request_shutdown(self, deadline_monotonic: float | None) -> None:
        """Latch a single SIGINT deadline before awaiting plugin teardown."""
        if deadline_monotonic is not None and getattr(self, "_shutdown_deadline_monotonic", None) is None:
            self._shutdown_deadline_monotonic = deadline_monotonic
            setattr(self._plugin, "_shutdown_deadline_monotonic", deadline_monotonic)
            base = getattr(self._plugin, "base", None)
            if base is not None:
                setattr(base, "_shutdown_deadline_monotonic", deadline_monotonic)
        self._abort.set()
        question_cancelled = getattr(self, "_external_question_cancelled", None)
        if question_cancelled is not None:
            question_cancelled.set()

    def _run_async(self, coroutine: Any) -> Any:
        assert self._loop is not None
        try:
            return asyncio.run_coroutine_threadsafe(coroutine, self._loop).result()
        except (FutureCancelledError, asyncio.CancelledError):
            if self._abort.is_set():
                raise self._artifacts.stopped("invocation stopped during clarification") from None
            raise

    # -- execution (Core thread) --------------------------------------

    def _execute(self, runtime: WorkRuntime, output: Path) -> dict[str, object]:
        args = self._args
        if args.reviewer_launch_policy is None:
            args = replace(args, reviewer_launch_policy=ReviewerLaunchPolicy.from_args(args))
            self._args = args
        task_bytes = args.task.expanduser().resolve(strict=True).read_bytes()
        task_text = task_bytes.decode("utf-8")
        if self._abort.is_set():
            raise FutureCancelledError()
        resolved = self._resolve_execution(runtime.workspace)
        if self._abort.is_set():
            raise FutureCancelledError()
        binding: dict[str, object] = {
            "schema": self._plugin.spec.execution_schema,
            "fingerprint": resolved.fingerprint,
            "summary": resolved.summary,
        }
        frozen = self._frozen_input(task_bytes, output)
        self._cli_frozen = frozen
        if self._abort.is_set():
            raise FutureCancelledError()
        state = self._open_work(runtime, frozen, binding, task_text)
        self._invocation.state = state
        if args.on_external_work_ready is not None:
            args.on_external_work_ready()
        configure_side_call_integrity_state(
            side_call_integrity_state_path(runtime.workspace, state.work_id),
            work_id=state.work_id,
            controller=self,
            plugin=self._plugin,
        )
        if self._gate is not None:
            self._gate.restore(state, runtime.workspace)
        if args.workflow == "suite" and "long_horizon_suite" in state.advisory:
            from .suite import validate_preparation

            try:
                validate_preparation(self, output, state)
            except self._artifacts.exhausted as exc:
                if public_review_failure(exc) is None:
                    raise
                state = self._save_public_review_p0(runtime, state, exc, clear_suite=True)
                self._invocation.state = state
                return frozen
        if state.delivery is not None:
            self._invocation.stop_reason = "delivered"
            return frozen
        if self._rpc_public_check and (
            self._check_deadline is None or self._check_receipts is None or self._gate is None
        ):
            # Missing original-clock/bind metadata is a configuration gap, not
            # permission to mint a local clock, probe roles or run more models.
            if self._phase(state) == "created":
                turn_root = self._turn_root(output, state.work_id)
                store = self._artifacts.create_store(turn_root.parent.parent)
                state = self._freeze_s0(runtime, output, turn_root, store, task_text, state)
            state = self._save_host_check(runtime, state, self._read_host_check(state, "configuration"))
            self._stop_for_public_check(state)
            self._invocation.state = state
            return frozen
        # A checkpoint restore is a handoff back to the Core writer. Validate
        # any recovered guard once, then let recovery rebuild the candidate.
        release_recovered_views((runtime.workspace,))
        try:
            state = runtime.recover(state.work_id, expected_revision=state.revision)
        except RuntimeStateError as exc:
            if not (
                args.resume_rotated_external_session
                and self._phase(state) == "p1_started"
                and str(exc) == "not_auto_resumable: external executor has no completed checkpoint"
            ):
                raise
            from .suite import close_interrupted_p1

            state = close_interrupted_p1(self, runtime, output, state)
        if args.work_id and args.workflow == "suite" and state.delivery is None and not self._abort.is_set():
            from .suite import register_suite_checks

            state = register_suite_checks(runtime, state, upgrade_registered=True)
            if args.external_live_workdir is None and not args.external_regression_context and state.verification:
                # Native CLI continuation owns a single Work. Read only its
                # current candidate's public checks after registration/recovery.
                from .regression_handoff import merge_regressions, worker_regression_context

                context = worker_regression_context(merge_regressions(state=state))
                args = replace(args, external_regression_context=context)
                self._args = args
        self._invocation.state = state
        if state.delivery is None:
            state = self._restore_early_audit(runtime, output, state)
        if state.delivery is not None:
            self._invocation.stop_reason = "delivered"
            return frozen
        if (
            state.acceptance is not None
            and state.delivery is None
            and self._early_audit_finished
            and _public_passed(state)
            and (
                self._early_audit_replaced
                or self._early_audit_candidate_is_fallback(state)
            )
        ):
            # New acceptance was already persisted before a crash; finish the
            # interrupted delivery instead of restarting the audit.
            try:
                state = runtime.deliver(
                    state.work_id, expected_revision=state.revision, acceptance=state.acceptance
                )
                self._invocation.state = state
                self._invocation.stop_reason = "accepted"
                return frozen
            except Exception:
                self._early_audit_closed = True
                self._invocation.stop_reason = "accepted_fallback_retained"
        if state.status == "paused" and not self._abort.is_set():
            state = runtime.resume(state.work_id, expected_revision=state.revision)
            self._invocation.state = state
        config = args.config()

        def review_checkpoint() -> tuple[Path, str] | None:
            current = self._invocation.state
            if current is None:
                return None
            return runtime.workspace, current.candidate.checkpoint

        reviewer_protected_roots = (
            runtime.workspace,
            Path(args.output) / "exports",
            Path(args.output) / "selection.json",
            Path(args.output) / "records",
            Path(args.output) / "operation-timings",
        )
        self._adapters = (
            _WorkerDeadlineGuard(
                RecoveredViewHandoffAdapter(
                    self._make_adapter("worker", resolved),
                    protected_roots=reviewer_protected_roots,
                ),
                self,
                regression_context=(
                    args.external_regression_context
                    if args.workflow == "suite" and args.external_live_workdir is None
                    else ""
                ),
            ),
            IsolatedExecutableReviewer(
                self._make_adapter("reviewer", resolved),
                root=Path(args.output),
                launch_policy=ReviewerLaunchPolicy.from_args(args),
                # The source checkout and every controller-owned delivery
                # surface are outside the Reviewer's writable copy.
                protected_roots=reviewer_protected_roots,
                checkpoint_provider=review_checkpoint,
            ),
        )
        worker, reviewer = self._adapters
        budget = create_budget(
            runtime.workspace / ".pact" / "bench" / "trials", config, state.work_id, args.budget_started_at_unix
        )
        self._budget = budget
        work_inputs: dict[str, object] = {
            "contract": state.contract,
            "constraints": list(state.constraints),
            "policy": state.policy.payload(),
            "worker_adapter": worker.id,
            "reviewer_adapter": reviewer.id,
        }
        budget.bind_work_inputs(work_inputs)
        if args.workflow == "suite":
            identity = {
                "trial_id": args.trial_id,
                "workspace": str(args.external_live_workdir or runtime.workspace),
                "external": args.external_live_workdir is not None,
                "started_at": budget.snapshot()["started_at"],
                "total_seconds": args.budget_seconds,
                "cutoff_at_unix": args.cutoff_at_unix,
                "window_seconds": args.window_seconds,
                "finish_reserve_seconds": args.finish_reserve_seconds,
                "backend": self._plugin.spec.id,
                "execution": resolved.fingerprint,
                "reviewer": reviewer.id,
                "model": args.model,
                "profile": args.profile_name,
                "reviewer_uid": args.reviewer_uid,
                "reviewer_gid": args.reviewer_gid,
                "reviewer_execution": args.reviewer_execution,
                "workflow": args.workflow,
                "verify_command_sha256": hashlib.sha256((args.verify_command or "").encode()).hexdigest(),
                "turn_timeout_seconds": args.turn_timeout_seconds,
                "control_profile": args.control_profile,
                "review_limits": list(args.review_limits()),
            }
            work_id = state.work_id
            self._acceptance_timings = AcceptanceTimings(output, identity, lambda: runtime.read_work(work_id))
            runtime.acceptance_timings = self._acceptance_timings
        bind_budget = getattr(self._plugin, "bind_trial_budget", None)
        if bind_budget is not None:
            bind_budget(budget)
        if args.workflow == "suite":
            from .suite import GuidedAdapter, ManagedReviewer

            worker = GuidedAdapter(worker, self, runtime, resolved, budget)
            reviewer = ManagedReviewer(reviewer, self, runtime, resolved, budget)
            self._adapters = (self._adapters[0], reviewer)
        turns_used = 0
        window: BudgetWindow | None = None
        if self._phase(state) != "first_review_done":
            with budget.execution():
                window = self._soft_window(budget, args.window_seconds)
                if window is None:
                    self._invocation.stop_reason = "window_budget_exhausted"
                    return frozen
                self._window = window
                self._initialize_phase_policy(output, budget, window)
                state, turns_used = self._bootstrap(runtime, output, budget, window, task_text, resolved, state)
                state = self._answer_external_questions(runtime, state)
        else:
            # A resumed first_review_done invocation has no outer window. Rebuild
            # the admitted absolute boundary from the persisted deadline instead
            # of opening a fresh window (which would extend the trial). The same
            # policy initialization applies to this recovery entry.
            window = self._restored_budget_window(budget)
            self._window = window
            self._initialize_phase_policy(output, budget, window)
        self._invocation.state = state
        if self._abort.is_set() and self._invocation.stop_reason == "not_started":
            self._invocation.stop_reason = "invocation_stopped"
        if self._abort.is_set() or self._phase(state) != "first_review_done":
            return frozen
        blocked_on_entry = _acceptance_blocked_only_by_questions(state)
        if blocked_on_entry and args.allow_user_interaction:
            self._invocation.stop_reason = "acceptance_blocked_unresolved_questions"
            return frozen
        # Suite's implementation cap is cumulative across explicit resumes.
        # The budget ledger also retains failed/admitted implementation calls.
        if args.workflow == "suite":
            turns_used = sum(call.get("purpose") == "implementation" for call in budget.snapshot()["calls"])
        remaining_turns = None if args.max_turns is None else args.max_turns - turns_used
        window_deadline = self._driver_window_seconds(window, budget)
        if window is not None and window_deadline is not None and window_deadline <= budget.clock():
            self._invocation.stop_reason = "window_budget_exhausted"
            return frozen
        if (
            remaining_turns is not None
            and remaining_turns < 1
            and state.acceptance is None
            and not (blocked_on_entry and not args.allow_user_interaction)
        ):
            self._invocation.stop_reason = "max_turns"
            return frozen
        acceptance_blocked = False
        no_progress = False
        unresolved_check: dict[str, object] | None = None
        resolution_control = {"worker_after_resolution": False}
        progress_guard = ReviewProgressGuard(
            review_interval=1 if args.review_each_round else args.review_interval,
            path=output / "review-progress.json",
        )

        def last_review_purpose(snapshot: dict[str, object]) -> str:
            for call in reversed(cast(list[dict[str, object]], snapshot.get("calls", []))):
                if call.get("role") == "reviewer" and call.get("purpose") in {
                    "diagnostic",
                    "feedback",
                    "acceptance",
                }:
                    return str(call["purpose"])
            return "diagnostic"

        def current_time_admission() -> object:
            snapshot = budget.snapshot()
            soft_deadline = window.deadline if window is not None else float(snapshot["deadline"])
            admission = time_admission(
                now=budget.clock(),
                soft_deadline=soft_deadline,
                original_deadline=float(snapshot["deadline"]),
                total_seconds=float(self._args.budget_seconds),
                snapshot=snapshot,
                check_durations=self._check_times(),
                full_acceptance=self._full_acceptance_times(),
            )
            self._review_governance["last_time_admission"] = admission.evidence()
            self._review_governance["soft_deadline_unix"] = round(soft_deadline, 3)
            return admission

        def review_gate(_state: object, _budget: object, _window: object, purpose: str) -> str | None:
            if self._model_window_closed():
                return "window_budget_exhausted"
            if not self._time_admission_active():
                # Sub-hour fixtures use an explicit turn budget; the long-horizon
                # time admission is meaningful only for the real multi-hour runs.
                return "diagnosis_paused" if purpose == "diagnostic" and progress_guard.diagnosis_paused else None
            admission = current_time_admission()
            if purpose == "acceptance":
                return None if admission.acceptance_allowed else "review_time_admission"
            if purpose == "diagnostic" and progress_guard.diagnosis_paused:
                return "diagnosis_paused"
            return None if admission.diagnosis_allowed else "review_time_admission"

        def worker_gate(current: WorkSnapshot, _budget: object, _window: object) -> str | None:
            if self._model_window_closed():
                return "window_budget_exhausted"
            skipped_acceptance_reserve = False
            if self._phase_policy is not None and self._budget is not None:
                evidence = self._phase_policy.worker_deadline_evidence()
                if self._budget.clock() >= float(evidence["deadline"]):
                    return "window_budget_exhausted"
                skipped_acceptance_reserve = bool(evidence["acceptance_reserve_skipped"])
            if not self._time_admission_active():
                return None
            admission = current_time_admission()
            if skipped_acceptance_reserve:
                # The Worker cutoff already used the overload branch: only the
                # seal/check reserve is held back, so do not let the larger A
                # estimate suppress the launch entirely.
                return None
            if admission.worker_allowed:
                return None
            # A fresh continuation restores these same absolute deadlines and
            # the durable operation estimates. The bounded profile can change its reserve as
            # time passes, so its denial cannot establish this disposition.
            if (
                args.work_id == current.work_id
                and args.expected_revision == current.revision
                and args.external_live_workdir is not None
                and self._phase_policy is None
                and (self._acceptance_timings is not None or not self._public_check_durations)
                and window is not None and window.deadline == args.soft_deadline_unix
                and budget.snapshot()["deadline"] == args.original_deadline_unix
                and current.verification is not None
                and current.verification.candidate == current.candidate
                and current.verification.status == "failed"
                and not current.unresolved_assumptions
            ):
                self._invocation.fixed_window_wait_revision = current.revision
            return "review_time_admission"

        def stop_driver() -> bool:
            nonlocal acceptance_blocked, no_progress, unresolved_check
            if self._abort.is_set() or no_progress or self._model_window_closed():
                return True
            current = runtime.read_work(state.work_id)
            if current.verification is not None and current.verification.candidate == current.candidate:
                route = self._read_host_check(current, "pact")
                if route is not None and route["stop_unresolved"] is True:
                    # A stop callback must not change Core's revision while the
                    # driver continues. Persist only after it has stopped.
                    unresolved_check = route
                    self._halt_public_check(str(route["candidate_checkpoint"]))
                    return True
            if _acceptance_blocked_only_by_questions(current):
                if not self._args.allow_user_interaction and resolution_control["worker_after_resolution"]:
                    # A clarification round classified at least one question as
                    # an implementation gap. Permit the requested Worker turn
                    # before the next question-only stop.
                    resolution_control["worker_after_resolution"] = False
                    return False
                # Distinguish our scheduling stop from a caller's cancellation.
                acceptance_blocked = True
                return True
            snapshot = budget.snapshot()
            completed_workers = sum(
                call.get("purpose") == "implementation" and call.get("status") == "completed"
                for call in cast(list[dict[str, object]], snapshot["calls"])
            )
            purpose = last_review_purpose(snapshot)
            if progress_guard.observe(current, completed_workers, purpose=purpose):
                no_progress = True
                (output / "no-progress-stop.json").write_text(
                    json.dumps(progress_guard.evidence(), indent=2, sort_keys=True) + "\n"
                )
                return True
            self._review_governance["progress_guard"] = progress_guard.evidence()
            return False

        driver_type = ReviewLimitedDriver
        if (
            self._plugin.evidence_configuration()
            .get("public_verification", "")
            .startswith("terminal-candidate-review/")
        ):
            from .suite import CandidateReviewDriver

            driver_type = CandidateReviewDriver

        def refuse() -> None:
            self._raise_native_turn_refusal(window, budget)

        def should_defer_delivery() -> bool:
            if self._early_audit_finished or self._early_audit_closed:
                return False
            current = runtime.read_work(state.work_id)
            return self._early_audit_triggered(current, budget)

        driver_runtime = DeferredDeliveryRuntime(runtime, should_defer_delivery)
        driver = driver_type(
            driver_runtime,
            LedgerStatusBoundary(_NativeTurnRefusalGuard(worker, refuse)),
            LedgerStatusBoundary(_NativeTurnRefusalGuard(reviewer, refuse)),
            config=config,
        )
        driver.review_admissions = self._review_admissions
        driver.review_gate = review_gate
        driver.worker_gate = worker_gate
        self._review_governance["progress_guard"] = progress_guard.evidence()
        if self._early_audit_closed:
            self._invocation.stop_reason = str(
                (self._early_audit_evidence or {}).get("stop_reason") or "accepted_fallback_retained"
            )
            return frozen
        if state.acceptance is not None and state.delivery is None:
            state, handled = self._accept_or_audit(
                runtime, output, budget, window, state, driver
            )
            self._invocation.state = state
            if handled:
                return frozen
        # Reuse the exact admitted absolute window instead of measuring a fresh
        # remaining duration, which would move the deadline later during setup.
        # Core's driver uses execution_deadline for its own verify_candidate,
        # so that field carries the hard clock for finish work; new Worker calls
        # stay bounded by the soft gate and the reserved Worker deadline.
        driver_window = window
        check_deadline = getattr(self, "_check_deadline", None)
        if window is not None and check_deadline is not None:
            driver_window = replace(window, execution_deadline=float(check_deadline))
        pin = _pinned_budget_window(driver_window) if driver_window is not None else contextlib.nullcontext()
        with pin:
            if blocked_on_entry:
                # The Work was already blocked at the first review. Do not replay
                # the driver; resolve questions first, then accept or continue.
                result = driver._result(state, "invocation_stopped", budget)
            else:
                result = driver.run(
                    state.work_id,
                    expected_revision=state.revision,
                    max_turns=None if remaining_turns is None else max(1, remaining_turns),
                    window_seconds=None if window is not None else window_deadline,
                    stop_requested=stop_driver,
                )
            self._invocation.driver_result = result
            self._invocation.state = result.snapshot
            if result.snapshot.acceptance is not None:
                audited, handled = self._accept_or_audit(
                    runtime, output, budget, window, result.snapshot, driver
                )
                self._invocation.state = audited
                if handled:
                    return frozen
                result = driver._result(audited, self._invocation.stop_reason, budget)
                self._invocation.driver_result = result
            if result.snapshot.acceptance is None and self._gate is not None:
                # Soft/hard proximity must not sacrifice a sealed candidate that
                # has not had its one host check. This is not new model work.
                checked = self._check_priority_candidate(runtime, result.snapshot)
                if checked is not result.snapshot:
                    result = driver._result(checked, result.stop_reason, budget)
                    self._invocation.driver_result = result
                    self._invocation.state = checked
                    if self._stop_for_public_check(checked):
                        return frozen
            if not args.allow_user_interaction and (
                acceptance_blocked or _acceptance_blocked_only_by_questions(result.snapshot)
            ):
                result = self._resolve_unattended_questions(
                    runtime,
                    output,
                    budget,
                    window,
                    task_text,
                    resolved,
                    driver,
                    result.snapshot,
                    result,
                    control=resolution_control,
                    stop_driver=stop_driver,
                    window_deadline=window_deadline,
                )
                self._invocation.driver_result = result
                self._invocation.state = result.snapshot
        methods, other_failures = method_diagnosis(result.snapshot)
        if result.stop_reason in {"review_time_admission", "review_fuse_reached", "review_limit_reached"} and (
            not methods or other_failures
        ):
            accepted_state, accepted = self._try_final_acceptance(
                runtime, output, budget, window, result.snapshot, driver
            )
            if accepted:
                result = driver._result(accepted_state, "accepted", budget)
                self._invocation.driver_result = result
                self._invocation.state = accepted_state
            else:
                result = driver._result(accepted_state, result.stop_reason, budget)
                self._invocation.driver_result = result
        if result.stop_reason in {"review_time_admission", "review_fuse_reached", "review_limit_reached"} and (
            not methods or other_failures
        ):
            repaired, repair_entered = self._repair_after_review_quota(
                runtime, output, budget, window, result.snapshot, driver
            )
            if repair_entered:
                # No Reviewer ran in this mode, so the work remains unaccepted.
                result = driver._result(repaired, result.stop_reason, budget)
                self._invocation.driver_result = result
        self._invocation.state = result.snapshot
        if self._args.external_question_channel is not None:
            answered = self._answer_external_questions(runtime, result.snapshot)
            self._invocation.state = answered
            if answered.revision != result.snapshot.revision:
                self._invocation.stop_reason = "external_question_answered"
                return frozen
        if unresolved_check is not None:
            current = self._save_host_check(runtime, result.snapshot, unresolved_check)
            self._invocation.state = current
            self._stop_for_public_check(current)
            return frozen
        stopped_for_progress = (
            progress_guard.stop_reason or "no_progress_repeated_reviews"
        )
        if args.allow_user_interaction:
            self._invocation.stop_reason = (
                stopped_for_progress
                if no_progress and result.stop_reason == "invocation_stopped" and not self._abort.is_set()
                else "acceptance_blocked_unresolved_questions"
                if (
                    acceptance_blocked
                    and result.stop_reason == "invocation_stopped"
                    and not self._abort.is_set()
                    and _acceptance_blocked_only_by_questions(result.snapshot)
                )
                else result.stop_reason
            )
        else:
            self._invocation.stop_reason = (
                stopped_for_progress
                if no_progress and result.stop_reason == "invocation_stopped" and not self._abort.is_set()
                else "window_budget_exhausted"
                if (
                    result.stop_reason == "invocation_stopped"
                    and not self._abort.is_set()
                    and self._model_window_closed()
                )
                else result.stop_reason
            )
        return frozen

    # -- early acceptance coverage audit -------------------------------

    def _early_audit_candidate_identity(self, candidate: Any) -> dict[str, object]:
        return {
            "work_id": str(candidate.work_id),
            "artifact": str(candidate.artifact),
            "checkpoint": str(candidate.checkpoint),
            "tree": str(candidate.tree),
        }

    def _early_audit_candidate_is_fallback(self, state: WorkSnapshot) -> bool:
        fallback = self._early_audit_fallback
        if not isinstance(fallback, dict):
            return False
        checkpoint = fallback.get("candidate_checkpoint")
        tree = fallback.get("candidate_tree")
        if not isinstance(checkpoint, str) or checkpoint != state.candidate.checkpoint:
            return False
        return not isinstance(tree, str) or tree == state.candidate.tree

    def _restore_early_audit(
        self, runtime: WorkRuntime, output: Path, state: WorkSnapshot
    ) -> WorkSnapshot:
        """Recover the frozen fallback and persist the exact interrupted decision.

        An ``audit_started`` record is never treated as a fresh audit: either a
        persisted acceptance is finished, or the already bounded audit is closed
        against its frozen fallback. Rounds and the original deadline survive.
        """
        raw = state.advisory.get("early_acceptance_audit")
        if not isinstance(raw, dict) or raw.get("schema") != EARLY_AUDIT_SCHEMA:
            return state
        self._early_audit_evidence = raw
        fallback = raw.get("fallback")
        if isinstance(fallback, dict):
            acceptance = fallback.get("acceptance")
            if isinstance(acceptance, dict):
                self._early_audit_acceptance = json.loads(json.dumps(acceptance, default=str))
            verification = fallback.get("verification")
            if isinstance(verification, dict):
                self._early_audit_verification = json.loads(json.dumps(verification, default=str))
            export = fallback.get("export")
            if isinstance(export, str) and export:
                try:
                    from .harbor_unified.delivery import receipt_from_artifact

                    receipt = receipt_from_artifact(output / export)
                    verify_export(receipt)
                    self._early_audit_fallback_receipt = receipt
                    self._early_audit_fallback = {
                        "candidate_tree": receipt.candidate.tree,
                        "candidate_checkpoint": receipt.candidate.checkpoint,
                        "export": export,
                        "export_sha256": receipt.artifact_sha256,
                    }
                except Exception:
                    self._early_audit_fallback_receipt = None
                    self._early_audit_fallback = None
        status = raw.get("status")
        self._early_audit_replaced = status == "replacement_accepted" or raw.get("replaced_fallback") is True
        if status == "not_triggered":
            # The first acceptance was already evaluated against the trigger
            # thresholds and correctly skipped. It remains an ordinary accepted
            # Work; a resume must not invent an audit after the fact.
            self._early_audit_finished = True
            return state
        terminal = {"accepted", "accepted_fallback", "replacement_accepted", "fallback_retained"}
        if status in terminal:
            self._early_audit_finished = True
            can_still_deliver = (
                state.delivery is None
                and state.acceptance is not None
                and state.acceptance.candidate == state.candidate
                and _public_passed(state)
                and (self._early_audit_replaced or self._early_audit_candidate_is_fallback(state))
            )
            if not self._early_audit_replaced and state.delivery is None and not can_still_deliver:
                self._early_audit_closed = True
            return state
        if status != "audit_started":
            # Unknown lifecycle states are closed conservatively, never replayed
            # from round one against a later candidate.
            evidence = dict(raw)
            evidence.update(
                status="fallback_retained",
                replaced_fallback=False,
                stop_reason="accepted_fallback_retained",
                audit_stop_reason="resume_unknown_audit_state",
                finished_at_unix=round(time.time(), 3),
            )
            self._early_audit_finished = True
            self._early_audit_closed = True
            with contextlib.suppress(Exception):
                state = self._flush_early_audit(runtime, output, state, evidence)
            return state
        if state.delivery is not None:
            self._early_audit_finished = True
            self._early_audit_replaced = not self._early_audit_candidate_is_fallback(state)
            return state
        if (
            state.acceptance is not None
            and state.acceptance.candidate == state.candidate
            and _public_passed(state)
        ):
            evidence = dict(raw)
            evidence["rounds"] = list(raw.get("rounds") or [])
            if self._early_audit_candidate_is_fallback(state):
                evidence.update(
                    status="accepted_fallback",
                    replaced_fallback=False,
                    stop_reason="accepted",
                    audit_stop_reason=str(raw.get("audit_stop_reason") or "audit_clean"),
                    finished_at_unix=round(time.time(), 3),
                )
                self._early_audit_replaced = False
            else:
                evidence.update(
                    status="replacement_accepted",
                    replaced_fallback=True,
                    replacement={
                        "candidate_tree": state.candidate.tree,
                        "candidate_checkpoint": state.candidate.checkpoint,
                        "acceptance": self._json_value(state.acceptance),
                    },
                    stop_reason="replacement_accepted",
                    audit_stop_reason="replacement_accepted",
                    finished_at_unix=round(time.time(), 3),
                )
                self._early_audit_replaced = True
            self._early_audit_finished = True
            with contextlib.suppress(Exception):
                state = self._flush_early_audit(runtime, output, state, evidence)
            return state
        # No persisted acceptance: close the already-started bounded audit to its
        # immutable fallback. Do not reset rounds or mint a new deadline.
        evidence = dict(raw)
        evidence["rounds"] = list(raw.get("rounds") or [])
        evidence.update(
            status="fallback_retained",
            replaced_fallback=False,
            stop_reason="accepted_fallback_retained",
            audit_stop_reason=str(raw.get("audit_stop_reason") or "resumed_audit_closed"),
            finished_at_unix=round(time.time(), 3),
        )
        self._early_audit_finished = True
        self._early_audit_closed = True
        self._invocation.stop_reason = "accepted_fallback_retained"
        with contextlib.suppress(Exception):
            state = self._flush_early_audit(runtime, output, state, evidence)
        return state

    def _early_audit_triggered(self, state: WorkSnapshot, budget: BenchBudget) -> bool:
        if self._early_audit_finished or self._early_audit_closed:
            return False
        if state.delivery is not None or state.acceptance is None or self._abort.is_set():
            return False
        snapshot = budget.snapshot()
        remaining = snapshot.get("remaining_seconds")
        if not isinstance(remaining, (int, float)) or isinstance(remaining, bool):
            return False
        return self._args.early_audit_policy().triggered(
            remaining_seconds=float(remaining), total_seconds=float(self._args.budget_seconds)
        )

    def _early_audit_not_triggered_reason(self, state: WorkSnapshot, budget: BenchBudget) -> str:
        policy = self._args.early_audit_policy()
        if self._early_audit_finished or self._early_audit_closed:
            return "audit_already_terminal"
        if self._abort.is_set():
            return "invocation_stopped"
        if policy.max_rounds <= 0:
            return "audit_disabled"
        snapshot = budget.snapshot()
        remaining = snapshot.get("remaining_seconds")
        total = self._args.budget_seconds
        if (
            isinstance(remaining, bool)
            or not isinstance(remaining, (int, float))
            or not math.isfinite(float(remaining))
        ):
            return "remaining_budget_unavailable"
        if (
            isinstance(total, bool)
            or not isinstance(total, (int, float))
            or not math.isfinite(float(total))
            or float(total) <= 0
        ):
            return "total_budget_unavailable"
        if float(remaining) <= float(total) * policy.min_remaining_ratio:
            return "remaining_ratio_not_above_threshold"
        if float(remaining) < policy.min_remaining_seconds:
            return "remaining_seconds_below_threshold"
        return "trigger_conditions_not_met"

    def _record_early_audit_not_triggered(
        self,
        runtime: WorkRuntime,
        output: Path,
        state: WorkSnapshot,
        budget: BenchBudget,
    ) -> WorkSnapshot:
        """Persist an explicit non-trigger decision instead of leaving null."""
        if self._early_audit_finished or self._early_audit_closed:
            return state
        policy = self._args.early_audit_policy()
        snapshot = budget.snapshot()
        remaining = snapshot.get("remaining_seconds")
        total = self._args.budget_seconds
        remaining_value = (
            round(float(remaining), 3)
            if isinstance(remaining, (int, float)) and not isinstance(remaining, bool)
            else None
        )
        total_value = (
            round(float(total), 3)
            if isinstance(total, (int, float)) and not isinstance(total, bool)
            else None
        )
        evidence: dict[str, object] = {
            "schema": EARLY_AUDIT_SCHEMA,
            "status": "not_triggered",
            "triggered": False,
            "reason": self._early_audit_not_triggered_reason(state, budget),
            "remaining_seconds": remaining_value,
            "total_seconds": total_value,
            "remaining_ratio": (
                round(float(remaining) / float(total), 6)
                if remaining_value is not None
                and total_value is not None
                and float(total) > 0
                else None
            ),
            "trigger": {
                "remaining_seconds": remaining_value,
                "total_seconds": total_value,
                "remaining_ratio": (
                    round(float(remaining) / float(total), 6)
                    if remaining_value is not None
                    and total_value is not None
                    and float(total) > 0
                    else None
                ),
                "min_remaining_ratio": policy.min_remaining_ratio,
                "min_remaining_seconds": policy.min_remaining_seconds,
                "max_rounds": policy.max_rounds,
                "max_seconds": policy.max_seconds,
            },
            "accepted_candidate": self._early_audit_candidate_identity(state.candidate),
            "rounds": [],
            "replaced_fallback": False,
            "stop_reason": "accepted",
            "evaluated_at_unix": round(budget.clock(), 3),
        }
        self._early_audit_finished = True
        with contextlib.suppress(Exception):
            state = self._flush_early_audit(runtime, output, state, evidence)
        return state

    def _accept_or_audit(
        self,
        runtime: WorkRuntime,
        output: Path,
        budget: BenchBudget,
        window: BudgetWindow | None,
        state: WorkSnapshot,
        driver: Any,
    ) -> tuple[WorkSnapshot, bool]:
        """Audit an eligible first acceptance or record why it was skipped.

        Every acceptance path funnels through this gate before delivery so a
        resolution, first-review, driver, final-acceptance or resume path
        cannot bypass the coverage audit.
        """
        if state.acceptance is None:
            return state, False
        if state.delivery is not None:
            # A driver that was allowed to deliver (because the audit was not
            # eligible) still needs an explicit non-trigger record. Completed
            # audits already carry their own terminal evidence.
            if not self._early_audit_finished and not self._early_audit_closed:
                state = self._record_early_audit_not_triggered(runtime, output, state, budget)
            # The driver already delivered the accepted candidate. This gate now
            # owns the invocation outcome, so propagate the same terminal reason
            # the pre-gate driver path published instead of leaving it at its
            # initial ``not_started`` value (which makes Meridian hold for
            # inspection and can drop the publication on operator_stop).
            if self._invocation.stop_reason not in {
                "accepted",
                "replacement_accepted",
                "accepted_fallback_retained",
            }:
                self._invocation.stop_reason = "accepted"
            self._invocation.state = state
            return state, True
        if self._early_audit_triggered(state, budget):
            state, handled = self._run_early_acceptance_audit(
                runtime, output, budget, window, state, driver
            )
            self._invocation.state = state
            if handled:
                return state, True
        else:
            state = self._record_early_audit_not_triggered(runtime, output, state, budget)
            self._invocation.state = state
        # A triggered audit may still decline ownership before freezing (for
        # example an I/O deadline); the already accepted candidate is then
        # published normally. A not-triggered acceptance is published here too.
        if state.acceptance is not None and state.delivery is None:
            state = runtime.deliver(
                state.work_id,
                expected_revision=state.revision,
                acceptance=state.acceptance,
            )
            self._invocation.state = state
            if self._invocation.stop_reason not in {
                "accepted",
                "replacement_accepted",
                "accepted_fallback_retained",
            }:
                self._invocation.stop_reason = "accepted"
        return state, state.delivery is not None

    @staticmethod
    def _json_value(value: object) -> object:
        from dataclasses import is_dataclass

        if is_dataclass(value) and not isinstance(value, type):
            value = asdict(value)
        return json.loads(json.dumps(value, default=str, allow_nan=False))

    def _flush_early_audit(
        self,
        runtime: WorkRuntime,
        output: Path,
        state: WorkSnapshot,
        evidence: dict[str, object],
    ) -> WorkSnapshot:
        """Write structured host evidence; never include model text."""
        rounds = evidence.get("rounds")
        if isinstance(rounds, list):
            evidence["gap_count"] = sum(
                int(row.get("gap_count", 0))
                for row in rounds
                if isinstance(row, dict) and isinstance(row.get("gap_count", 0), int)
            )
        self._early_audit_evidence = evidence
        write_json_atomic(output / "early-acceptance-audit.json", self._json_value(evidence))
        try:
            return runtime.update_advisory(
                state.work_id,
                expected_revision=state.revision,
                advisory={**state.advisory, "early_acceptance_audit": evidence},
            )
        except Exception:
            # Evidence on disk is still authoritative; a resume will not widen
            # the audit because the persisted status is closed.
            return state

    def _early_audit_review_verification(self, state: WorkSnapshot) -> Any:
        verification = state.verification
        if verification is not None and verification.candidate == state.candidate:
            return verification
        return None

    def _legacy_verification_prompt(self, verification: Any) -> str:
        """The historical verification excerpt used by the default profile."""
        return json.dumps(
            self._json_value(verification) if verification is not None else None,
            ensure_ascii=False,
        )[:8000]

    def _verification_prompt_value(self, verification: Any, *, maximum: int | None) -> str:
        """Serialize verification plus the complete deterministic check logs.

        The pinned wheel stores only a short tail in ``VerificationEvidence``.
        The runner also retains every completed check log beside the sealed
        artifact; read those bytes so a larger prompt bound is not a no-op.
        The default profile never takes this path: its callers must preserve
        the historical 8000-character excerpt byte for byte.
        """
        if capacity_scale() == DEFAULT_CAPACITY_SCALE:
            return self._legacy_verification_prompt(verification)
        value = self._json_value(verification) if verification is not None else None
        if verification is not None:
            outputs = read_check_outputs(
                getattr(verification, "artifact", None),
                maximum=None if maximum is None else max(1024, maximum),
            )
            if isinstance(value, dict):
                value = {**value, "complete_check_output": outputs.prompt_block()}
            else:
                value = {"verification": value, "complete_check_output": outputs.prompt_block()}
        encoded = json.dumps(value, ensure_ascii=False)
        return encoded if maximum is None else encoded[:maximum]

    def _early_audit_prompt(self, state: WorkSnapshot) -> str:
        goal = state.contract.get("goal", "")
        suite = state.advisory.get("long_horizon_suite")
        brief = suite.get("brief", "") if isinstance(suite, dict) else ""
        return (
            "[SUITE_COVERAGE_AUDIT]\n"
            "You are the independent executable requirement-coverage auditor. This pass is not acceptance and "
            "you must not edit the candidate. The original task below is authoritative; the current candidate is "
            "a disposable copy that you may build and run.\n\n"
            "Enumerate every requirement in the original task, including secondary behavior such as permissions "
            "and access scope, revocation, error codes, and boundary or empty states. For each item, actually "
            "execute a concrete check in your disposable copy and record the observed result. Do not claim "
            "coverage from source inspection alone and do not invent requirements.\n\n"
            "Return exactly one JSON object inside <pact-coverage-audit>...</pact-coverage-audit> with this shape:\n"
            '{"verdict":"clean"|"gaps",'
            '"official_client_available":true|false,'
            '"checked":[{"requirement":"...","verified":true,'
            '"validation":"official-client-tested"|"self-test-only",'
            '"input":"novel-input-tested"|"public-fixture-only",'
            '"evidence":"command and observed result"}],'
            '"gaps":[{"requirement":"...","expected":"...","observed":"..."}]}\n'
            'Use verdict "clean" only when every requirement was actually verified, gaps is empty, and checked '
            'is a nonempty list of the explicitly verified requirements. Use verdict "gaps" with a concrete gaps '
            'list otherwise. Empty objects, empty checked lists, contradictory text, or prose alone are not a clean '
            "report. For each stateful requirement with a task-provided public reference tool, add the "
            "compare receipt as public_reference_check on its checked row. A clean match needs the current candidate's "
            "Core output. For a newly observed failure in the disposable copy, put the public request/actions "
            "and a reproducible invocation or durable fixture location in the checked row's evidence. "
            "For an unverified comparison, attach the observation there and leave candidate repair gaps for "
            "separate product defects. Omit the field for other requirements. "
            "Report the original task verbatim as the authority.\n\n"
            "Original task:\n"
            + json.dumps(goal, ensure_ascii=False)
            + "\n\nTask brief and evidence (untrusted suggestions):\n"
            + str(brief)[:model_content_limit(24000)]
            + "\n\nCurrent registered checks and public methods:\n"
            + json.dumps(list(state.checks), ensure_ascii=False)[:model_content_limit(12000)]
            + "\n\nPrevious deterministic verification:\n"
            + self._verification_prompt_value(state.verification, maximum=None)
            + reviewer_delivery_hygiene_guidance()
            + audit_generalization_check_guidance()
        )

    def _early_audit_fix_prompt(self, state: WorkSnapshot, gaps: tuple[str, ...]) -> str:
        suite = state.advisory.get("long_horizon_suite")
        brief = suite.get("brief", "") if isinstance(suite, dict) else ""
        return (
            "[SUITE_EARLY_AUDIT_FIX]\n"
            "An already accepted candidate is frozen as the fallback. This bounded repair may replace it only "
            "after a fresh public check and a fresh acceptance. Repair every coverage gap below without weakening "
            "the original task, supplied checks or edge-case behavior. Run the relevant public checks and report "
            "PACT_REVIEW_READY on its own line when the gaps are actually fixed.\n\n"
            "Original task (authority):\n"
            + json.dumps(state.contract.get("goal", ""), ensure_ascii=False)
            + "\n\nCoverage gaps from the executable Reviewer:\n"
            + json.dumps(list(gaps), ensure_ascii=False)[:model_content_limit(20000)]
            + "\n\nTask brief and evidence (untrusted suggestions):\n"
            + str(brief)[:model_content_limit(24000)]
            + "\n\nCurrent public checks and latest deterministic result:\n"
            + json.dumps(list(state.checks), ensure_ascii=False)[:model_content_limit(12000)]
            + "\n"
            + self._verification_prompt_value(state.verification, maximum=None)
            + worker_evidence_guidance()
        )

    def _early_audit_public_check(
        self,
        runtime: WorkRuntime,
        state: WorkSnapshot,
        stage: str,
        deadline: float,
    ) -> WorkSnapshot:
        if self._gate is not None:
            return self._check_candidate(runtime, state, stage, deadline=deadline)
        check_deadline = deadline
        if self._phase_policy is not None:
            check_deadline = min(check_deadline, self._phase_policy.deadline_for("check"))
        if (
            state.status != "completed"
            and self._budget is not None
            and self._budget.clock() < check_deadline
            and not self._abort.is_set()
        ):
            state = runtime.verify_candidate(
                state.work_id, expected_revision=state.revision, deadline=check_deadline
            )
        return state

    def _run_early_acceptance_audit(
        self,
        runtime: WorkRuntime,
        output: Path,
        budget: BenchBudget,
        window: BudgetWindow | None,
        state: WorkSnapshot,
        driver: Any,
    ) -> tuple[WorkSnapshot, bool]:
        """Freeze the accepted fallback, audit coverage, and replace only safely."""
        if not self._early_audit_triggered(state, budget):
            return state, False
        policy = self._args.early_audit_policy()
        snapshot = budget.snapshot()
        remaining = float(snapshot["remaining_seconds"])
        total = float(self._args.budget_seconds)
        now = budget.clock()
        if window is None:
            window = self._soft_window(budget, policy.time_limit(remaining_seconds=remaining, total_seconds=total))
            if window is None:
                return state, False
        deadline = policy.audit_deadline(
            now=now,
            soft_deadline=window.deadline,
            remaining_seconds=remaining,
            total_seconds=total,
        )
        if deadline <= now:
            return state, False
        fallback_identity = self._early_audit_candidate_identity(state.candidate)

        # Freeze the exact accepted candidate through the existing checkpoint
        # and export path before any audit call can change the current Work.
        # Export and snapshot staging are synchronous; bound them by the audit
        # deadline so a slow filesystem cannot consume the fallback publish
        # reserve.
        fallback_dir = output / "stages" / f"accepted-fallback-{uuid4().hex}"

        frozen_parts: dict[str, Any] = {}

        def freeze_fallback() -> tuple[Any, object]:
            verify_recovered_views()
            receipt = runtime.export_candidate(state.candidate, fallback_dir)
            frozen_parts["receipt"] = receipt
            stage, _snapshot = self._artifacts.stage_record(
                output_root=output,
                turn_root=self._turn_root(output, state.work_id),
                stage="accepted-fallback",
                receipt=receipt,
                metadata={
                    "work_id": state.work_id,
                    "revision": state.revision,
                    "candidate_tree": state.candidate.tree,
                    "acceptance": self._json_value(state.acceptance),
                },
                archive_roots=(self._archive_root,) if self._archive_root is not None else (),
            )
            return receipt, stage

        freeze_timed_out = False
        try:
            receipt, stage = _run_before_deadline(deadline, freeze_fallback)
        except _AuditDeadlineExceeded:
            receipt = frozen_parts.get("receipt")
            if receipt is None:
                # The still-current candidate is the accepted fallback. Abandon
                # this audit and let the ordinary driver deliver it.
                self._early_audit_finished = True
                self._early_audit_closed = True
                evidence = {
                    "schema": EARLY_AUDIT_SCHEMA,
                    "status": "audit_aborted",
                    "triggered": True,
                    "rounds": [],
                    "replaced_fallback": False,
                    "stop_reason": "audit_io_deadline",
                    "audit_stop_reason": "audit_freeze_deadline",
                    "current_candidate": fallback_identity,
                }
                self._early_audit_evidence = evidence
                with contextlib.suppress(Exception):
                    write_json_atomic(output / "early-acceptance-audit.json", self._json_value(evidence))
                return state, False
            # Export completed but snapshot staging did not: the sealed receipt
            # is already enough to publish the accepted fallback.
            stage = None
            freeze_timed_out = True
        except Exception:
            # Without a verified fallback the audit must not start.
            return state, False
        acceptance = self._json_value(state.acceptance)
        verification = self._json_value(state.verification) if state.verification is not None else None
        fallback_export = contained(output, receipt.artifact)
        self._early_audit_fallback_receipt = receipt
        self._early_audit_acceptance = acceptance
        self._early_audit_verification = verification if isinstance(verification, dict) else None
        self._early_audit_fallback = {
            "candidate_tree": state.candidate.tree,
            "candidate_checkpoint": state.candidate.checkpoint,
            "export": fallback_export,
            "export_sha256": receipt.artifact_sha256,
            "stage": stage,
        }
        evidence = {
            "schema": EARLY_AUDIT_SCHEMA,
            "status": "audit_started",
            "triggered": True,
            "remaining_seconds": round(remaining, 3),
            "total_seconds": round(total, 3),
            "gap_count": 0,
            "started_at_unix": round(now, 3),
            "current_candidate": fallback_identity,
            "trigger": {
                "remaining_seconds": round(remaining, 3),
                "total_seconds": round(total, 3),
                "remaining_ratio": round(remaining / total, 6) if total > 0 else None,
                "min_remaining_ratio": policy.min_remaining_ratio,
                "min_remaining_seconds": policy.min_remaining_seconds,
                "max_rounds": policy.max_rounds,
                "max_seconds": policy.max_seconds,
                "soft_deadline_unix": round(window.deadline, 3),
                "audit_deadline_unix": round(deadline, 3),
                "audit_seconds": round(deadline - now, 3),
                "publication_reserve_seconds": policy.publication_reserve_seconds,
            },
            "fallback": {
                **self._early_audit_fallback,
                "acceptance": acceptance,
                "verification": verification,
                "frozen_at_unix": round(now, 3),
            },
            "rounds": [],
            "replaced_fallback": False,
            "stop_reason": "audit_started",
            "audit_stop_reason": None,
        }
        if freeze_timed_out:
            evidence.update(
                status="fallback_retained",
                replaced_fallback=False,
                stop_reason="accepted_fallback_retained",
                audit_stop_reason="audit_freeze_deadline",
                finished_at_unix=round(budget.clock(), 3),
            )
            self._early_audit_finished = True
            self._early_audit_closed = True
            self._invocation.stop_reason = "accepted_fallback_retained"
            with contextlib.suppress(Exception):
                state = self._flush_early_audit(runtime, output, state, evidence)
            try:
                if state.acceptance is not None and state.delivery is None:
                    state = runtime.deliver(
                        state.work_id, expected_revision=state.revision, acceptance=state.acceptance
                    )
            except Exception:
                pass
            return state, True
        try:
            state = _run_before_deadline(
                deadline, self._flush_early_audit, runtime, output, state, evidence
            )
        except _AuditDeadlineExceeded:
            self._early_audit_finished = True
            self._early_audit_closed = True
            return state, False
        audit_window = replace(
            window,
            deadline=deadline,
            execution_deadline=deadline,
            reserve_seconds=0.0,
        )
        clean = False
        audit_stop = "audit_rounds_exhausted"
        start_round = len(evidence["rounds"]) + 1
        for round_number in range(start_round, policy.max_rounds + 1):
            if self._abort.is_set():
                audit_stop = "invocation_stopped"
                break
            if self._model_window_closed():
                audit_stop = "window_budget_exhausted"
                break
            if time.time() >= deadline:
                audit_stop = "audit_deadline"
                break
            stopped = budget.stop_reason(deadline)
            if stopped:
                audit_stop = stopped
                break
            evidence["current_candidate"] = self._early_audit_candidate_identity(state.candidate)
            round_evidence: dict[str, object] = {
                "round": round_number,
                "review_admission": "diagnostic",
                "candidate_checkpoint": state.candidate.checkpoint,
                "current_is_fallback": self._early_audit_candidate_is_fallback(state),
            }
            decision = self._review_admissions.admit(
                state,
                purpose="diagnostic",
                full=False,
                force=True,
                check=lambda: budget.stop_reason(deadline, purpose="diagnostic"),
            )
            if not decision["admitted"]:
                round_evidence.update(
                    audit_status="not_admitted",
                    review_limit_reason=decision.get("reason") or decision.get("stop"),
                )
                evidence["rounds"].append(round_evidence)
                audit_stop = str(decision.get("stop") or "audit_review_limit_reached")
                break
            wrapped = BudgetedAdapter(self._adapters[1], budget, purpose="diagnostic", deadline=deadline)
            try:
                state = runtime.review_candidate(
                    state.work_id,
                    expected_revision=state.revision,
                    reviewer=wrapped,
                    timeout_seconds=self._args.turn_timeout_seconds,
                    context=self._early_audit_prompt(state),
                    deadline=deadline,
                    verification_evidence=self._early_audit_review_verification(state),
                )
            except Exception as exc:
                round_evidence.update(audit_status="error", error_type=type(exc).__name__)
                evidence["rounds"].append(round_evidence)
                audit_stop = "audit_review_error"
                break
            if not wrapped.results:
                round_evidence.update(audit_status="not_executed")
                evidence["rounds"].append(round_evidence)
                audit_stop = wrapped.stopped or "audit_review_not_executed"
                break
            review = state.review
            invalid = (
                review is None
                or review.candidate != state.candidate
                or bool(
                    set(review.reasons)
                    & {
                        "reviewer_mutation",
                        "budget_exhausted",
                        "reviewer_output_missing",
                        "reviewer_protocol_error",
                    }
                )
            )
            if invalid:
                round_evidence.update(audit_status="invalid_review")
                evidence["rounds"].append(round_evidence)
                audit_stop = "audit_review_invalid"
                break
            reference_checks = recorded_reference_checks(state)
            report = parse_audit_report(review.feedback, reference_receipts=reference_checks)
            if reference_checks or report.unverified:
                round_evidence["public_reference"] = {
                    "candidate_checkpoint": state.candidate.checkpoint,
                    "recorded_check_count": len(reference_checks),
                    "unverified": list(report.unverified),
                }
            if report.unverified and not report.gaps:
                round_evidence.update(audit_status="unverified", worker_status="not_started", gap_count=0)
                evidence["rounds"].append(round_evidence)
                audit_stop = "public_reference_unverified"
                break
            round_evidence.update(
                gap_count=report.gap_count,
                checked_count=len(report.checked),
                audit_status="parse",
            )
            if not report.usable:
                # Loose, empty or contradictory output is not clean. Treat it as
                # an unresolved investigation item and let the bounded repair
                # Worker act on it; otherwise the loop ends at the fallback.
                round_evidence["audit_status"] = "unusable"
                report = AuditReport(
                    gaps=("Reviewer did not return a usable structured coverage report.",),
                    usable=True,
                    clean=False,
                )
                round_evidence["gap_count"] = report.gap_count
            if report.clean:
                round_evidence["audit_status"] = "clean"
                evidence["rounds"].append(round_evidence)
                clean = True
                audit_stop = "audit_clean"
                state = self._flush_early_audit(runtime, output, state, evidence)
                break
            round_evidence["audit_status"] = "gaps"
            if budget.stop_reason(deadline) or time.time() >= deadline:
                audit_stop = str(budget.stop_reason(deadline) or "audit_deadline")
                evidence["rounds"].append(round_evidence)
                break
            if self._model_window_closed():
                round_evidence.update(worker_status="not_started", reason="window_budget_exhausted")
                evidence["rounds"].append(round_evidence)
                audit_stop = "window_budget_exhausted"
                break
            try:
                release_recovered_views((runtime.workspace,))
                handle = runtime.begin_execution(
                    state.work_id,
                    expected_revision=state.revision,
                    context="[SUITE_EARLY_AUDIT_FIX]",
                    draft=True,
                )
            except Exception as exc:
                round_evidence.update(worker_status="not_started", error_type=type(exc).__name__)
                evidence["rounds"].append(round_evidence)
                audit_stop = "audit_worker_not_started"
                break
            fix_prompt = self._early_audit_fix_prompt(state, report.gaps)
            wrapped_worker = BudgetedAdapter(
                self._adapters[0], budget, purpose="implementation", deadline=deadline
            )
            try:
                result = wrapped_worker.run_turn(
                    TurnRequest(
                        role="worker",
                        prompt=fix_prompt,
                        workdir=handle.workdir,
                        timeout_seconds=self._args.turn_timeout_seconds,
                        artifact_dir=handle.artifact_dir,
                    )
                )
            except Exception as exc:
                handle.close()
                round_evidence.update(worker_status="error", error_type=type(exc).__name__)
                evidence["rounds"].append(round_evidence)
                audit_stop = "audit_worker_error"
                break
            if not wrapped_worker.results or result.status != "completed":
                handle.close()
                round_evidence.update(worker_status=result.status if result is not None else "not_executed")
                evidence["rounds"].append(round_evidence)
                audit_stop = wrapped_worker.stopped or (
                    f"audit_worker_{result.status}" if result is not None else "audit_worker_not_executed"
                )
                break
            progress = _bench_progress(state)
            progress.update(
                worker_turns=int(progress.get("worker_turns", 0)) + len(wrapped_worker.results),
                ready=bool(re.search(r"(?im)^\s*PACT_REVIEW_READY\s*$", result.final_text)),
            )
            try:
                state = _run_before_deadline(
                    deadline,
                    runtime.save_candidate,
                    handle,
                    summary=result.final_text
                    or result.stderr_tail
                    or f"Early coverage audit repair {round_number} completed.",
                    advisory={**state.advisory, "benchmark": progress},
                )
            except _AuditDeadlineExceeded:
                round_evidence.update(worker_status="deadline")
                evidence["rounds"].append(round_evidence)
                audit_stop = "audit_io_deadline"
                break
            except Exception as exc:
                handle.close()
                round_evidence.update(worker_status="error", error_type=type(exc).__name__)
                evidence["rounds"].append(round_evidence)
                audit_stop = "audit_worker_save_error"
                break
            round_evidence["worker_status"] = "completed"
            evidence["current_candidate"] = self._early_audit_candidate_identity(state.candidate)
            with contextlib.suppress(Exception):
                state = self._flush_early_audit(runtime, output, state, evidence)
            state = self._early_audit_public_check(
                runtime, state, f"early-audit-{round_number}", deadline
            )
            public_status = (
                state.verification.status
                if state.verification is not None and state.verification.candidate == state.candidate
                else "missing"
            )
            round_evidence["public_check"] = public_status
            if not _public_passed(state):
                evidence["rounds"].append(round_evidence)
                audit_stop = "audit_public_check_failed"
                state = self._flush_early_audit(runtime, output, state, evidence)
                break
            if budget.stop_reason(deadline) or time.time() >= deadline:
                evidence["rounds"].append(round_evidence)
                audit_stop = str(budget.stop_reason(deadline) or "audit_deadline")
                state = self._flush_early_audit(runtime, output, state, evidence)
                break
            state, _role_stop = driver._review(
                state,
                budget,
                audit_window,
                purpose="acceptance",
                full=True,
                force=True,
            )
            accepted = state.acceptance is not None and _public_passed(state)
            round_evidence["acceptance"] = "accepted" if accepted else "rejected"
            if not accepted:
                evidence["rounds"].append(round_evidence)
                state = self._flush_early_audit(runtime, output, state, evidence)
                continue
            replacement = {
                "candidate_tree": state.candidate.tree,
                "candidate_checkpoint": state.candidate.checkpoint,
                "acceptance": self._json_value(state.acceptance),
            }
            self._early_audit_replaced = True
            evidence.update(
                status="replacement_accepted",
                replaced_fallback=True,
                replacement=replacement,
                current_candidate=self._early_audit_candidate_identity(state.candidate),
                stop_reason="replacement_accepted",
                audit_stop_reason="replacement_accepted",
                finished_at_unix=round(budget.clock(), 3),
            )
            evidence["rounds"].append(round_evidence)
            # Persist the decision before promotion; a failed delivery keeps the
            # accepted fallback selectable instead of claiming a new delivery.
            state = self._flush_early_audit(runtime, output, state, evidence)
            try:
                state = runtime.deliver(
                    state.work_id, expected_revision=state.revision, acceptance=state.acceptance
                )
            except Exception as exc:
                evidence.update(
                    status="fallback_retained",
                    replaced_fallback=False,
                    stop_reason="accepted_fallback_retained",
                    audit_stop_reason=f"replacement_delivery_{type(exc).__name__}",
                )
                self._early_audit_replaced = False
                state = self._flush_early_audit(runtime, output, state, evidence)
                self._early_audit_finished = True
                self._early_audit_closed = True
                self._invocation.stop_reason = "accepted_fallback_retained"
                return state, True
            self._early_audit_finished = True
            self._invocation.stop_reason = "accepted"
            return state, True

        if clean:
            # A clean report only permits delivery of the exact candidate it
            # audited. If a repair candidate is still current, it must pass a
            # fresh full public check and acceptance first; otherwise the frozen
            # fallback remains the delivery. Never re-label an unaccepted repair
            # candidate as the fallback.
            current_is_fallback = self._early_audit_candidate_is_fallback(state)
            evidence["current_candidate"] = self._early_audit_candidate_identity(state.candidate)
            accepted = False
            if not budget.stop_reason(deadline) and not self._abort.is_set() and time.time() < deadline:
                if not current_is_fallback:
                    state = self._early_audit_public_check(
                        runtime, state, "early-audit-clean", deadline
                    )
                if _public_passed(state):
                    reviewed_checkpoint = state.candidate.checkpoint
                    state, _role_stop = driver._review(
                        state,
                        budget,
                        audit_window,
                        purpose="acceptance",
                        full=True,
                        force=True,
                    )
                    accepted = (
                        state.acceptance is not None
                        and _public_passed(state)
                        and state.candidate.checkpoint == reviewed_checkpoint
                    )
            if accepted:
                replacement = not current_is_fallback
                self._early_audit_replaced = replacement
                evidence.update(
                    status="replacement_accepted" if replacement else "accepted_fallback",
                    replaced_fallback=replacement,
                    current_candidate=self._early_audit_candidate_identity(state.candidate),
                    stop_reason="replacement_accepted" if replacement else "accepted",
                    audit_stop_reason="audit_clean",
                    finished_at_unix=round(budget.clock(), 3),
                )
                if replacement:
                    evidence["replacement"] = {
                        "candidate_tree": state.candidate.tree,
                        "candidate_checkpoint": state.candidate.checkpoint,
                        "acceptance": self._json_value(state.acceptance),
                    }
                state = self._flush_early_audit(runtime, output, state, evidence)
                try:
                    state = runtime.deliver(
                        state.work_id, expected_revision=state.revision, acceptance=state.acceptance
                    )
                except Exception as exc:
                    evidence.update(
                        status="fallback_retained",
                        replaced_fallback=False,
                        stop_reason="accepted_fallback_retained",
                        audit_stop_reason=f"clean_delivery_{type(exc).__name__}",
                    )
                    self._early_audit_replaced = False
                    state = self._flush_early_audit(runtime, output, state, evidence)
                    self._early_audit_finished = True
                    self._early_audit_closed = True
                    self._invocation.stop_reason = "accepted_fallback_retained"
                    return state, True
                self._early_audit_finished = True
                self._invocation.stop_reason = "accepted" if not replacement else "replacement_accepted"
                return state, True
        evidence.update(
            status="fallback_retained",
            replaced_fallback=False,
            current_candidate=self._early_audit_candidate_identity(state.candidate),
            stop_reason="accepted_fallback_retained",
            audit_stop_reason=audit_stop,
            finished_at_unix=round(budget.clock(), 3),
        )
        self._early_audit_replaced = False
        state = self._flush_early_audit(runtime, output, state, evidence)
        self._early_audit_finished = True
        self._early_audit_closed = True
        self._invocation.stop_reason = "accepted_fallback_retained"
        return state, True

    def _resolve_execution(self, workspace: Path) -> ExecutionBinding:
        return self._plugin.resolve_execution(workspace, self._args)

    def _answer_external_questions(self, runtime: WorkRuntime, state: WorkSnapshot) -> WorkSnapshot:
        """Record only substantive answers supplied by the caller's channel."""
        channel = self._args.external_question_channel
        if channel is None or not state.unresolved_assumptions or self._abort.is_set():
            return state
        from .ports import ExternalQuestion

        async def exchange(question: ExternalQuestion, timeout: float):
            await asyncio.wait_for(channel.post(question, cancelled=self._external_question_cancelled), timeout)
            return await asyncio.wait_for(
                channel.await_answer(question.identity, timeout=timeout, cancelled=self._external_question_cancelled),
                timeout,
            )

        for row in tuple(state.unresolved_assumptions):
            remaining = (self._soft_deadline or time.time() + 30) - time.time()
            if remaining <= 0 or self._abort.is_set():
                break
            question_id = str(row.get("id", ""))
            if not question_id:
                continue
            question = ExternalQuestion(f"{state.work_id}:{question_id}", _question_text(row))
            try:
                answer = self._run_async(exchange(question, min(30.0, remaining)))
            except TimeoutError:
                continue
            if answer.status == "answer" and isinstance(answer.text, str) and answer.text.strip():
                state = runtime.record_user_decision(
                    state.work_id,
                    expected_revision=state.revision,
                    question_id=question_id,
                    answer=answer.text,
                )
                self._invocation.state = state
        return state

    def _frozen_input(self, task_bytes: bytes, output: Path) -> dict[str, object]:
        args = self._args
        evidence_configuration = {}
        if args.workflow == "suite":
            from .localization import localization_configuration

            evidence_configuration = {
                **self._plugin.evidence_configuration(),
                "localization": localization_configuration(cutoff_at_unix=args.cutoff_at_unix),
            }
        return {
            "schema": self._plugin.spec.input_schema,
            "request_id": args.request_id,
            "configuration": args.config().payload(),
            **(
                {"budget_started_at_unix": args.budget_started_at_unix}
                if args.budget_started_at_unix is not None
                else {}
            ),
            "max_turns": args.max_turns,
            **(
                {"finish_reserve_seconds": args.finish_reserve_seconds}
                if args.finish_reserve_override_seconds is not None
                else {}
            ),
            "transport_recovery_attempts": args.transport_recovery_attempts,
            "unattended_question_policy": {
                "schema": "long-horizon/unattended-question-policy/v1",
                "allow_user_interaction": args.allow_user_interaction,
                "clarification_max_rounds": args.clarification_max_rounds,
                "clarification_budget_ratio": args.clarification_budget_ratio,
            },
            **(
                {
                    "turn_budget_policy": "original_global_deadline_v1",
                    "deadline_only_budget_seconds": args.deadline_only_budget_seconds,
                }
                if args.max_turns is None
                else {}
            ),
            "worker_evidence_guidance": WORKER_EVIDENCE_GUIDANCE_VERSION,
            "anytime_deliverable_guidance": ANYTIME_DELIVERABLE_GUIDANCE_VERSION,
            "ui_state_evidence_guidance": UI_STATE_EVIDENCE_GUIDANCE_VERSION,
            "reference_client_spec_guidance": REFERENCE_SPEC_GUIDANCE_VERSION,
            "generalization_check_guidance": GENERALIZATION_CHECK_GUIDANCE_VERSION,
            "reviewer_execution_guidance": REVIEWER_EXECUTION_GUIDANCE_VERSION,
            **(
                {
                    "candidate_artifacts": {
                        "policy": "sealed-candidate-after-save-v1",
                        "archive_root": str(self._archive_root),
                        "original_deadline_unix": self._check_deadline,
                    }
                }
                if self._archive_root is not None
                else {}
            ),
            "snapshot_resources": {
                "policy": "bounded-snapshot-bytes-v1",
                "max_total_bytes": args.snapshot_max_total_bytes,
            },
            **(
                {"control_profile": "bounded-phases-public-candidate-gate-v3-check-outcome-routing"}
                if args.control_profile == "bounded"
                else {}
            ),
            "original_task_sha256": sha256_bytes(task_bytes),
            "original_task_bytes": len(task_bytes),
            "output_root": str(output),
            "profile_name": args.profile_name,
            "model": args.model,
            "verify_command": args.verify_command,
            **(
                {
                    "workflow": "suite",
                    "evidence_configuration": evidence_configuration,
                    "capacity_profile": capacity_receipt(),
                }
                if args.workflow == "suite"
                else {}
            ),
        }

    def _open_work(
        self, runtime: WorkRuntime, frozen: dict[str, object], binding: dict[str, object], task_text: str
    ) -> WorkSnapshot:
        args = self._args
        work_id = args.derived_work_id
        latch = runtime.workspace / ".pact-io" / self._plugin.spec.suspension_directory / f"{work_id}.json"
        if args.work_id is not None:
            if args.work_id != work_id:
                raise RuntimeContractError("--work-id does not belong to this task_id/trial_id")
            state = runtime.read_work(work_id)
            self._invocation.state = state
            if state.revision != args.expected_revision:
                raise RuntimeContractError("stale expected work revision")
            self._require_same(state, frozen, binding, self._args.profile_name)
            return state
        contract, plan = read_text_plan(args.task.expanduser().resolve(strict=True), workspace=runtime.workspace)
        if contract.get("goal") != task_text:
            raise RuntimeContractError("the Goal must be the original task verbatim")
        advisory: dict[str, object] = {
            "profile": "benchmark",
            self._plugin.spec.configuration_key: self._plugin.configuration(args.profile_name),
            self._plugin.spec.execution_key: binding,
            self._plugin.spec.input_key: frozen,
            self._plugin.spec.progress_key: {
                "schema": self._plugin.spec.progress_schema,
                "phase": "created",
                "stages": {},
            },
            "benchmark": {"schema": _BENCH_PROGRESS_SCHEMA},
        }
        state = runtime.create_work(
            request_id=args.request_id,
            work_id=work_id,
            contract=contract,
            policy=AcceptancePolicy(
                verify_command=args.verify_command,
                allow_unverified=False,
                **(
                    {
                        "verify_timeout_seconds": float(args.budget_seconds),
                        "final_verify_timeout_seconds": float(args.budget_seconds),
                    }
                    if args.control_profile == "bounded"
                    else {}
                ),
            ),
            constraints=plan.constraints,
            advisory=advisory,
        )
        self._invocation.state = state
        self._require_same(state, frozen, binding, args.profile_name)
        if state.delivery is not None:
            return state
        if latch.exists():
            raise RuntimeStateError(f"work budget suspension requires explicit --work-id resume: {work_id}")
        if self._phase(state) != "created":
            raise RuntimeStateError(
                f"work {work_id} already started (phase {self._phase(state)}, revision {state.revision}); "
                "resume explicitly with --work-id and --expected-revision. No automatic restart."
            )
        return state

    def _make_adapter(self, role: Literal["worker", "reviewer"], resolved: ExecutionBinding) -> RoleAdapter:
        assert self._loop is not None
        adapter = self._plugin.make_adapter(
            role, resolved, self._args, loop=self._loop, invocation_id=self._invocation.invocation_id, abort=self._abort
        )
        probe = adapter.probe()
        if not probe.available:
            raise RuntimeContractError(f"{role} adapter is unavailable: {probe.adapter_id}")
        return RoleBoundary(adapter)

    # -- bootstrap: S0 → P0 → clarification → first review -------------

    def _bootstrap(
        self,
        runtime: WorkRuntime,
        output: Path,
        budget: BenchBudget,
        window: BudgetWindow,
        task_text: str,
        resolved: ExecutionBinding,
        state: WorkSnapshot,
    ) -> tuple[WorkSnapshot, int]:
        turn_root = self._turn_root(output, state.work_id)
        store = self._artifacts.create_store(turn_root.parent.parent)
        turns = 0
        if self._phase(state) == "created":
            state = self._freeze_s0(runtime, output, turn_root, store, task_text, state)
            state = self._check_candidate(runtime, state, "s0")
            if self._stop_for_public_check(state):
                return state, turns
        if self._phase(state) == "p0_started":
            state = self._reconcile_p0(runtime, budget, state)
        phase = self._phase(state)
        recover = (
            self._args.resume_failed_external_draft
            and self._args.work_id == state.work_id
            and has_saved_external_worker_failure(self._progress(state))
        )
        if phase == "s0_frozen" or recover:
            if self._abort.is_set():
                return state, turns
            prior_stop_reason = self._invocation.stop_reason
            state, ran = self._run_p0(runtime, output, turn_root, budget, window, state, recover=recover)
            turns += int(ran)
            if self._invocation.failure is not None or self._abort.is_set():
                return state, turns
            if not ran:
                if not recover:
                    return state, turns
                # Leave one opportunity to recover if implementation was refused.
                # After a second refusal, review the same saved draft normally.
                refusals = 2 if self._progress(state).get("draft_recovery_refusals") == 1 else 1
                state = self._advance(runtime, state, "p0_saved", draft_recovery_refusals=refusals)
                if refusals == 1:
                    return state, turns
                self._invocation.stop_reason = prior_stop_reason
            else:
                # Leave a structured external failure for an explicit continuation
                # before spending review budget.
                saved = self._progress(state).get("p0")
                if isinstance(saved, dict):
                    if recover and saved.get("status") != "completed":
                        return state, turns
                    if saved.get("status") == "output_missing" and saved.get("native_status") == "failed":
                        return state, turns
        if self._phase(state) in ("p0_saved", "clarification_started") and "p0" not in self._stages(state):
            # Core recovered a saved P0 whose stage evidence was not yet sealed.
            state = self._seal_p0(runtime, output, turn_root, state)
        if self._args.external_question_channel is not None and self._phase(state) in (
            "p0_saved",
            "clarification_started",
        ):
            state = self._answer_external_questions(runtime, state)
        if self._phase(state) in ("p0_saved", "clarification_started"):
            state = self._check_candidate(runtime, state, "p0")
            if self._stop_for_public_check(state):
                return state, turns
        if self._phase(state) in ("p0_saved", "clarification_started"):
            if self._abort.is_set():
                return state, turns
            state = self._clarify(runtime, output, turn_root, store, budget, window, task_text, resolved, state)
            if self._phase(state) not in ("brief_binding", "brief_bound"):
                return state, turns
        if self._phase(state) == "brief_binding":
            try:
                state = self._bind_brief(runtime, output, turn_root, state, task_text)
            except self._artifacts.exhausted as exc:
                if public_review_failure(exc) is None:
                    raise
                return self._save_public_review_p0(runtime, state, exc), turns
        if self._args.workflow == "suite" and self._phase(state) == "brief_bound":
            from .suite import incomplete_public_review

            progress = incomplete_public_review(state.advisory.get("long_horizon_suite"))
            if progress is not None:
                # Data written by an earlier version may already be bound with
                # an incomplete full review. Never start P1 from it; retain the
                # per-criterion checkpoints and make the next explicit resume
                # prepare/review again from the same frozen S0.
                attempts = cast(list[object], self._progress(state).get("clarification_attempts", []))
                self._invocation.stop_reason = progress.reason or "incomplete"
                state = runtime.update_advisory(
                    state.work_id,
                    expected_revision=state.revision,
                    advisory={k: v for k, v in state.advisory.items() if k != "long_horizon_suite"},
                )
                return (
                    self._advance(
                        runtime,
                        state,
                        "p0_saved",
                        clarification_attempts=[*attempts, self._public_review_progress_attempt(progress)],
                    ),
                    turns,
                )
        if self._args.workflow == "suite" and self._phase(state) in ("brief_bound", "p1_started"):
            if self._args.max_turns is not None and self._args.max_turns < 2:
                self._invocation.stop_reason = "max_turns"
                return state, turns
            from .suite import run_repair

            state, ran = run_repair(self, runtime, output, turn_root, budget, window, state)
            turns += int(ran)
            if self._phase(state) != "p1_saved" or self._abort.is_set():
                return state, turns
            if self._stop_for_public_check(state):
                return state, turns
            if self._gate is not None and not self._progress(state).get("p1_worker_completed", False):
                return state, turns
        if self._phase(state) in ("brief_bound", "p1_saved", "first_review_started") and not self._abort.is_set():
            state = self._first_review(runtime, budget, window, state)
        return state, turns

    def _turn_root(self, output: Path, work_id: str) -> Path:
        return output / "evidence" / work_id / "requirement_clarification" / "turn_1"

    def _full_acceptance_times(self) -> list[float]:
        history = getattr(self, "_acceptance_timings", None)
        return history.recent("accept") if history is not None else []

    def _check_times(self) -> list[float]:
        history = getattr(self, "_acceptance_timings", None)
        if history is not None:
            return history.worker_check_times()
        return self._public_check_durations

    def _initialize_phase_policy(self, output: Path, budget: BenchBudget, window: BudgetWindow) -> None:
        """Create the same soft/hard/reserve policy for every run entry.

        New runs, bootstrap resumes and a resumed ``first_review_done`` all
        reach this method before any Worker is constructed. The policy is not
        allowed to inherit Core's hard clock as the Worker cutoff.
        """
        if self._args.control_profile != "bounded":
            return
        if self._soft_deadline is not None and self._original_deadline is not None:
            hard_deadline = float(self._original_deadline)
            soft_deadline = min(float(window.deadline), float(self._soft_deadline))
        else:
            hard_deadline = float(window.deadline)
            soft_deadline = float(window.execution_deadline)
        snapshot = budget.snapshot()
        started = snapshot.get("started_at", window.started_at)
        if isinstance(started, bool) or not isinstance(started, (int, float)) or not math.isfinite(float(started)):
            started = window.started_at
        policy_path = output / "phase-policy.json"
        finish_observations, stored_acceptance = _restored_phase_history(policy_path)
        recent_acceptance = recent_acceptance_durations(snapshot) or stored_acceptance

        def acceptance_history() -> list[float]:
            latest = recent_acceptance_durations(budget.snapshot())
            return latest or list(stored_acceptance)

        self._phase_policy = PhasePolicy(
            float(started),
            hard_deadline,
            soft_deadline,
            max(0.0, float(self._args.budget_seconds)),
            policy_path,
            clock=budget.clock,
            side_calls_use_execution_deadline=self._args.max_turns is None,
            recent_acceptance=list(recent_acceptance),
            finish_observations=list(finish_observations),
            acceptance_history_provider=acceptance_history,
            full_acceptance_provider=self._full_acceptance_times,
        )
        if finish_observations and not getattr(self, "_finish_observations", None):
            self._finish_observations = list(finish_observations)

    def _driver_window_seconds(self, window: BudgetWindow | None, budget: BenchBudget) -> float | None:
        """Return the admitted absolute deadline, never a freshly measured duration."""
        if window is None:
            return self._args.window_seconds
        return window.deadline

    def _restored_budget_window(self, budget: BenchBudget) -> BudgetWindow:
        """Rebuild an absolute window for a resumed invocation without reopening it."""
        snapshot = budget.snapshot()
        now = budget.clock()
        deadline = float(snapshot["deadline"])
        if self._soft_deadline is not None:
            deadline = min(deadline, float(self._soft_deadline))
        return BudgetWindow(now, deadline, deadline, 0.0)

    def _soft_window(self, budget: BenchBudget, requested: float | None) -> BudgetWindow | None:
        """Admit model work only before the soft deadline; reserve stays for finish."""
        if self._soft_deadline is None:
            remaining = requested
        else:
            remaining = self._soft_deadline - budget.clock()
            if remaining <= 0:
                return None
            if requested is not None:
                remaining = min(remaining, requested)
        window = budget.window(remaining)
        deadline = min(window.deadline, self._soft_deadline) if self._soft_deadline is not None else window.deadline
        if deadline <= budget.clock():
            return None
        # The whole admitted window is available to model work; the finish
        # reserve lives outside it rather than in a second hidden reserve.
        return BudgetWindow(window.started_at, deadline, deadline, 0.0)

    def _freeze_s0(
        self,
        runtime: WorkRuntime,
        output: Path,
        turn_root: Path,
        store: ArtifactStore,
        task_text: str,
        state: WorkSnapshot,
    ) -> WorkSnapshot:
        verify_recovered_views()
        store.save_requirement_input(revision=1, messages=(task_text,))
        receipt = runtime.export_candidate(state.candidate, output / "stages" / f"s0-{uuid.uuid4().hex}")
        record, _snapshot = self._artifacts.stage_record(
            output_root=output,
            turn_root=turn_root,
            stage="s0",
            receipt=receipt,
            metadata={"work_id": state.work_id, "original_task_sha256": self._frozen(state)["original_task_sha256"]},
        )
        record["baseline_commit"] = read_json(receipt.artifact).get("baseline_commit")
        record["source_head_commit"] = receipt.source_basis.get("head_commit")
        progress = {**_bench_progress(state), "initial_tree": state.candidate.tree}
        return self._advance(runtime, state, "s0_frozen", stages={"s0": record}, benchmark=progress)

    def _reconcile_p0(self, runtime: WorkRuntime, budget: BenchBudget, state: WorkSnapshot) -> WorkSnapshot:
        """After Core recovery, only a never-attempted P0 may run; never replay one."""
        if "prior_failed_draft" in self._progress(state):
            # An in-flight recovery has no saved result. Even if its budget
            # admission is absent, the external side effects are unconfirmed.
            raise RuntimeStateError("not_auto_resumable: saved draft recovery was interrupted before save")
        calls = cast(list[dict[str, object]], budget.snapshot()["calls"])
        if any(call.get("purpose") == "implementation" for call in calls):
            raise RuntimeStateError(
                "not_auto_resumable: P0 was admitted but its candidate was not saved; "
                "inspect the Core operation and budget ledger. P0 is never replayed."
            )
        s0 = self._stages(state).get("s0")
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
        *,
        recover: bool = False,
    ) -> tuple[WorkSnapshot, bool]:
        deadline = budget.purpose_deadline(window, "implementation")
        if self._phase_policy is not None:
            deadline = min(deadline, self._phase_policy.deadline_for("p0"))
        stop = budget.stop_reason(deadline, purpose="implementation")
        if stop is not None or self._model_window_closed():
            self._invocation.stop_reason = stop or "window_budget_exhausted"
            return state, False
        prompt = (
            self._args.external_prompt_verbatim
            if self._args.external_prompt_verbatim is not None
            else _p0_prompt(state)
        )
        prior = {"prior_failed_draft": self._stages(state)["p0"]} if recover else {}
        # Persist before admission. A crash after admission still takes the
        # existing fail-closed p0_started reconciliation path.
        state = self._advance(runtime, state, "p0_started", **prior)
        release_recovered_views((runtime.workspace,))
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
            if self._adapters[0].budget_failure == "TurnBudgetExhausted":
                self._raise_native_turn_refusal(window, budget)
            executed = wrapped.stopped is None
            progress = _bench_progress(state)
            if executed:
                progress.update(
                    worker_turns=(int(progress.get("worker_turns", 0)) if recover else 0) + len(wrapped.results),
                    ready=result.status == "completed"
                    and re.search(r"(?im)^\s*PACT_REVIEW_READY\s*$", result.final_text) is not None,
                    milestone_ready=result.status == "completed"
                    and re.search(r"(?im)^\s*PACT_MILESTONE_READY\s*$", result.final_text) is not None,
                    last_worker_status=result.status,
                )
            p0 = {"status": result.status, "executed": executed, "stopped": wrapped.stopped}
            if self._args.external_live_workdir is not None:
                p0["native_status"] = getattr(self._adapters[0], "last_native_status", None)
            lh = self._progress(state)
            summary = result.final_text or result.stderr_tail or f"Worker {result.status}; partial files retained."
            # A refused P0 made no model call; its saved tree equals S0 and a
            # later explicit resume may still run the first implementation.
            if recover and executed:
                # Recovery saves a new candidate. A crash before _seal_p0
                # must not leave the active stage pointing at its predecessor.
                lh["stages"] = {k: v for k, v in self._stages(state).items() if k != "p0"}
            if recover and not executed:
                from jiuwenswarm.pact_core.kernel import read_work_candidate

                lh.pop("prior_failed_draft", None)
                lh["phase"] = "p0_saved"
                summary = read_work_candidate(state.candidate.artifact).summary.read_text(encoding="utf-8")
            else:
                lh.update(phase="p0_saved" if executed else "s0_frozen", p0=p0)
            state = runtime.save_candidate(
                handle,
                summary=summary,
                advisory={**state.advisory, "benchmark": progress, self._plugin.spec.progress_key: lh},
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
        state = self._check_candidate(runtime, state, "p0")
        if wrapped.stopped is not None or result.status != "completed":
            self._invocation.stop_reason = wrapped.stopped or f"worker_{result.status}"
        return state, True

    def _seal_p0(self, runtime: WorkRuntime, output: Path, turn_root: Path, state: WorkSnapshot) -> WorkSnapshot:
        started = self._budget.clock() if self._budget is not None else time.time()
        verify_recovered_views()
        receipt = runtime.export_candidate(state.candidate, output / "stages" / f"p0-{uuid.uuid4().hex}")
        p0 = self._progress(state).get("p0")
        record, _snapshot = self._artifacts.stage_record(
            output_root=output,
            turn_root=turn_root,
            stage="p0",
            receipt=receipt,
            metadata={"work_id": state.work_id, "worker": p0 if isinstance(p0, dict) else None},
        )
        finished = self._budget.clock() if self._budget is not None else time.time()
        self._pending_seal_seconds = max(0.0, finished - started)
        return self._advance(runtime, state, "p0_saved", stages={"p0": record})

    def _read_host_check(self, state: WorkSnapshot, stage: str) -> dict[str, object] | None:
        """Read the existing fixed RPC evidence; ordinary Core checks stay local."""
        if not self._rpc_public_check:
            return None
        value = None
        verification = state.verification
        if (
            self._check_deadline is not None
            and self._check_receipts is not None
            and self._gate is not None
            and verification is not None
            and verification.candidate == state.candidate
        ):
            value = decode_public_check_outcome(
                verification.check_results,
                receipts=self._check_receipts,
                expected_candidate_checkpoint=state.candidate.checkpoint,
                verification=verification,
            )
        return {
            "schema": "long-horizon/host-public-check-routing/v1",
            "stage": stage,
            "candidate_checkpoint": state.candidate.checkpoint,
            "verification_artifact": str(verification.artifact) if verification is not None else None,
            "host_outcome": asdict(value) if value is not None else None,
            "stop_unresolved": value is None or value.stop_unresolved,
            "reason": (
                "rpc_requires_public_candidate_gate"
                if self._gate is None
                else value.error_kind
                if value is not None
                else "receipt_unavailable_or_unbound"
            ),
            "independent_reward": None,
        }

    def _save_host_check(
        self, runtime: WorkRuntime, state: WorkSnapshot, route: dict[str, object] | None
    ) -> WorkSnapshot:
        if route is None:
            return state
        if state.status == "completed":
            # Core refuses every mutation once Work is delivered. The route is
            # still evaluated (eligibility, unresolved stop) but kept locally.
            self._post_delivery_host_checks[str(route["stage"])] = route
            return state
        records = dict(cast(dict[str, object], self._progress(state).get("host_public_checks", {})))
        if records.get(str(route["stage"])) == route:
            return state
        records[str(route["stage"])] = route
        return self._advance(
            runtime, state, self._phase(state), host_public_checks=records, last_host_public_check=route
        )

    def _halt_public_check(self, checkpoint: str) -> None:
        """Latch the first unresolved event, even if a driver later seals again."""
        if self._public_check_halted:
            return
        self._public_check_halted = True
        selected = self._gate.selected if self._gate is not None else None
        if selected is not None and selected[0].checkpoint != checkpoint:
            self._public_check_fallback_checkpoint = selected[0].checkpoint

    def _stop_for_public_check(self, state: WorkSnapshot) -> bool:
        if not self._rpc_public_check:
            return False
        route = self._progress(state).get("last_host_public_check")
        if state.status == "completed" and self._post_delivery_host_checks:
            route = next(reversed(self._post_delivery_host_checks.values()))
        if (
            isinstance(route, dict)
            and route.get("candidate_checkpoint") == state.candidate.checkpoint
            and route.get("stop_unresolved") is True
        ):
            self._halt_public_check(state.candidate.checkpoint)
        if not self._public_check_halted:
            return False
        budget_stop = (
            self._budget.stop_reason(self._check_deadline)
            if self._budget is not None and self._check_deadline is not None
            else None
        )
        self._invocation.stop_reason = (
            "invocation_stopped" if self._abort.is_set() else budget_stop or "public_check_unresolved"
        )
        return True

    def _check_candidate(
        self,
        runtime: WorkRuntime,
        state: WorkSnapshot,
        stage: str,
        *,
        deadline: float | None = None,
    ) -> WorkSnapshot:
        """Use Core's verifier and fixed host proof before recording eligibility."""
        if not hasattr(self, "_public_check_evidence"):
            self._public_check_evidence = []
        if not hasattr(self, "_rejected_check_checkpoints"):
            self._rejected_check_checkpoints = set()
        candidate_checkpoint = getattr(state.candidate, "checkpoint", None)
        if isinstance(candidate_checkpoint, str) and candidate_checkpoint in self._rejected_check_checkpoints:
            self._invocation.stop_reason = "window_budget_exhausted"
            return state
        if self._rpc_public_check and self._abort.is_set():
            self._halt_public_check(state.candidate.checkpoint)
            self._invocation.stop_reason = "invocation_stopped"
            return state
        if self._rpc_public_check and self._public_check_halted:
            self._stop_for_public_check(state)
            return state
        if self._rpc_public_check and (
            self._check_deadline is None or self._check_receipts is None or self._gate is None
        ):
            state = self._save_host_check(runtime, state, self._read_host_check(state, stage))
            self._stop_for_public_check(state)
            return state
        if self._gate is None:
            return state
        existing = state.verification is not None and state.verification.candidate == state.candidate
        check_stop_reason: str | None = None
        if self._rpc_public_check and existing:
            state = self._save_host_check(runtime, state, self._read_host_check(state, stage))
            if self._stop_for_public_check(state):
                return state
        if self._gate.observed(state.candidate):
            return state
        if not existing:
            if self._phase_policy is None or self._budget is None:
                return state
            check_deadline = self._phase_policy.deadline_for("check")
            if deadline is not None:
                check_deadline = min(check_deadline, deadline)
            # Delivered Work cannot record a new verification; Core acceptance
            # already verified the delivered candidate.
            if state.status != "completed" and self._budget.clock() < check_deadline and not self._abort.is_set():
                started = self._budget.clock()
                record: dict[str, object] = {
                    "schema": "long-horizon/public-check-lifecycle/v1",
                    "stage": stage,
                    "candidate_checkpoint": getattr(state.candidate, "checkpoint", None),
                    "candidate_tree": getattr(state.candidate, "tree", None),
                    "deadline_unix": check_deadline,
                    "started_at": started,
                    "finished_at": None,
                    "duration_seconds": None,
                    "crossed_soft_deadline": False,
                    "stop_reason": "started",
                }
                self._public_check_evidence.append(record)
                self._last_public_check_started_at = started
                try:
                    state = runtime.verify_candidate(
                        state.work_id, expected_revision=state.revision, deadline=check_deadline
                    )
                except BaseException:
                    finished = self._budget.clock()
                    record.update(
                        finished_at=finished,
                        duration_seconds=max(0.0, finished - started),
                        stop_reason="cancelled" if self._abort.is_set() else "hard_deadline",
                    )
                    raise
                finished = self._budget.clock()
                soft = getattr(self, "_soft_deadline", None)
                hard = getattr(self, "_original_deadline", None) or getattr(self, "_check_deadline", None)
                crossed = soft is not None and started < soft <= finished
                if self._abort.is_set():
                    stop_reason = "cancelled"
                elif hard is not None and finished >= hard:
                    stop_reason = "hard_deadline"
                elif soft is not None and (started >= soft or crossed):
                    stop_reason = "soft_deadline_allowed"
                else:
                    stop_reason = "completed"
                verification = state.verification
                check_seconds = max(0.0, finished - started)
                check_stop_reason = stop_reason
                if stop_reason in {"cancelled", "hard_deadline"} and isinstance(candidate_checkpoint, str):
                    self._rejected_check_checkpoints.add(candidate_checkpoint)
                record.update(
                    finished_at=finished,
                    duration_seconds=check_seconds,
                    crossed_soft_deadline=crossed,
                    stop_reason=stop_reason,
                    verification_status=(
                        verification.status
                        if verification is not None and verification.candidate == state.candidate
                        else "missing"
                    ),
                )
                seal_seconds = max(0.0, float(getattr(self, "_pending_seal_seconds", 0.0) or 0.0))
                self._pending_seal_seconds = 0.0
                self._record_finish_observation(seal_seconds + check_seconds, check_seconds=check_seconds)
            state = self._save_host_check(runtime, state, self._read_host_check(state, stage))
            if self._stop_for_public_check(state):
                return state
        if check_stop_reason in {"cancelled", "hard_deadline"} or self._abort.is_set():
            if self._rpc_public_check:
                self._halt_public_check(state.candidate.checkpoint)
            self._invocation.stop_reason = (
                "invocation_stopped"
                if self._abort.is_set() or check_stop_reason == "cancelled"
                else "window_budget_exhausted"
            )
            return state
        self._gate.record(state, stage)
        if self._gate.selected is not None and state.delivery is None:
            state = runtime.update_advisory(
                state.work_id,
                expected_revision=state.revision,
                advisory={**state.advisory, self._gate.advisory_key: self._gate.saved_entries()},
            )
        self._invocation.state = state
        return state

    def _public_review_receipt(self, progress: PublicReviewProgress) -> dict[str, object]:
        receipt = progress.receipt()
        budget = self._budget.snapshot() if self._budget is not None else {}
        # Keep the admitted absolute deadline and the original task budget; a
        # recoverable stop must not shorten or rebase either one.
        deadline = budget.get("deadline", progress.deadline_unix)
        receipt["deadline_unix"] = deadline if deadline is not None else self._original_deadline
        receipt["total_seconds"] = float(self._args.budget_seconds)
        receipt["started_at"] = budget.get("started_at")
        return receipt

    def _public_review_attempt(self, exc: BaseException, *, admission: Any = None) -> dict[str, object]:
        progress = public_review_failure(exc)
        attempt: dict[str, object] = {
            "invocation_id": self._invocation.invocation_id,
            "status": "stopped" if isinstance(exc, self._artifacts.stopped) else "budget_exhausted",
            "detail": str(exc)[:1000],
        }
        if admission is not None:
            attempt["admitted_calls"] = admission.admitted
            attempt["refused"] = admission.refused
        if progress is not None:
            attempt["reason"] = progress.reason or "incomplete"
            attempt["recoverable"] = self._public_review_receipt(progress)
        return attempt

    def _public_review_progress_attempt(self, progress: PublicReviewProgress) -> dict[str, object]:
        return {
            "invocation_id": self._invocation.invocation_id,
            "status": "budget_exhausted",
            "detail": progress.message(),
            "reason": progress.reason or "incomplete",
            "recoverable": self._public_review_receipt(progress),
        }

    def _save_public_review_p0(
        self,
        runtime: WorkRuntime,
        state: WorkSnapshot,
        exc: BaseException,
        *,
        admission: Any = None,
        clear_suite: bool = False,
    ) -> WorkSnapshot:
        """Reuse the controller's p0_saved/resume path for incomplete review."""
        attempts = cast(list[object], self._progress(state).get("clarification_attempts", []))
        attempt = self._public_review_attempt(exc, admission=admission)
        progress = public_review_failure(exc)
        if isinstance(exc, self._artifacts.stopped):
            reason = "invocation_stopped"
        elif progress is not None:
            reason = progress.reason or "incomplete"
        elif admission is not None:
            reason = admission.refused or "clarification_budget_exhausted"
        else:
            reason = "clarification_budget_exhausted"
        self._invocation.public_review_basis_sha256 = None
        if (reason == "public_evidence_unavailable" and progress is not None and progress.started
                and progress.required_criteria and state.candidate is not None):
            # The receipt retains free-form diagnostics. Only structured
            # evidence and decisions can change the retry basis.
            basis = [
                state.work_id, state.candidate.tree, sorted(progress.required_criteria),
                sorted(progress.completed_criteria), list(state.user_decisions),
            ]
            try:
                encoded = json.dumps(basis, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
            except (TypeError, ValueError):
                # An unencodable basis leaves the normal continuation path available.
                pass
            else:
                self._invocation.public_review_basis_sha256 = hashlib.sha256(encoded).hexdigest()
        self._invocation.stop_reason = reason
        if clear_suite and "long_horizon_suite" in state.advisory:
            state = runtime.update_advisory(
                state.work_id,
                expected_revision=state.revision,
                advisory={k: v for k, v in state.advisory.items() if k != "long_horizon_suite"},
            )
        # Clarification never touched Core source; an explicit resume may
        # clarify/review again from the same frozen S0. P0 is not repeated.
        return self._advance(runtime, state, "p0_saved", clarification_attempts=[*attempts, attempt])

    def _clarify(
        self,
        runtime: WorkRuntime,
        output: Path,
        turn_root: Path,
        store: ArtifactStore,
        budget: BenchBudget,
        window: BudgetWindow,
        task_text: str,
        resolved: ExecutionBinding,
        state: WorkSnapshot,
    ) -> WorkSnapshot:
        if self._model_window_closed():
            self._invocation.stop_reason = "window_budget_exhausted"
            return state
        s0 = cast(dict[str, object], self._stages(state)["s0"])
        snapshot = self._artifacts.load_stage_snapshot(output, s0)
        attempts = cast(list[object], self._progress(state).get("clarification_attempts", []))
        state = self._advance(runtime, state, "clarification_started")
        clarification_deadline = budget.purpose_deadline(window, "clarification")
        if self._phase_policy is not None:
            clarification_deadline = min(clarification_deadline, self._phase_policy.deadline_for("planning"))
        allowance = self._clarification_budget_status(state, budget, window)
        if allowance["available"]:
            # The first clarification is subject to the same count and
            # whole-trial ratio as every later resolution round. It cannot
            # consume the entire trial before any implementation work.
            clarification_deadline = min(clarification_deadline, float(cast(float, allowance["deadline"])))
        self._planning_deadline = clarification_deadline
        admission = self._artifacts.make_admission(
            budget,
            deadline=clarification_deadline,
            workdir=Path(snapshot.roots[0].view_root),
            adapter_id=f"{self._plugin.spec.id}-clarifier-{resolved.fingerprint}",
            stop_requested=self._model_stop_requested,
        )
        context = ClarificationContext(
            profile=resolved.model_context,
            snapshot=snapshot,
            session_id=f"{self._invocation.invocation_id}-clarification",
            session_dir=turn_root.parent.parent / "clarification-session",
            admission=admission,
            deadline=clarification_deadline,
        )
        attempt: dict[str, object] = {"invocation_id": self._invocation.invocation_id}
        if self._model_window_closed():
            self._invocation.stop_reason = "window_budget_exhausted"
            attempt.update(status="stopped", detail="model admission closed before clarification")
            return self._advance(
                runtime, state, "p0_saved", clarification_attempts=[*attempts, attempt]
            )
        try:
            result = self._run_async(self._run_clarification(context, task_text))
        except self._artifacts.exhausted as exc:
            self._invocation.clarification_calls.extend(admission.admitted)
            return self._save_public_review_p0(runtime, state, exc, admission=admission)
        self._invocation.clarification_calls.extend(admission.admitted)
        if self._args.workflow == "suite":
            assert self._suite_preparation is not None
            write_json_atomic(turn_root / "suite-preparation.json", self._suite_preparation)
        store.save_result(result, requirement_messages=(task_text,))
        bundle = self._artifacts.read_clarification_bundle(turn_root, original_task=task_text)
        checkpoint = self._artifacts.evidence_checkpoint(
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
            stages={
                "clarification": {
                    **bundle.record(output),
                    **checkpoint,
                    **(
                        {"suite_preparation_sha256": sha256_bytes((turn_root / "suite-preparation.json").read_bytes())}
                        if self._args.workflow == "suite"
                        else {}
                    ),
                }
            },
            clarification_attempts=[*attempts, attempt],
        )

    async def _run_clarification(self, context: ClarificationContext, task_text: str) -> object:
        if self._args.workflow == "suite":
            from .suite import prepare_suite

            regression_context = getattr(self._args, "external_regression_context", "")
            if self._phase_policy is None:
                result, self._suite_preparation = await prepare_suite(
                    self._plugin, context, task_text, regression_context=regression_context,
                    cutoff_at_unix=self._args.cutoff_at_unix, clock=self._args.clock,
                )
                return result
            planning_deadline = self._planning_deadline
            analysis_seconds = max(0.001, (planning_deadline - self._budget.clock()) * 0.25)
            optional_admission = self._artifacts.make_admission(
                self._budget,
                deadline=min(planning_deadline, self._budget.clock() + analysis_seconds),
                workdir=Path(context.snapshot.roots[0].view_root),
                adapter_id="suite-bounded-optional",
                stop_requested=self._model_stop_requested,
            )
            optional_context = replace(context, admission=optional_admission)
            result, self._suite_preparation = await prepare_suite(
                self._plugin,
                context,
                task_text,
                regression_context=regression_context,
                optional_context=optional_context,
                optional_seconds=analysis_seconds,
                planning_deadline=planning_deadline,
                cutoff_at_unix=self._args.cutoff_at_unix,
                clock=self._args.clock,
            )
            context.admission.admitted[:0] = optional_admission.admitted
            return result
        return await self._plugin.clarify(context, task_text)

    # -- unattended unresolved-question resolution ---------------------

    def _clarification_budget_status(
        self, state: WorkSnapshot, budget: BenchBudget, window: BudgetWindow
    ) -> dict[str, object]:
        """Return the durable clarification allowance before another round."""
        progress = self._progress(state)
        attempts = progress.get("clarification_attempts", [])
        used_rounds = len(attempts) if isinstance(attempts, list) else 0
        snapshot = budget.snapshot()
        spent = 0.0
        for call in cast(list[dict[str, object]], snapshot["calls"]):
            if not str(call.get("purpose", "")).startswith("clarification:"):
                continue
            duration = call.get("observed_duration_seconds")
            if isinstance(duration, bool) or not isinstance(duration, (int, float)):
                started = call.get("started_at")
                finished = call.get("finished_at")
                duration = (
                    float(finished) - float(started)
                    if isinstance(started, (int, float)) and isinstance(finished, (int, float))
                    else 0.0
                )
            spent += max(0.0, float(duration))
        limit = float(self._args.budget_seconds) * float(self._args.clarification_budget_ratio)
        now = budget.clock()
        hard_stop = budget.stop_reason(window.deadline)
        remaining = max(0.0, min(window.deadline - now, limit - spent))
        if hard_stop is not None:
            reason = hard_stop
        elif used_rounds >= self._args.clarification_max_rounds:
            reason = "clarification_round_limit"
        elif spent >= limit:
            reason = "clarification_budget_ratio"
        elif remaining <= 1.0:
            reason = "clarification_time_remaining"
        else:
            reason = None
        return {
            "available": reason is None,
            "reason": reason,
            "rounds_used": used_rounds,
            "max_rounds": self._args.clarification_max_rounds,
            "spent_seconds": round(spent, 3),
            "limit_seconds": round(limit, 3),
            "remaining_seconds": round(remaining, 3),
            "ratio": float(self._args.clarification_budget_ratio),
            "deadline": min(window.deadline, now + remaining) if reason is None else now,
        }

    def _resolution_prompt(self, state: WorkSnapshot, task_text: str) -> str:
        questions = [
            {
                "id": str(question.get("id")),
                "question": _question_text(question),
                "requirement_ids": question.get("requirement_ids", []),
                "material": question.get("material"),
                "basis": question.get("basis"),
            }
            for question in state.unresolved_assumptions
        ]
        verification = state.verification
        verification_evidence = None
        if verification is not None:
            if capacity_scale() == DEFAULT_CAPACITY_SCALE:
                verification_evidence = {
                    "status": verification.status,
                    "command": verification.command,
                    "exit_code": verification.exit_code,
                    "stdout_tail": verification.stdout_tail[-4000:],
                    "stderr_tail": verification.stderr_tail[-4000:],
                }
            else:
                outputs = read_check_outputs(
                    getattr(verification, "artifact", None),
                    maximum=None,
                )
                verification_evidence = {
                    "status": verification.status,
                    "command": verification.command,
                    "exit_code": verification.exit_code,
                    "stdout_tail": verification.stdout_tail[-scaled(4000):],
                    "stderr_tail": verification.stderr_tail[-scaled(4000):],
                    "complete_check_output": outputs.prompt_block(),
                }
        evidence = {
            "candidate_tree": state.candidate.tree,
            "verification": verification_evidence,
            "latest_review": (
                {"feedback": state.review.feedback[:model_content_limit(8000)], "reasons": list(state.review.reasons)}
                if state.review is not None
                else None
            ),
            "saved_handoff": state.handoff,
            "acceptance_brief": state.acceptance_brief.get("text", "")[:model_content_limit(12000)],
        }
        return (
            "[UNATTENDED_QUESTION_RESOLUTION]\n"
            "This is one bounded, read-only clarification round for an unattended benchmark. No user can answer. "
            "Use the original task, the frozen baseline repository, the current handoff and recorded evidence. "
            "For each question choose exactly one kind:\n"
            "- evidence_resolved: the original task or repository evidence settles it; state the inference.\n"
            "- implementation_gap: the candidate is missing required behavior; give a concrete Worker fix item.\n"
            "- user_decision: only a user preference could settle it; choose the most conservative interpretation "
            "that preserves existing behavior and the literal original task, and state it as an assumption.\n"
            "Never waive a supplied check, invent a new requirement, or claim acceptance. Return ONLY JSON:\n"
            '{"resolutions":[{"id":"<question id>","kind":"evidence_resolved|implementation_gap|user_decision",'
            '"resolution":"<conclusion or fix>","assumption":"<assumption for evidence_resolved/user_decision>",'
            '"basis":"<task/repository/evidence basis>"}]}\n\n'
            f"Original task:\n{task_text}\n\n"
            f"Questions:\n{json.dumps(questions, ensure_ascii=False, indent=2)}\n\n"
            f"Recorded evidence:\n{json.dumps(evidence, ensure_ascii=False, indent=2)}"
        )[:model_content_limit(150000)]

    def _clarification_resolution_round(
        self,
        output: Path,
        budget: BenchBudget,
        window: BudgetWindow,
        task_text: str,
        resolved: ExecutionBinding,
        state: WorkSnapshot,
        number: int,
        budget_status: dict[str, object],
    ) -> tuple[str, dict[str, object]]:
        stages = self._stages(state)
        s0_record = stages.get("s0")
        if not isinstance(s0_record, dict):
            raise RuntimeStateError("unattended resolution requires the frozen S0 checkpoint")
        snapshot = self._artifacts.load_stage_snapshot(output, s0_record)
        deadline = float(cast(float, budget_status["deadline"]))
        admission = self._artifacts.make_admission(
            budget,
            deadline=deadline,
            workdir=Path(snapshot.roots[0].view_root),
            adapter_id=f"{self._plugin.spec.id}-unattended-resolver-{resolved.fingerprint}",
            stop_requested=self._model_stop_requested,
        )
        context = ClarificationContext(
            profile=resolved.model_context,
            snapshot=snapshot,
            session_id=f"{self._invocation.invocation_id}-unattended-{number}",
            session_dir=output / "unattended-resolution" / f"round-{number:03d}",
            admission=admission,
            deadline=deadline,
        )
        if self._model_window_closed():
            self._invocation.stop_reason = "window_budget_exhausted"
            return "", {"admitted_calls": [], "deadline": deadline, "refused": "window_budget_exhausted"}
        try:
            try:
                text = self._run_async(
                    self._plugin.investigate(
                        context,
                        self._resolution_prompt(state, task_text),
                        stage=f"unattended-question-resolution-{number}",
                    )
                )
            except SideCallIntegrityError as exc:
                recovery = recover_side_call_integrity(
                    exc,
                    stage=f"unattended-question-resolution-{number}",
                    controller=self,
                    plugin=self._plugin,
                )
                if recovery is None:
                    raise
                raise SideCallRecovered(recovery) from exc
        finally:
            self._invocation.clarification_calls.extend(admission.admitted)
        return text, {"admitted_calls": list(admission.admitted), "deadline": deadline}

    def _apply_question_resolutions(
        self,
        runtime: WorkRuntime,
        state: WorkSnapshot,
        resolution: dict[str, object],
    ) -> tuple[WorkSnapshot, dict[str, object]]:
        current = {str(question.get("id")): question for question in state.unresolved_assumptions}
        assumptions: list[dict[str, object]] = []
        deferred: list[dict[str, object]] = []
        gaps: list[dict[str, object]] = []
        for raw in cast(list[object], resolution.get("resolutions", [])):
            if not isinstance(raw, dict):
                continue
            question_id = str(raw.get("id"))
            question = current.get(question_id)
            if question is None:
                continue
            kind = str(raw.get("kind"))
            if kind == "implementation_gap":
                gaps.append(
                    {
                        "id": question_id,
                        "question": _question_text(question),
                        "fix": str(raw.get("resolution") or _question_text(question))[:model_content_limit(4000)],
                        "basis": str(raw.get("basis") or "")[:model_content_limit(4000)],
                    }
                )
            else:
                deferred.append(
                    {
                        "question_id": question_id,
                        "question": _question_text(question),
                        "kind": kind,
                        "assumption": str(raw.get("assumption") or _CONSERVATIVE_ASSUMPTION)[:model_content_limit(4000)],
                        "basis": str(raw.get("basis") or raw.get("source") or "unattended policy")[:model_content_limit(4000)],
                        "recorded_as": "unattended_policy_assumption",
                        "applied": True,
                        "core_answer_provenance": "explicitly marked as assumption, not a real user decision",
                    }
                )
        if gaps:
            # Recording an assumption clears Core's review binding. Defer all
            # assumption decisions until the implementation gaps are fixed so
            # the blocked Work can still accept the requested Worker turn; the
            # next bounded round re-classifies the remaining questions.
            block = (
                "\n\n[UNATTENDED_IMPLEMENTATION_FIXES]\n"
                "The following recorded questions are implementation gaps. Fix them before acceptance; they are "
                "not user decisions and must not be waived.\n"
                + "\n".join(f"- {row['id']}: {row['fix']} (basis: {row['basis']})" for row in gaps)
            )
            existing = str(state.acceptance_brief.get("text") or "")
            if block not in existing:
                state = runtime.set_acceptance_brief(
                    state.work_id,
                    expected_revision=state.revision,
                    text=(existing + block)[:model_content_limit(150000)],
                    source=str(state.acceptance_brief.get("source") or "unattended question resolution"),
                )
            return state, {
                "assumptions": [],
                "implementation_gaps": gaps,
                "deferred_assumptions": deferred,
            }
        for entry in deferred:
            question_id = str(entry["question_id"])
            state = runtime.record_user_decision(
                state.work_id,
                expected_revision=state.revision,
                question_id=question_id,
                answer=_assumption_answer(entry),
            )
            current.pop(question_id, None)
            assumptions.append(entry)
        return state, {"assumptions": assumptions, "implementation_gaps": []}

    def _record_question_resolution(
        self,
        runtime: WorkRuntime,
        output: Path,
        state: WorkSnapshot,
        *,
        number: int,
        status: str,
        budget_status: dict[str, object],
        resolution: dict[str, object] | None,
        applied: dict[str, object] | None,
        clarification: dict[str, object],
        detail: str = "",
        final: bool = False,
    ) -> WorkSnapshot:
        path = output / "unattended-resolution" / f"round-{number:03d}.json"
        assumptions = list(cast(list[dict[str, object]], (applied or {}).get("assumptions", [])))
        gaps = list(cast(list[dict[str, object]], (applied or {}).get("implementation_gaps", [])))
        questions = [
            {
                "id": str(question.get("id")),
                "question": _question_text(question),
                "requirement_ids": question.get("requirement_ids", []),
                "material": question.get("material"),
            }
            for question in state.unresolved_assumptions
        ]
        record: dict[str, object] = {
            "schema": "long-horizon/unattended-question-resolution/v1",
            "work_id": state.work_id,
            "candidate_tree": state.candidate.tree,
            "round": number,
            "status": status,
            "detail": detail[:2000],
            "clarification": clarification,
            "clarification_budget": budget_status,
            "unresolved_questions": questions,
            "assumptions": assumptions,
            "implementation_gaps": gaps,
            "resolution_source": (resolution or {}).get("source"),
        }
        write_json_atomic(path, record)
        progress = self._progress(state)
        previous = progress.get("unattended_resolution", {})
        previous = previous if isinstance(previous, dict) else {}
        rounds = list(cast(list[object], previous.get("rounds", [])))
        summary = {
            "round": number,
            "status": status,
            "detail": detail[:2000],
            "clarification_used": bool(clarification.get("used")),
            "clarification_status": clarification.get("status"),
            "assumptions": len(assumptions),
            "implementation_gaps": len(gaps),
            "unresolved_question_ids": [row["id"] for row in questions],
            "record": contained(output, path),
            "record_sha256": sha256_bytes(path.read_bytes()),
        }
        if rounds and isinstance(rounds[-1], dict) and cast(dict[str, object], rounds[-1]).get("round") == number:
            rounds[-1] = summary
        else:
            rounds.append(summary)
        all_assumptions: list[object] = []
        seen_assumptions: set[str] = set()
        for row in [*cast(list[object], previous.get("assumptions", [])), *assumptions]:
            if not isinstance(row, dict):
                continue
            key = json.dumps(row, sort_keys=True, ensure_ascii=False, default=str)
            if key not in seen_assumptions:
                seen_assumptions.add(key)
                all_assumptions.append(row)
        value = {
            "schema": "long-horizon/unattended-question-resolution-summary/v1",
            "status": status,
            "rounds": rounds,
            "round_count": len([row for row in rounds if isinstance(row, dict) and row.get("clarification_used")]),
            "assumptions": all_assumptions,
            "unresolved_questions": questions,
            "clarification_budget": budget_status,
            "final_detail": detail[:2000] if final else "",
        }
        return self._advance(runtime, state, self._phase(state), unattended_resolution=value)

    def _resolve_unattended_questions(
        self,
        runtime: WorkRuntime,
        output: Path,
        budget: BenchBudget,
        window: BudgetWindow,
        task_text: str,
        resolved: ExecutionBinding,
        driver: Any,
        state: WorkSnapshot,
        result: Any,
        *,
        control: dict[str, bool],
        stop_driver: Callable[[], bool],
        window_deadline: float | None,
    ) -> Any:
        """Resolve question-only blocks without spending the whole trial on clarification."""
        if self._args.allow_user_interaction or not _acceptance_blocked_only_by_questions(state):
            return result
        if window is None:
            snapshot = budget.snapshot()
            deadline = float(cast(float, snapshot["deadline"]))
            window = BudgetWindow(budget.clock(), deadline, deadline, 0.0)
        last_result = result
        max_iterations = max(4, self._args.clarification_max_rounds * 2 + 2)
        for iteration in range(1, max_iterations + 1):
            state = runtime.read_work(state.work_id)
            self._invocation.state = state
            if self._abort.is_set() or self._model_window_closed():
                reason = "invocation_stopped" if self._abort.is_set() else "window_budget_exhausted"
                state = self._record_question_resolution(
                    runtime,
                    output,
                    state,
                    number=iteration,
                    status="invocation_stopped",
                    budget_status=self._clarification_budget_status(state, budget, window),
                    resolution=None,
                    applied=None,
                    clarification={"used": False, "status": "not_started"},
                    detail="model admission closed before question resolution completed",
                    final=True,
                )
                return driver._result(state, reason, budget)
            if state.acceptance is not None:
                state, handled = self._accept_or_audit(
                    runtime, output, budget, window, state, driver
                )
                self._invocation.state = state
                if handled:
                    return driver._result(
                        state, self._invocation.stop_reason or "accepted", budget
                    )
                return driver._result(state, "accepted", budget)
            if not _acceptance_blocked_only_by_questions(state):
                break
            budget_status = self._clarification_budget_status(state, budget, window)
            hard_stop = budget.stop_reason(window.deadline)
            if hard_stop is not None:
                proposed_resolution, proposed_applied = _proposed_question_assumptions(
                    state.unresolved_assumptions, hard_stop
                )
                state = self._record_question_resolution(
                    runtime,
                    output,
                    state,
                    number=iteration,
                    status="budget_exhausted",
                    budget_status=budget_status,
                    resolution=proposed_resolution,
                    applied=proposed_applied,
                    clarification={"used": False, "status": hard_stop},
                    detail=f"question resolution stopped because the trial budget is exhausted: {hard_stop}",
                    final=True,
                )
                return driver._result(state, "acceptance_blocked_unresolved_questions", budget)
            clarification: dict[str, object] = {"used": False, "status": "not_used"}
            applied: dict[str, object] | None = None
            resolution: dict[str, object] | None = None
            if bool(budget_status["available"]):
                try:
                    text, admission = self._clarification_resolution_round(
                        output, budget, window, task_text, resolved, state, iteration, budget_status
                    )
                    resolution = _normalize_question_resolutions(
                        text, state.unresolved_assumptions, fallback_reason="clarification_unclassified_question"
                    )
                    clarification = {
                        "used": True,
                        "status": "completed",
                        "admitted_calls": admission["admitted_calls"],
                        "deadline": admission["deadline"],
                        "reply_sha256": sha256_bytes(text.encode()),
                    }
                except self._artifacts.exhausted as exc:
                    clarification = {
                        "used": True,
                        "status": "stopped" if isinstance(exc, self._artifacts.stopped) else "budget_exhausted",
                        "detail": str(exc)[:1000],
                    }
                    resolution = _fallback_question_resolutions(
                        state.unresolved_assumptions, str(clarification["status"])
                    )
                except SideCallRecovered as exc:
                    clarification = {
                        "used": True,
                        "status": "integrity_recovered",
                        "detail": "side-call integrity recovered; using the conservative assumption route",
                        "integrity_recovery": exc.recovery,
                    }
                    resolution = _fallback_question_resolutions(
                        state.unresolved_assumptions, "integrity_recovered"
                    )
                except NonDegradableIntegrityError:
                    raise
                except Exception as exc:
                    clarification = {
                        "used": True,
                        "status": "failed",
                        "detail": f"{type(exc).__name__}: {exc}"[:1000],
                    }
                    resolution = _fallback_question_resolutions(state.unresolved_assumptions, "clarification_failed")
            else:
                resolution = _fallback_question_resolutions(
                    state.unresolved_assumptions, str(budget_status["reason"] or "clarification_unavailable")
                )
                clarification = {"used": False, "status": str(budget_status["reason"] or "unavailable")}
            # An implementation fix needs a Worker turn. If none remains, keep
            # the conservative assumption route rather than stopping only for
            # lack of an implementation slot.
            implementation_calls = sum(call.get("purpose") == "implementation" for call in budget.snapshot()["calls"])
            remaining_turns = (
                None if self._args.max_turns is None else max(0, self._args.max_turns - implementation_calls)
            )
            if remaining_turns is not None and remaining_turns < 1:
                resolution = _normalize_question_resolutions(
                    None,
                    state.unresolved_assumptions,
                    fallback_reason="no_implementation_turn_remaining",
                )
            state, applied = self._apply_question_resolutions(runtime, state, resolution)
            status = "worker_fix_required" if applied["implementation_gaps"] else "assumptions_recorded"
            previous_attempts = list(cast(list[object], self._progress(state).get("clarification_attempts", [])))
            attempt = {
                "invocation_id": self._invocation.invocation_id,
                "kind": "unattended_resolution",
                "round": iteration,
                "status": clarification.get("status"),
                "admitted_calls": clarification.get("admitted_calls", []),
            }
            state = self._advance(
                runtime,
                state,
                self._phase(state),
                clarification_attempts=[*previous_attempts, attempt],
            )
            state = self._record_question_resolution(
                runtime,
                output,
                state,
                number=iteration,
                status=status,
                budget_status=budget_status,
                resolution=resolution,
                applied=applied,
                clarification=clarification,
            )
            self._invocation.state = state
            if applied["implementation_gaps"]:
                control["worker_after_resolution"] = True
                remaining = (
                    None
                    if self._args.max_turns is None
                    else max(
                        1,
                        self._args.max_turns
                        - sum(call.get("purpose") == "implementation" for call in budget.snapshot()["calls"]),
                    )
                )
                last_result = driver.run(
                    state.work_id,
                    expected_revision=state.revision,
                    max_turns=remaining,
                    window_seconds=None if window is not None else window_deadline,
                    stop_requested=stop_driver,
                )
                state = last_result.snapshot
                self._invocation.state = state
                if state.acceptance is not None:
                    state, handled = self._accept_or_audit(
                        runtime, output, budget, window, state, driver
                    )
                    self._invocation.state = state
                    if handled:
                        return driver._result(
                            state, self._invocation.stop_reason or "accepted", budget
                        )
                if state.delivery is not None:
                    return driver._result(state, "accepted", budget)
                continue
            # All current questions were resolved as evidence-backed or policy
            # assumptions. Request acceptance again without spending a Worker
            # turn; a rejected review still returns to the ordinary Worker path.
            last_result = driver._result(state, "invocation_stopped", budget)
            state, role_stop = driver._review(state, budget, window, purpose="acceptance", full=True, force=True)
            self._invocation.state = state
            if state.acceptance is not None:
                state = self._record_question_resolution(
                    runtime,
                    output,
                    state,
                    number=iteration,
                    status="accepted",
                    budget_status=self._clarification_budget_status(state, budget, window),
                    resolution=resolution,
                    applied=applied,
                    clarification=clarification,
                    final=True,
                )
                state, handled = self._accept_or_audit(
                    runtime, output, budget, window, state, driver
                )
                self._invocation.state = state
                if handled:
                    return driver._result(
                        state, self._invocation.stop_reason or "accepted", budget
                    )
                return driver._result(state, "accepted", budget)
            if role_stop is not None:
                last_result = driver._result(state, role_stop, budget)
                break
            if _acceptance_blocked_only_by_questions(state):
                continue
            # The Reviewer rejected the candidate without leaving a question.
            # Continue implementation rather than treating that as a policy stop.
            control["worker_after_resolution"] = True
            remaining = (
                None
                if self._args.max_turns is None
                else max(
                    1,
                    self._args.max_turns
                    - sum(call.get("purpose") == "implementation" for call in budget.snapshot()["calls"]),
                )
            )
            last_result = driver.run(
                state.work_id,
                expected_revision=state.revision,
                max_turns=remaining,
                window_seconds=None if window is not None else window_deadline,
                stop_requested=stop_driver,
            )
            state = last_result.snapshot
            self._invocation.state = state
            if state.delivery is not None:
                return driver._result(state, "accepted", budget)
        state = runtime.read_work(state.work_id)
        final_status = (
            "acceptance_blocked_unresolved_questions"
            if _acceptance_blocked_only_by_questions(state)
            else last_result.stop_reason
        )
        proposed_resolution, proposed_applied = _proposed_question_assumptions(
            state.unresolved_assumptions, "resolution_loop_exhausted"
        )
        state = self._record_question_resolution(
            runtime,
            output,
            state,
            number=max_iterations,
            status="resolution_loop_exhausted",
            budget_status=self._clarification_budget_status(state, budget, window),
            resolution=proposed_resolution,
            applied=proposed_applied,
            clarification={"used": False, "status": "rounds_or_budget_exhausted"},
            detail="bounded unattended question-resolution loop ended without acceptance",
            final=True,
        )
        return driver._result(state, final_status, budget)

    def _bind_brief(
        self, runtime: WorkRuntime, output: Path, turn_root: Path, state: WorkSnapshot, task_text: str
    ) -> WorkSnapshot:
        stages = self._stages(state)
        recorded = cast(dict[str, object], stages["clarification"])
        bundle = self._artifacts.read_clarification_bundle(turn_root, original_task=task_text)
        if bundle.document_sha256 != recorded.get("document_sha256") or bundle.manifest_sha256 != recorded.get(
            "manifest_sha256"
        ):
            raise RuntimeStateError("clarification bundle changed after it was recorded")
        s0 = cast(dict[str, object], stages["s0"])
        source = (
            f"{self._plugin.spec.brief_source}; original_task_sha256={self._frozen(state)['original_task_sha256']}; "
            f"s0_tree={s0['candidate_tree']}; clarified_requirement_sha256={bundle.document_sha256}; "
            f"manifest_sha256={bundle.manifest_sha256}"
        )
        if state.acceptance_brief.get("text") != bundle.text or state.acceptance_brief.get("source") != source:
            state = runtime.set_acceptance_brief(
                state.work_id, expected_revision=state.revision, text=bundle.text, source=source
            )
        p0 = cast(dict[str, object], stages["p0"])
        handoff = self._artifacts.evidence_checkpoint(
            output_root=output,
            turn_root=turn_root,
            stage="suite-brief-binding" if self._args.workflow == "suite" else "pact-handoff",
            snapshot=self._artifacts.load_stage_snapshot(output, p0),
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
        if self._args.workflow == "suite":
            from .suite import bind_preparation

            state = bind_preparation(self, runtime, output, turn_root, state, task_text)
        return self._advance(runtime, state, "brief_bound", stages={"handoff": handoff})

    def _record_review_quota_repair(
        self,
        runtime: WorkRuntime,
        output: Path,
        state: WorkSnapshot,
        evidence: dict[str, object],
    ) -> WorkSnapshot:
        """Persist reviewer-less repair facts in Work and a bounded artifact."""
        progress = _bench_progress(state)
        progress["review_quota_worker_repair"] = evidence
        state = runtime.update_advisory(
            state.work_id,
            expected_revision=state.revision,
            advisory={**state.advisory, "benchmark": progress},
        )
        write_json_atomic(
            output / "review-quota-worker-repair.json",
            json.loads(json.dumps(evidence, ensure_ascii=True, default=str, sort_keys=True)),
        )
        return state

    def _retain_review_quota_passed_candidate(
        self,
        runtime: WorkRuntime,
        output: Path,
        state: WorkSnapshot,
        evidence: dict[str, object],
        passed_state: WorkSnapshot,
        stage: str,
    ) -> WorkSnapshot:
        """Make the latest bound public pass the export authority.

        A gate records the entry candidate before repair starts, so later failed
        repairs cannot displace it. A legacy run keeps the corresponding sealed
        export receipt instead. Passing repair candidates replace either one.
        """
        verification = passed_state.verification
        if (
            verification is None
            or verification.candidate != passed_state.candidate
            or not _public_passed(passed_state)
        ):
            return state
        if self._gate is not None:
            selected = self._gate.selected
            if selected is not None and selected[0] == passed_state.candidate and selected[1] == verification:
                return state
            self._gate.record(passed_state, stage)
            if self._gate.selected is not None and state.delivery is None:
                state = runtime.update_advisory(
                    state.work_id,
                    expected_revision=state.revision,
                    advisory={**state.advisory, self._gate.advisory_key: self._gate.saved_entries()},
                )
                self._invocation.state = state
            return state
        receipt = self._review_quota_passed_receipt
        if receipt is not None and receipt.candidate == passed_state.candidate:
            return state
        verify_recovered_views()
        try:
            receipt = runtime.export_candidate(
                passed_state.candidate,
                output / "exports" / f"review-quota-repair-{uuid.uuid4().hex}",
            )
            self._review_quota_passed_receipt = receipt
            evidence["delivery_candidate"] = {
                "export": str(receipt.artifact),
                "candidate_tree": receipt.candidate.tree,
                "candidate_checkpoint": receipt.candidate.checkpoint,
            }
        except NonDegradableIntegrityError:
            raise
        except Exception as exc:
            evidence["delivery_candidate_error"] = f"{type(exc).__name__}: {exc}"[:2000]
        return state

    def _try_final_acceptance(
        self,
        runtime: WorkRuntime,
        output: Path,
        budget: BenchBudget,
        window: BudgetWindow,
        state: WorkSnapshot,
        driver: BenchDriver,
    ) -> tuple[WorkSnapshot, bool]:
        """Open the reserved final acceptance when repair time is no longer safe.

        This is still an ordinary Core acceptance admission with a Reviewer; it
        cannot synthesize acceptance from a public pass or from a review body.
        """
        verify_recovered_views()
        if state.acceptance is not None or state.delivery is not None:
            return state, state.acceptance is not None
        if self._abort.is_set():
            return state, False
        progress = _bench_progress(state)
        if not (progress.get("ready") and _public_passed(state)):
            return state, False
        if window is None:
            window = self._restored_budget_window(budget)
        snapshot = budget.snapshot()
        admission = time_admission(
            now=budget.clock(),
            soft_deadline=window.deadline if window is not None else float(snapshot["deadline"]),
            original_deadline=float(snapshot["deadline"]),
            total_seconds=float(self._args.budget_seconds),
            snapshot=snapshot,
            check_durations=self._check_times(),
            full_acceptance=self._full_acceptance_times(),
        )
        self._review_governance["final_acceptance_time_admission"] = admission.evidence()
        if self._time_admission_active() and not admission.acceptance_allowed:
            return state, False
        state, stopped = driver._review(state, budget, window, purpose="acceptance", full=True, force=True)
        self._invocation.state = state
        if state.acceptance is not None and state.delivery is None:
            state, handled = self._accept_or_audit(
                runtime, output, budget, window, state, driver
            )
            self._invocation.state = state
            return state, handled
        return state, False

    def _repair_after_review_quota(
        self,
        runtime: WorkRuntime,
        output: Path,
        budget: BenchBudget,
        window: BudgetWindow,
        state: WorkSnapshot,
        driver: BenchDriver,
    ) -> tuple[WorkSnapshot, bool]:
        """Fallback after the extreme acceptance fuse, never a normal stop.

        No Reviewer is admitted here. Each round receives the latest recorded
        rejection through the ordinary Worker prompt, saves a candidate, and runs
        the public check. The mode keeps the finish reserve plus one full Worker
        round in hand and never creates acceptance, so a passing candidate stays
        explicitly unaccepted.
        """
        if self._abort.is_set() or state.acceptance is not None or state.delivery is not None:
            return state, False
        if self._review_admissions is None:
            return state, False
        payload = self._review_admissions.payload()
        if not acceptance_fuse_reached(payload):
            return state, False
        snapshot = budget.snapshot()
        remaining = snapshot.get("remaining_seconds")
        if (
            isinstance(remaining, bool)
            or not isinstance(remaining, (int, float))
            or not math.isfinite(float(remaining))
        ):
            return state, False
        finish_reserve = self._args.finish_reserve_seconds
        worker_round = worker_repair_round_seconds(snapshot)
        if not worker_repair_allowed(
            remaining_seconds=float(remaining),
            finish_reserve_seconds=finish_reserve,
            worker_round_seconds=worker_round,
            rounds_completed=0,
        ):
            return state, False
        review = state.review
        if (
            review is None
            or not review.feedback.strip()
            or set(review.reasons)
            & {"reviewer_mutation", "reviewer_output_missing", "reviewer_protocol_error", "budget_exhausted"}
        ):
            return state, False
        acceptance_row = cast(dict[str, object], payload.get("purposes", {}).get("acceptance", {}))
        acceptance_count = int(cast(int, acceptance_row.get("count", 0)))
        acceptance_limit = int(cast(int, cast(dict[str, object], payload.get("limits", {})).get("acceptance", 0)))
        evidence: dict[str, object] = {
            "schema": WORKER_REPAIR_SCHEMA,
            "status": "running",
            "trigger": "acceptance_fuse_reached",
            "acceptance_count": acceptance_count,
            "acceptance_limit": acceptance_limit,
            "remaining_seconds_at_entry": round(float(remaining), 3),
            "soft_remaining_seconds_at_entry": round(max(0.0, float(window.deadline) - budget.clock()), 3),
            "finish_reserve_seconds": round(finish_reserve, 3),
            "worker_round_estimate_seconds": round(worker_round, 3),
            "max_rounds": MAX_WORKER_REPAIR_ROUNDS,
            "rounds": [],
            "rounds_completed": 0,
            "latest_review": {
                "artifact": str(review.artifact),
                "candidate_tree": review.candidate.tree,
                "reasons": list(review.reasons),
            },
            "passed_candidate": None,
            "delivery_basis": "last_public_check_passed_unaccepted",
            "stop_reason": None,
        }
        state = self._record_review_quota_repair(runtime, output, state, evidence)
        last_passed: WorkSnapshot | None = state if _public_passed(state) else None
        if last_passed is not None:
            state = self._retain_review_quota_passed_candidate(
                runtime, output, state, evidence, last_passed, "review-quota-entry"
            )
            last_passed = state if _public_passed(state) else last_passed
        rounds = cast(list[dict[str, object]], evidence["rounds"])
        final_reason = "round_limit"
        for round_number in range(1, MAX_WORKER_REPAIR_ROUNDS + 1):
            current = budget.snapshot()
            remaining_value = current.get("remaining_seconds")
            if (
                isinstance(remaining_value, bool)
                or not isinstance(remaining_value, (int, float))
                or not worker_repair_allowed(
                    remaining_seconds=float(remaining_value),
                    finish_reserve_seconds=finish_reserve,
                    worker_round_seconds=worker_round,
                    rounds_completed=round_number - 1,
                )
            ):
                final_reason = "soft_deadline"
                break
            if self._abort.is_set():
                final_reason = "invocation_stopped"
                break
            if self._model_window_closed():
                final_reason = "window_budget_exhausted"
                break
            implementations = sum(
                call.get("purpose") == "implementation"
                for call in cast(list[dict[str, object]], current.get("calls", []))
            )
            if self._args.max_turns is not None and implementations >= self._args.max_turns:
                final_reason = "max_turns"
                break
            deadline = budget.purpose_deadline(window, "implementation")
            role_stop = budget.stop_reason(deadline, purpose="implementation")
            if role_stop is not None:
                final_reason = role_stop
                break
            state, stopped = driver._worker(state, budget, window)
            self._invocation.state = state
            check_deadline = budget.purpose_deadline(window, "implementation")
            check_stop = budget.stop_reason(check_deadline)
            if check_stop is not None and check_stop != "role_budget_exhausted":
                rounds.append(
                    {
                        "round": round_number,
                        "worker_stop": stopped,
                        "remaining_seconds": round(float(cast(float, current["remaining_seconds"])), 3),
                        "public_check": "not_run",
                        "public_passed": False,
                    }
                )
                final_reason = check_stop
                state = self._record_review_quota_repair(runtime, output, state, evidence)
                break
            if self._gate is not None:
                state = self._check_candidate(runtime, state, f"review-quota-repair-{round_number}")
            else:
                state = runtime.verify_candidate(
                    state.work_id, expected_revision=state.revision, deadline=check_deadline
                )
                state = driver._observe(state)
            self._invocation.state = state
            if self._stop_for_public_check(state):
                final_reason = "public_check_unresolved"
            passed = _public_passed(state)
            if passed:
                last_passed = state
                state = self._retain_review_quota_passed_candidate(
                    runtime,
                    output,
                    state,
                    evidence,
                    state,
                    f"review-quota-repair-{round_number}",
                )
                last_passed = state if _public_passed(state) else last_passed
            verification = state.verification
            rounds.append(
                {
                    "round": round_number,
                    "worker_stop": stopped,
                    "remaining_seconds": round(float(cast(float, current["remaining_seconds"])), 3),
                    "candidate_tree": state.candidate.tree,
                    "candidate_checkpoint": state.candidate.checkpoint,
                    "public_check": verification.status if verification is not None else "missing",
                    "public_exit_code": verification.exit_code if verification is not None else None,
                    "public_passed": bool(passed),
                }
            )
            state = self._record_review_quota_repair(runtime, output, state, evidence)
            if final_reason == "public_check_unresolved":
                break
            if stopped is not None:
                final_reason = stopped
                break
            if check_stop is not None:
                final_reason = check_stop
                break
        evidence["rounds_completed"] = len(rounds)
        exit_snapshot = budget.snapshot()
        exit_remaining = exit_snapshot.get("remaining_seconds")
        evidence["remaining_seconds_at_exit"] = (
            round(float(exit_remaining), 3) if isinstance(exit_remaining, (int, float)) else None
        )
        if last_passed is not None:
            evidence["passed_candidate"] = {
                "tree": last_passed.candidate.tree,
                "checkpoint": last_passed.candidate.checkpoint,
                "revision": last_passed.revision,
            }
            state = self._retain_review_quota_passed_candidate(
                runtime, output, state, evidence, last_passed, "review-quota-final"
            )
        evidence["status"] = "completed"
        evidence["stop_reason"] = final_reason
        state = self._record_review_quota_repair(runtime, output, state, evidence)
        return state, True

    def _first_review(
        self, runtime: WorkRuntime, budget: BenchBudget, window: BudgetWindow, state: WorkSnapshot
    ) -> WorkSnapshot:
        """Verify and review the saved baseline before any later Worker.

        The attempt is persisted in BenchDriver's own ``review_pending`` record
        before the review operation, so BenchDriver completes its bookkeeping
        (observations, check proposals, cadence) exactly as for its own reviews.
        """
        baseline = "p1" if self._args.workflow == "suite" else "p0"
        baseline_tree = cast(dict[str, object], self._stages(state)[baseline])["candidate_tree"]
        if self._phase(state) == "first_review_started":
            attempt = _bench_progress(state).get("review_pending")
            if (
                isinstance(attempt, dict)
                and any(
                    call["purpose"] in {"diagnostic", "feedback", "acceptance"} and call["status"] != "started"
                    for call in cast(list[dict[str, object]], budget.snapshot()["calls"])
                )
                and state.review is not None
                and state.review.candidate.tree == baseline_tree
                and str(state.review.artifact) != attempt.get("previous_review")
            ):
                return self._advance(runtime, state, "first_review_done")
        if state.candidate.tree != baseline_tree:
            raise RuntimeStateError(f"first review target differs from the saved {baseline.upper()} candidate")
        # The first review's host check is finish work for an already sealed
        # candidate; it may use the original hard clock after soft closes
        # model admission.
        execution_deadline = getattr(self, "_check_deadline", None) or budget.purpose_deadline(
            window, "implementation"
        )
        stop = budget.stop_reason(window.deadline)
        if stop is not None:
            outcome = {
                **_first_check_outcome(state, window.deadline),
                "outcome": "global_deadline_exhausted",
                "stop_reason": stop,
            }
            state = self._advance(runtime, state, self._phase(state), first_check_outcome=outcome)
            self._invocation.stop_reason = stop
            return state
        # A required public gate has already attempted this candidate. Missing
        # or timed-out evidence is not permission to repeat that check silently.
        if state.verification is None and self._gate is None:
            state = runtime.verify_candidate(
                state.work_id, expected_revision=state.revision, deadline=execution_deadline
            )
            state = runtime.update_advisory(
                state.work_id,
                expected_revision=state.revision,
                advisory={**state.advisory, "benchmark": review_progress.observe(_bench_progress(state), state)},
            )
        state = self._save_host_check(runtime, state, self._read_host_check(state, "first_review"))
        if self._stop_for_public_check(state):
            return state
        route = self._progress(state).get("last_host_public_check") if self._rpc_public_check else None
        host = route.get("host_outcome") if isinstance(route, dict) else None
        outcome = _first_check_outcome(
            state,
            window.deadline,
            check_configured=self._args.verify_command is not None or self._gate is not None,
            host_outcome=host if isinstance(host, dict) else None,
        )
        if isinstance(route, dict):
            if (
                isinstance(host, dict)
                and host.get("error_kind") in {"public_test_passed", "public_test_failed"}
                and state.verification is not None
                and state.verification.status == "failed"
            ):
                # Public or derived tests may use code125. A proven completed
                # public command cannot promote Core's failed check set.
                outcome.update(outcome="command_failure_diagnostic", stop_reason=None)
        methods, other = method_diagnosis(state)
        if methods:
            outcome["method_prerequisites"] = methods
            outcome["other_failures_possible"] = other
        state = self._advance(runtime, state, self._phase(state), first_check_outcome=outcome)
        if outcome["stop_reason"] is not None:
            self._invocation.stop_reason = str(outcome["stop_reason"])
            return state
        progress = _bench_progress(state)
        passed = _public_passed(state)
        full = bool(progress.get("ready")) and passed
        if (
            self._plugin.evidence_configuration()
            .get("public_verification", "")
            .startswith("terminal-candidate-review/")
        ):
            full = full and not any(check["disposition"] == "pending" for check in state.checks)
        purpose = "acceptance" if full else "feedback" if passed else "diagnostic"
        if self._time_admission_active():
            snapshot = budget.snapshot()
            admission = time_admission(
                now=budget.clock(),
                soft_deadline=window.deadline,
                original_deadline=float(snapshot["deadline"]),
                total_seconds=float(self._args.budget_seconds),
                snapshot=snapshot,
                check_durations=self._check_times(),
                full_acceptance=self._full_acceptance_times(),
            )
            allowed = (
                admission.acceptance_allowed
                if purpose == "acceptance"
                else admission.diagnosis_allowed
            )
            rejection_condition = (
                None
                if allowed
                else (
                    "soft_remaining < acceptance_reserve"
                    if purpose == "acceptance"
                    else "soft_remaining <= worker_estimate + acceptance_reserve"
                )
            )
            if "first_review_time_admission" not in self._review_governance:
                self._review_governance["first_review_time_admission"] = {
                    **admission.evidence(),
                    "purpose": purpose,
                    "allowed": allowed,
                    "rejection_condition": rejection_condition,
                }
            if not allowed:
                self._invocation.stop_reason = "review_time_admission"
                return state
        deadline = budget.purpose_deadline(window, purpose)
        stop = budget.stop_reason(deadline, purpose=purpose)
        if stop is not None:
            # Keep the phase: the first review must happen before Worker 2.
            self._invocation.stop_reason = stop
            return state
        if self._review_admissions is not None:
            decision = self._review_admissions.admit(
                state,
                purpose=purpose,
                full=full,
                force=False,
                check=lambda: budget.stop_reason(deadline, purpose=purpose),
            )
            if not decision["admitted"]:
                # Once diagnosis/repair capacity is spent, keep the reserved
                # acceptance allowance usable. Same-input refusal is the
                # anti-retry guard and still stops rather than re-reviewing it.
                if (
                    not method_diagnosis(state)[0]
                    and decision.get("category") == "diagnosis"
                    and decision.get("reason") == "diagnosis_limit"
                    and decision.get("acceptance_remaining", 0) > 0
                ):
                    purpose, full = "acceptance", True
                    deadline = budget.purpose_deadline(window, purpose)
                    stop = budget.stop_reason(deadline, purpose=purpose)
                    if stop is not None:
                        self._invocation.stop_reason = stop
                        return state
                    decision = self._review_admissions.admit(
                        state,
                        purpose=purpose,
                        full=True,
                        force=True,
                        check=lambda: budget.stop_reason(deadline, purpose=purpose),
                    )
                if not decision["admitted"]:
                    self._invocation.stop_reason = str(
                        decision.get("stop") or "review_limit_reached"
                    )
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
        if self._model_window_closed():
            self._invocation.stop_reason = "window_budget_exhausted"
            return state
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
        if self._adapters[1].budget_failure == "TurnBudgetExhausted":
            self._raise_native_turn_refusal(window, budget)
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
        plugin = getattr(self, "_plugin", None)
        integrity_adapters = (
            *self._adapters,
            *getattr(plugin, "_side_integrity_adapters", ()),
        )
        findings = [failure for adapter in integrity_adapters
                    for failure in getattr(adapter, "integrity_failures", ())
                    if not failure.get("recovered")]
        integrity = next((row for row in findings if row["reason"] == "reviewer_restore_failed"),
                         findings[0] if findings else None)
        if integrity is not None and (
            self._invocation.failure is None
            or self._invocation.failure.get("reason") in ("reviewer_mutation", "reviewer_restore_failed")
        ):
            self._invocation.failure = dict(integrity)
            self._invocation.stop_reason = str(integrity["reason"])
        failure = next((adapter.budget_failure for adapter in self._adapters if adapter.budget_failure), None)
        if failure is not None:
            self._invocation.budget_detail = failure
            self._invocation.stop_reason = "budget_exhausted"
        drift = next(
            (adapter.configuration_failure for adapter in self._adapters if adapter.configuration_failure), None
        )
        if drift is not None:
            self._invocation.stop_reason = "configuration_drift"
        if any(getattr(adapter, "candidate_untrusted", False) for adapter in self._adapters):
            # A failed restore leaves the current candidate untrustworthy.  Stop
            # scheduling work and let the existing archive/gate fallback publish
            # the last candidate that still has complete evidence.
            self._invocation.stop_reason = "reviewer_restore_failed"
            if self._invocation.failure is None:
                self._invocation.failure = {
                    "type": "ReviewerIntegrityError", "reason": "reviewer_restore_failed",
                    "detail": "reviewer restoration failed; candidate is untrusted",
                }

    def _raise_native_turn_refusal(self, window: BudgetWindow | None, budget: BenchBudget) -> None:
        """Recover a typed pre-native refusal without refunding an admitted call."""
        total = float(self._args.budget_seconds)
        deadline = window.deadline if window is not None else cast(float, budget.snapshot()["deadline"])
        raise TurnWindowSpent(
            "native role refused the turn window",
            remaining_seconds=official_residual(deadline, total, budget.clock()),
            total_seconds=total,
        )

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
        progress = self._progress(state)
        progress["phase"] = phase
        if stages:
            progress["stages"] = {**cast(dict[str, object], progress["stages"]), **stages}
        progress.update(updates)
        advisory = {**state.advisory, self._plugin.spec.progress_key: progress}
        if benchmark is not None:
            advisory["benchmark"] = benchmark
        state = runtime.update_advisory(state.work_id, expected_revision=state.revision, advisory=advisory)
        self._invocation.state = state
        return state

    def _worktree_root(self) -> Path | None:
        return self._args.worktree_root.expanduser().resolve() if self._args.worktree_root else None

    # -- export and report ---------------------------------------------

    def _code_sha256(self) -> str:
        override = os.environ.get("LH_CODE_SHA", "").strip()
        if override:
            return override
        digest = hashlib.sha256()
        root = Path(__file__).resolve().parent
        for name in (
            "controller.py",
            "progress.py",
            "review_admissions.py",
            "review_governance.py",
            "acceptance_timing.py",
            "review_limits.py",
            "review_transport.py",
        ):
            path = root / name
            try:
                digest.update(name.encode("utf-8"))
                digest.update(path.read_bytes())
            except OSError:
                continue
        return digest.hexdigest()

    def _model_window_closed(self) -> bool:
        """Whether a new model turn is forbidden, independent of teardown.

        Explicit cancellation and the hard deadline set ``_abort``; the soft
        deadline only closes model admission so an admitted check/export can
        complete.
        """
        if self._abort.is_set():
            return True
        if self._soft_deadline is None:
            return False
        budget = getattr(self, "_budget", None)
        now = budget.clock() if budget is not None else time.time()
        return now >= self._soft_deadline

    def _model_stop_requested(self) -> bool:
        return self._model_window_closed()

    def _record_finish_observation(self, seconds: float, *, check_seconds: float | None = None) -> None:
        if (
            isinstance(seconds, bool)
            or not isinstance(seconds, (int, float))
            or not math.isfinite(float(seconds))
            or float(seconds) < 0
        ):
            return
        value = float(seconds)
        if not hasattr(self, "_finish_observations"):
            self._finish_observations = []
        self._finish_observations.append(value)
        if self._phase_policy is not None:
            self._phase_policy.observe_finish(value)
        if check_seconds is not None and math.isfinite(float(check_seconds)) and float(check_seconds) >= 0:
            if not hasattr(self, "_public_check_durations"):
                self._public_check_durations = []
            self._public_check_durations.append(float(check_seconds))

    def _finish_reserve_evidence(self) -> dict[str, object]:
        if self._phase_policy is not None:
            seconds, source = self._phase_policy.finish_reserve()
        else:
            seconds, source = float(self._args.finish_reserve_seconds), "configured"
        observations = getattr(self, "_finish_observations", [])
        return {
            "seconds": round(float(seconds), 3),
            "source": source,
            "observations_seconds": [round(float(value), 3) for value in observations],
        }

    def _check_priority_candidate(
        self, runtime: WorkRuntime, state: WorkSnapshot, *, stage: str = "deadline-priority-check"
    ) -> WorkSnapshot:
        """Complete one sealed candidate's first host check before more model work.

        A previous passing candidate is authoritative and is never displaced by
        a later failed check. Once a candidate has a bound verification (pass or
        fail), this does not start another check for it.
        """
        if self._gate is None or self._budget is None or self._abort.is_set():
            return state
        if state.status == "completed" or getattr(state, "delivery", None) is not None:
            return state
        if self._gate.selected is not None or _public_passed(state):
            return state
        verification = state.verification
        if verification is not None and verification.candidate == state.candidate:
            return state
        check_deadline = self._check_deadline or self._original_deadline
        if check_deadline is not None and self._budget.clock() >= check_deadline:
            return state
        state = self._check_candidate(runtime, state, stage, deadline=check_deadline)
        self._invocation.state = state
        return state

    def _time_admission_active(self) -> bool:
        """Time admission applies to real multi-hour windows, not short fixtures."""
        if float(self._args.budget_seconds) < 3600.0:
            return False
        window_seconds = getattr(self._args, "window_seconds", None)
        return not (
            window_seconds is not None
            and float(window_seconds) < 3600.0
        )

    def _review_governance_payload(self, state: WorkSnapshot | None) -> dict[str, object]:
        """Structured metadata only: no Reviewer prose, tokens marked unknown."""
        payload = self._review_admissions.payload()
        limits = payload.get("limits", {})
        policy = {
            "schema": "long-horizon/review-governance-policy/v1",
            "diagnosis_fuse": limits.get("diagnosis"),
            "acceptance_fuse": limits.get("acceptance"),
            "same_purpose_same_input_limit": limits.get("same_input"),
            "progression_stop_rounds": 3,
            "same_gap_stop_rounds": 3,
            "diagnosis_pause_rounds": 2,
            "time_admission": "S > W + A opens another worker/check round",
            "acceptance_reserve": "max(10min, 1.25 * observed acceptance cost); retain 5% T until 3 comparable complete candidates",
            "format_repair": "one short repair per admission; two protocol-failed admissions are a stall",
        }
        encoded_policy = json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()
        calls: list[dict[str, object]] = []
        usage = {"status": "unknown", "tokens": None, "cost": None}
        try:
            if self._budget is not None:
                snapshot = self._budget.snapshot()
                calls = [dict(row) for row in cast(list[dict[str, object]], snapshot.get("calls", []))]
                usage = cast(dict[str, object], snapshot.get("usage", usage))
        except Exception:
            calls = []
        admissions = [
            dict(row) for row in cast(list[dict[str, object]], payload.get("admissions", [])) if isinstance(row, dict)
        ]
        admission_rows: list[dict[str, object]] = []
        for index, row in enumerate(admissions):
            admitted_at = row.get("admitted_at")
            next_at = admissions[index + 1].get("admitted_at") if index + 1 < len(admissions) else None
            matched = []
            for call in calls:
                started = call.get("started_at")
                if not isinstance(admitted_at, (int, float)) or not isinstance(started, (int, float)):
                    continue
                if started < float(admitted_at):
                    continue
                if isinstance(next_at, (int, float)) and started >= float(next_at):
                    continue
                if call.get("role") != "reviewer" or call.get("purpose") != row.get("purpose"):
                    continue
                duration = call.get("observed_duration_seconds")
                kind = "format_repair" if isinstance(duration, (int, float)) and duration < 120 else "body"
                matched.append({"call_id": call.get("id"), "kind": kind, "status": call.get("status")})
            admission_rows.append(
                {
                    "admission_id": row.get("admission_id"),
                    "purpose": row.get("purpose"),
                    "category": row.get("category"),
                    "full": bool(row.get("full")),
                    "force": bool(row.get("force")),
                    "input_sha256": row.get("input_sha256"),
                    "admitted_at": admitted_at,
                    "calls": matched,
                }
            )
        review = state.review if state is not None else None
        candidate = state.candidate if state is not None else None
        acceptance = state.acceptance if state is not None else None
        final = {
            "candidate_tree": getattr(candidate, "tree", None),
            "candidate_checkpoint": getattr(candidate, "checkpoint", None),
            "acceptance_artifact": str(acceptance.artifact) if acceptance is not None else None,
            "accepted": acceptance is not None,
        }
        governance = {
            "schema": "long-horizon/review-governance-evidence/v1",
            "code_sha": self._code_sha256(),
            "effective_policy": policy,
            "effective_policy_sha256": hashlib.sha256(encoded_policy).hexdigest(),
            "fuses": {"diagnosis": limits.get("diagnosis"), "acceptance": limits.get("acceptance")},
            "admission_calls": admission_rows,
            "refusals": list(cast(list[object], payload.get("refusals", []))),
            "time_admission": self._review_governance.get("last_time_admission"),
            "first_review_time_admission": self._review_governance.get("first_review_time_admission"),
            "final_acceptance_time_admission": self._review_governance.get("final_acceptance_time_admission"),
            "soft_deadline_unix": self._review_governance.get("soft_deadline_unix"),
            "progress_guard": self._review_governance.get("progress_guard"),
            "structured_verdict": {
                "artifact": str(review.artifact) if review is not None else None,
                "reasons": list(review.reasons) if review is not None else [],
                "complete": bool(getattr(review, "complete", False)) if review is not None else False,
                "parse_status": (
                    "protocol_error"
                    if review is not None and "reviewer_protocol_error" in set(review.reasons)
                    else "recorded"
                    if review is not None
                    else "missing"
                ),
                "protocol_error": bool(
                    review is not None and "reviewer_protocol_error" in set(review.reasons)
                ),
                "core_rejection_reasons": [
                    reason
                    for reason in (list(review.reasons) if review is not None else [])
                    if reason not in {"feedback_recorded", "gate_complete"}
                ],
            },
            "usage": usage,
            "tokens": usage.get("tokens") if isinstance(usage, dict) else None,
            "cost": usage.get("cost") if isinstance(usage, dict) else None,
            "usage_status": usage.get("status", "unknown") if isinstance(usage, dict) else "unknown",
            "final_delivery_candidate": final,
        }
        return governance

    def _publish(self, runtime: WorkRuntime, output: Path, frozen: dict[str, object] | None) -> Path | None:
        verify_recovered_views()
        invocation = self._invocation
        state = invocation.state
        export: LatestCandidateExport | None = None
        export_status = "not_attempted"
        export_error: str | None = None
        record: dict[str, object] | None = None
        record_path: Path | None = None
        final_stage: dict[str, object] | None = None
        budget_snapshot: dict[str, object] | None = None
        latest_check_error: str | None = None
        export_failure: dict[str, object] | None = None
        archive_basis: dict[str, object] | None = None
        inputs_valid = (
            frozen is not None and state is not None and state.advisory.get(self._plugin.spec.input_key) == frozen
        )
        can_export = state is not None and inputs_valid and not self._integrity_hard_failure
        if self._integrity_hard_failure:
            export_status = "refused_integrity_failure"
            export_failure = {
                "type": "NonDegradableIntegrityError",
                "reason": "integrity_failure",
                "detail": "export refused after an unrecoverable protected-view integrity failure",
            }
            if invocation.failure is None:
                invocation.failure = dict(export_failure)
        if can_export:
            try:
                state = runtime.read_work(state.work_id)
                invocation.state = state
                trials = runtime.workspace / ".pact" / "bench" / "trials"
                # Reading must never create a ledger: that would start the trial clock.
                if (trials / self._args.trial_id / "budget.json").is_file():
                    budget_snapshot = BenchBudget(trials, self._args.config(), state.work_id).snapshot()
                try:
                    state = self._check_candidate(runtime, state, "latest")
                except Exception as exc:
                    # A failed final re-check must not discard candidates that
                    # already passed the gate or Core acceptance. Export below
                    # keeps every integrity check; this only records the error.
                    latest_check_error = f"{type(exc).__name__}: {exc}"[:2000]
                    state = runtime.read_work(state.work_id)
                invocation.state = state
                if self._gate is not None:
                    unresolved_current = self._stop_for_public_check(state)
                    selected = self._gate.selected
                    blocked_selection = unresolved_current and (
                        selected is None or selected[0].checkpoint != self._public_check_fallback_checkpoint
                    )
                    receipt = (
                        None
                        if blocked_selection
                        else self._gate.export(runtime, output / "exports" / invocation.invocation_id)
                    )
                    export = LatestCandidateExport(receipt, ())
                    export_status = "exported" if receipt is not None else "no_publicly_validated_candidate"
                elif self._review_quota_passed_receipt is not None:
                    # Legacy/no-gate path: retain the last candidate that passed
                    # the public check even if a later repair round regressed.
                    export = LatestCandidateExport(self._review_quota_passed_receipt, ())
                    export_status = "exported_from_review_quota_repair"
                elif self._rpc_public_check:
                    export = LatestCandidateExport(None, ())
                    export_status = "no_publicly_validated_candidate"
                else:
                    export = runtime.export_latest_candidate(
                        state.work_id, output / "exports" / invocation.invocation_id, expected_revision=state.revision
                    )
                    export_status = "exported" if export.receipt is not None else "no_exportable_candidate"
                if (
                    self._early_audit_fallback_receipt is not None
                    and not self._early_audit_replaced
                ):
                    # The frozen, fully accepted candidate remains the delivery
                    # unless a fresh candidate passed public checks and a fresh
                    # acceptance. Never promote an unaccepted audit candidate.
                    export = LatestCandidateExport(self._early_audit_fallback_receipt, ())
                    export_status = "exported_accepted_fallback"
                self._candidate_available = export.receipt is not None
            except NonDegradableIntegrityError:
                raise
            except Exception as exc:
                export_status = "export_failed"
                export_error = f"{type(exc).__name__}: {exc}"[:2000]
                export_failure = {
                    "type": type(exc).__name__,
                    "reason": "export_error",
                    "detail": str(exc)[:2000],
                }
            if can_export and (export is None or export.receipt is None):
                from .harbor_unified.delivery import select_fallback

                try:
                    chosen = select_fallback(
                        output,
                        None,
                        archive_roots=(self._archive_root,) if self._archive_root is not None else (),
                    )
                except Exception as archive_error:
                    # Keep the original export failure visible; an unreadable
                    # archive is not evidence that a candidate was delivered.
                    if export_failure is not None:
                        export_failure["archive_error"] = f"{type(archive_error).__name__}: {archive_error}"[:2000]
                    chosen = None
                if chosen is not None:
                    archived_receipt, archive_basis = chosen
                    export = LatestCandidateExport(archived_receipt, ())
                    export_status = "exported_from_archive"
                    self._candidate_available = True
                    if export_failure is not None:
                        invocation.failure = invocation.failure or export_failure
            if export is not None and invocation.budget_detail is not None:
                # Adapter-owned latch: create cannot replay; explicit resume may continue.
                with contextlib.suppress(Exception):
                    self._record_work_budget(
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
                    final_stage, _snapshot = self._artifacts.stage_record(
                        output_root=output,
                        turn_root=self._turn_root(output, state.work_id),
                        stage="final",
                        receipt=export.receipt,
                        metadata={"work_id": state.work_id, "invocation_id": invocation.invocation_id},
                        archive_roots=(self._archive_root,) if self._archive_root is not None else (),
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
        payload["latest_check_error"] = latest_check_error
        if archive_basis is not None:
            payload["archive_fallback"] = archive_basis
        if self._post_delivery_host_checks:
            payload["post_delivery_host_public_checks"] = self._post_delivery_host_checks
        try:
            return publish_selection(
                output,
                payload,
                schema=self._plugin.spec.result_schema,
                archive_roots=(self._archive_root,) if self._archive_root is not None else (),
            )
        except NonDegradableIntegrityError:
            raise
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
        """Write the per-invocation workflow record, then validate it against the Core receipt."""
        records = output / "records"
        records.mkdir(parents=True, exist_ok=True)
        configuration_failure = next(
            (adapter.configuration_failure for adapter in self._adapters if adapter.configuration_failure), None
        )
        artifact = records / f"{state.work_id}-{self._invocation.invocation_id}-result.json"
        if artifact.exists() or artifact.is_symlink():
            raise RuntimeContractError("benchmark result destination already exists")
        fallback_selected = (
            export.receipt is not None
            and self._early_audit_fallback_receipt is not None
            and export.receipt.artifact == self._early_audit_fallback_receipt.artifact
        )
        acceptance_payload = (
            self._early_audit_acceptance
            if fallback_selected
            else asdict(state.acceptance)
            if export.receipt and export.receipt.accepted and state.acceptance
            else None
        )
        verification_payload = (
            self._early_audit_verification
            if fallback_selected
            else asdict(self._gate.selected[1])
            if self._gate and self._gate.selected
            else asdict(state.verification)
            if self._gate is None and state.verification
            else None
        )
        verification_sha256: str | None = None
        if isinstance(verification_payload, dict):
            verification_artifact = verification_payload.get("artifact")
            if isinstance(verification_artifact, str) and verification_artifact:
                try:
                    verification_sha256 = sha256_file(Path(verification_artifact))
                except OSError:
                    verification_sha256 = None
        if verification_sha256 is None and self._gate is not None and self._gate.selected is not None:
            verification_sha256 = self._gate.selected[3]
        delivery_payload = (
            asdict(state.delivery)
            if (
                export.receipt is not None
                and export.receipt.accepted
                and state.delivery is not None
                and state.delivery.checkpoint == export.receipt.candidate.checkpoint
            )
            else None
        )
        record = {
            "schema": self._plugin.spec.result_schema,
            "invocation_id": self._invocation.invocation_id,
            "work_id": state.work_id,
            "revision": state.revision,
            "task_id": self._args.task_id,
            "trial_id": self._args.trial_id,
            "stop_reason": "configuration_drift" if configuration_failure else self._invocation.stop_reason,
            "configuration_failure": configuration_failure,
            "status": state.status,
            "internal_accepted": export.receipt is not None and export.receipt.accepted,
            "delivered": delivery_payload is not None,
            "core_candidate": asdict(state.candidate),
            "candidate": asdict(export.receipt.candidate) if export.receipt else None,
            "acceptance": acceptance_payload,
            "delivery": delivery_payload,
            "public_verification": verification_payload,
            "public_verification_sha256": verification_sha256,
            "export": asdict(export.receipt) if export.receipt else None,
            "export_diagnostics": [asdict(item) for item in export.diagnostics],
            "budget": budget,
            "finish_reserve_seconds": self._finish_reserve_evidence()["seconds"],
            "finish_reserve_source": self._finish_reserve_evidence()["source"],
            "public_checks": list(self._public_check_evidence),
            "external_grade": {"status": "not_run", "reward": None},
        }
        if self._args.reviewer_launch_policy is not None:
            record["review_isolation"] = self._review_isolation()
            record["review_isolation_reason"] = self._review_isolation_reason()
        write_json_atomic(artifact, json.loads(json.dumps(record, ensure_ascii=True, default=str)))
        record = read_json(artifact)
        self._validate_record(record, state, export.receipt)
        return artifact, record

    def _record_work_budget(
        self, *, workspace: Path, work_id: str, revision: int, detail: str, artifacts: Path
    ) -> None:
        write_json_atomic(
            workspace / ".pact-io" / self._plugin.spec.suspension_directory / f"{work_id}.json",
            {
                "schema": "long-horizon/work-budget/v1",
                "work_id": work_id,
                "revision": revision,
                "detail": detail,
                "artifacts": str(artifacts),
                "next_action": "Inspect the original trial ledger and retained candidate; resume explicitly.",
            },
        )

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
                "schema": self._plugin.spec.result_schema,
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
        recovery_report = getattr(self._plugin, "transport_recovery_report", None)
        if recovery_report is not None:
            receipts = recovery_report()
            if receipts is not None:
                base["transport_recovery"] = receipts
        progress = state.advisory.get(self._plugin.spec.progress_key) if state is not None else None
        progress = progress if isinstance(progress, dict) else {}
        resolution = progress.get("unattended_resolution")
        resolution = resolution if isinstance(resolution, dict) else {}
        stages = dict(cast(dict[str, object], progress.get("stages", {})))
        if final_stage is not None:
            stages["final"] = final_stage
        s0 = stages.get("s0")
        export_baseline = read_json(receipt.artifact).get("baseline_commit") if receipt is not None else None
        export_patch = (
            contained_in_roots(
                receipt.patch,
                output_root=output,
                archive_roots=(self._archive_root,) if self._archive_root is not None else (),
            )
            if receipt is not None
            else None
        )
        base.update(
            {
                "schema": self._plugin.spec.result_schema,
                "workflow": "long-horizon-benchmark",
                "export": receipt_payload(receipt) if receipt is not None else None,
                "export_status": export_status,
                "export_error": export_error,
                "export_patch": export_patch["path"] if export_patch is not None else None,
                "export_patch_root": export_patch["root"] if export_patch is not None else None,
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
                    dict[str, object], state.advisory.get(self._plugin.spec.execution_key, {})
                ).get("fingerprint")
                if state is not None
                else None,
                "host_public_checks": progress.get("host_public_checks", {}),
                "internal_failure": invocation.failure,
                "budget_suspension": invocation.budget_detail,
                "record": contained(output, record_path) if record_path is not None else None,
                "long_horizon": {
                    "phase": progress.get("phase"),
                    "p0": progress.get("p0"),
                    "p0_ran_this_invocation": invocation.p0_ran,
                    "workflow": self._args.workflow,
                    "suite": state.advisory.get("long_horizon_suite") if state else None,
                    "clarification_attempts": progress.get("clarification_attempts", []),
                    "clarification_calls_this_invocation": invocation.clarification_calls,
                    "first_review": progress.get("first_review"),
                    "unresolved_questions": resolution.get(
                        "unresolved_questions",
                        [
                            {"id": str(question.get("id")), "question": _question_text(question)}
                            for question in state.unresolved_assumptions
                        ]
                        if state is not None
                        else [],
                    ),
                    "assumptions": resolution.get("assumptions", []),
                    "resolution_rounds": resolution.get("rounds", []),
                    "resolution_round_count": resolution.get("round_count", 0),
                    "unattended_resolution": resolution,
                    "early_acceptance_audit": self._early_audit_evidence
                    or (state.advisory.get("early_acceptance_audit") if state is not None else None),
                    "review_governance": self._review_governance_payload(state),
                    "finish_reserve_seconds": self._finish_reserve_evidence()["seconds"],
                    "finish_reserve_source": self._finish_reserve_evidence()["source"],
                    "finish_reserve_observations_seconds": self._finish_reserve_evidence()[
                        "observations_seconds"
                    ],
                    "public_checks": list(self._public_check_evidence),
                    "stages": stages,
                },
                "early_acceptance_audit": self._early_audit_evidence
                or (state.advisory.get("early_acceptance_audit") if state is not None else None),
                "external_grade": {"status": "not_run", "reward": None},
            }
        )
        quota_repair = _bench_progress(state).get("review_quota_worker_repair") if state is not None else None
        if isinstance(quota_repair, dict):
            long_horizon = base.get("long_horizon")
            if isinstance(long_horizon, dict):
                long_horizon["review_quota_worker_repair"] = quota_repair
        if self._args.reviewer_launch_policy is not None:
            base["review_isolation"] = self._review_isolation()
            base["review_isolation_reason"] = self._review_isolation_reason()
        base.update(
            workflow_status=state.status if state else "not_created",
            generation_status=("candidate_validated" if self._gate else "candidate_exported")
            if receipt
            else "no_valid_candidate",
            core_candidate=asdict(state.candidate) if state else None,
            candidate=asdict(receipt.candidate) if receipt else None,
            internal_accepted=bool(receipt and receipt.accepted),
            delivered=bool(record.get("delivered"))
            if record is not None
            else bool(
                receipt
                and receipt.accepted
                and state
                and state.delivery
                and state.delivery.checkpoint == receipt.candidate.checkpoint
            ),
            public_candidate_gate=read_json(self._gate.path) if self._gate else None,
            finish_reserve_seconds=self._finish_reserve_evidence()["seconds"],
            finish_reserve_source=self._finish_reserve_evidence()["source"],
            public_checks=list(self._public_check_evidence),
        )
        if record is None:
            base["stop_reason"] = invocation.stop_reason
        if not base.get("public_verification_sha256"):
            if self._gate is not None and self._gate.selected is not None:
                # The check summary is part of the binding, not a decorative status.
                base["public_verification_sha256"] = self._gate.selected[3]
            elif state is not None and state.verification is not None:
                with contextlib.suppress(OSError):
                    base["public_verification_sha256"] = sha256_file(Path(state.verification.artifact))
        return base

    def _review_isolation(self) -> str:
        policy = self._args.reviewer_launch_policy
        if policy is None:
            return "unverified"
        if policy.mode == "degraded":
            return "degraded"
        return "uid_drop" if policy.proofs and policy.enforcement_error is None else "unverified"

    def _review_isolation_reason(self) -> str | None:
        policy = self._args.reviewer_launch_policy
        return policy.enforcement_error or policy.reason if policy is not None else None

    def _validate_record(self, record: dict[str, object], state: WorkSnapshot, receipt: ExportReceipt | None) -> None:
        """The per-invocation record must reference exactly this work and export."""
        if record.get("schema") != self._plugin.spec.result_schema or record.get("work_id") != state.work_id:
            raise BenchmarkFailure("benchmark record does not reference this work")
        if receipt is None:
            if record.get("export") is not None:
                raise BenchmarkFailure("benchmark record claims an export that does not exist")
            return
        verify_export(receipt)
        exported = record.get("export")
        if not isinstance(exported, dict) or exported.get("artifact") != str(receipt.artifact):
            raise BenchmarkFailure("benchmark record export differs from the Core receipt")

    def _require_same(
        self, state: WorkSnapshot, frozen: dict[str, object], binding: dict[str, object], profile: str
    ) -> None:
        if state.advisory.get(self._plugin.spec.input_key) != frozen:
            raise RuntimeContractError("existing trial uses different task, configuration or output inputs")
        if state.advisory.get(self._plugin.spec.configuration_key) != self._plugin.configuration(profile):
            raise RuntimeContractError("existing trial uses a different agent profile or workflow")
        stored = state.advisory.get(self._plugin.spec.execution_key)
        if not isinstance(stored, dict) or stored.get("fingerprint") != binding["fingerprint"]:
            raise RuntimeContractError("configuration_drift: existing trial uses a different execution configuration")

    def _progress(self, state: WorkSnapshot) -> dict[str, object]:
        raw = state.advisory.get(self._plugin.spec.progress_key)
        if (
            not isinstance(raw, dict)
            or raw.get("schema") != self._plugin.spec.progress_schema
            or raw.get("phase") not in _PHASES
        ):
            raise RuntimeContractError("unsupported or missing long-horizon benchmark progress")
        return json.loads(json.dumps(raw))

    def _stages(self, state: WorkSnapshot) -> dict[str, object]:
        return cast(dict[str, object], self._progress(state)["stages"])

    def _phase(self, state: WorkSnapshot) -> Phase:
        return cast(Phase, self._progress(state)["phase"])

    def _frozen(self, state: WorkSnapshot) -> dict[str, object]:
        raw = state.advisory.get(self._plugin.spec.input_key)
        if not isinstance(raw, dict) or raw.get("schema") != self._plugin.spec.input_schema:
            raise RuntimeContractError("unsupported or missing long-horizon benchmark input")
        return raw


def _bench_progress(state: WorkSnapshot) -> dict[str, object]:
    raw = state.advisory.get("benchmark", {"schema": _BENCH_PROGRESS_SCHEMA})
    if not isinstance(raw, dict) or raw.get("schema") != _BENCH_PROGRESS_SCHEMA:
        raise RuntimeContractError("unsupported benchmark progress schema")
    return json.loads(json.dumps(raw))


def _recorded_structural_failure(artifact: Path) -> bool:
    """Core's built-in checks live in its verification artifact, not derived checks."""
    if not isinstance(artifact, Path):
        return False
    try:
        record = read_json(artifact)
    except (OSError, TypeError, ValueError):
        return False
    if (
        not isinstance(record, dict)
        or record.get("status") != "failed"
        or "exit_code" not in record
        or record["exit_code"] is not None
    ):
        return False
    checks = record.get("checks")
    return isinstance(checks, list) and any(
        isinstance(check, dict)
        and isinstance(check.get("name"), str)
        and check.get("name")
        in {"checkpoint_readable", "patch_generatable", "namespace_clean", "required_artifacts_present"}
        and check.get("passed") is False
        for check in checks
    )


def _first_check_outcome(
    state: WorkSnapshot,
    deadline: float,
    *,
    check_configured: bool = True,
    host_outcome: dict[str, object] | None = None,
) -> dict[str, object]:
    """Classify recorded evidence without inferring a cause from arbitrary text.

    ``check_configured`` distinguishes an absent public check from a configured
    check that could not run: when no check is configured, Core's ``unavailable``
    result only means there was nothing to verify, so the first review proceeds
    with ``not_configured`` and no stop reason. This still grants no pass, retry,
    or acceptance. RPC stderr markers are signals, not authenticated
    infrastructure diagnoses. Unknown results stay unresolved; no classification
    grants a retry or a pass.
    """
    verification = state.verification
    outcome, stop = "missing", "public_check_missing"
    if verification is not None:
        if verification.candidate != state.candidate:
            outcome, stop = "candidate_mismatch", "public_check_candidate_mismatch"
        elif isinstance(host_outcome, dict) and host_outcome.get("error_kind") == "public_check_timeout":
            # The host already bounded and cleaned this check. It is a
            # diagnostic timeout, never a pass and never an unbounded retry.
            outcome, stop = "timeout_diagnostic", None
        elif verification.status == "passed":
            outcome, stop = "passed", None
        elif verification.status == "timeout":
            outcome, stop = "timeout_diagnostic", None
        elif verification.status == "failed":
            code = verification.exit_code
            markers = {"public_check_runtime_error", "public_check_rpc_unavailable"}
            if code == 125:
                outcome = (
                    "infrastructure_signal" if verification.stderr_tail.strip() in markers else "unclassified_error"
                )
                stop = (
                    "public_check_infrastructure_unresolved"
                    if outcome == "infrastructure_signal"
                    else "public_check_unclassified_error"
                )
            elif type(code) is int and code > 0:
                outcome, stop = "command_failure_diagnostic", None
            elif code is None and _recorded_structural_failure(verification.artifact):
                outcome, stop = "structural_failure_diagnostic", None
            else:
                outcome, stop = "unclassified_error", "public_check_unclassified_error"
        elif verification.status == "unavailable" and not check_configured:
            outcome, stop = "not_configured", None
        else:
            outcome, stop = "unavailable", "public_check_unavailable"
    return {
        "schema": "long-horizon/first-public-check-outcome/v1",
        "candidate_tree": state.candidate.tree,
        "verification_artifact": str(verification.artifact) if verification is not None else None,
        "verification_status": verification.status if verification is not None else None,
        "exit_code": verification.exit_code if verification is not None else None,
        "global_deadline_unix": deadline,
        "outcome": outcome,
        "stop_reason": stop,
        "automatic_recheck": False,
        "infrastructure_cause_established": False,
    }


def _public_passed(state: WorkSnapshot) -> bool:
    return state.verification is not None and (
        state.verification.status == "passed"
        or (state.verification.status == "unavailable" and state.policy.allow_unverified)
    )


def _acceptance_blocked_only_by_questions(state: WorkSnapshot) -> bool:
    """Stop scheduling only after Core's complete Gate for this exact candidate."""
    if (
        state.status != "blocked"
        or not state.unresolved_assumptions
        or state.acceptance is not None
        or state.delivery is not None
        or state.review is None
        or state.review.candidate != state.candidate
        or set(state.review.reasons) != {"gate_complete", "unresolved_user_questions"}
        or not _public_passed(state)
    ):
        return False
    # Core defines complete as promotion_ready AND no unresolved questions;
    # it is acceptance, not whether the Reviewer invocation finished.
    return not state.review.complete


_RESOLUTION_KINDS = frozenset({"evidence_resolved", "implementation_gap", "user_decision"})
_CONSERVATIVE_ASSUMPTION = (
    "Preserve all existing behavior, follow the original task's literal requirements, and add no unrequested "
    "behavior. Where the text remains ambiguous, choose the smallest change supported by the repository and "
    "recorded evidence rather than inventing a new requirement."
)


def _question_text(question: dict[str, object]) -> str:
    for key in ("assumption", "question", "text", "summary"):
        value = question.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:model_content_limit(4000)]
    return json.dumps(question, ensure_ascii=False, sort_keys=True)[:model_content_limit(4000)]


def _conservative_assumption(question: dict[str, object], reason: str) -> str:
    return (
        f"{_CONSERVATIVE_ASSUMPTION} Question: {_question_text(question)} "
        f"[basis: {reason}; unattended policy; not a user decision]"
    )[:model_content_limit(4000)]


def _extract_resolution_object(text: str) -> dict[str, object] | None:
    """Read one bounded JSON object from an otherwise free-form read-only reply."""
    if not isinstance(text, str) or not text.strip() or len(text) > scaled(150000):
        return None
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate, flags=re.IGNORECASE)
    for value in (candidate, candidate[candidate.find("{") : candidate.rfind("}") + 1]):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _normalize_question_resolutions(
    text: str | None,
    questions: tuple[dict[str, object], ...],
    *,
    fallback_reason: str,
) -> dict[str, object]:
    """Turn read-only advice into one bounded disposition per current question.

    Missing, malformed or unknown entries never keep Core blocked indefinitely.
    They collapse to the conservative unattended assumption and retain their
    reason in the evidence, so a later auditor can distinguish evidence-backed
    resolution from a policy default.
    """
    parsed = _extract_resolution_object(text) if text is not None else None
    raw = parsed.get("resolutions") if isinstance(parsed, dict) else None
    by_id: dict[str, dict[str, object]] = {}
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            question_id = item.get("id")
            if isinstance(question_id, str) and question_id not in by_id:
                by_id[question_id] = item
    entries: list[dict[str, object]] = []
    for question in questions:
        question_id = str(question.get("id"))
        item = by_id.get(question_id)
        kind = item.get("kind") if isinstance(item, dict) else None
        if kind not in _RESOLUTION_KINDS:
            entry = {
                "id": question_id,
                "kind": "user_decision",
                "assumption": _conservative_assumption(question, fallback_reason),
                "resolution": "",
                "basis": fallback_reason,
                "source": "policy_default",
            }
        else:
            assumption = item.get("assumption") or item.get("resolution")
            resolution = item.get("resolution") or item.get("fix") or item.get("instruction")
            evidence = item.get("evidence") or item.get("basis")
            basis = (
                "; ".join(str(row) for row in evidence)
                if isinstance(evidence, list)
                else str(evidence)
                if evidence is not None
                else ""
            )
            if kind == "implementation_gap":
                entry = {
                    "id": question_id,
                    "kind": kind,
                    "assumption": "",
                    "resolution": str(resolution or _question_text(question))[:model_content_limit(4000)],
                    "basis": basis[:model_content_limit(4000)],
                    "source": "clarification",
                }
            else:
                assumption_text = str(assumption).strip() if assumption is not None else ""
                if not assumption_text:
                    assumption_text = _conservative_assumption(question, "clarification_returned_no_assumption")
                entry = {
                    "id": question_id,
                    "kind": kind,
                    "assumption": assumption_text[:model_content_limit(4000)],
                    "resolution": str(resolution or assumption_text)[:model_content_limit(4000)],
                    "basis": basis[:model_content_limit(4000)],
                    "source": "clarification",
                }
        entry["question"] = _question_text(question)
        entries.append(entry)
    return {
        "schema": "long-horizon/unattended-question-resolutions/v1",
        "source": "clarification" if parsed is not None and by_id else "policy_default",
        "fallback_reason": fallback_reason,
        "resolutions": entries,
    }


def _assumption_answer(entry: dict[str, object]) -> str:
    return (
        "[unattended-policy-assumption v1; not a user decision] "
        + str(entry.get("assumption") or _CONSERVATIVE_ASSUMPTION)
        + " | basis: "
        + str(entry.get("basis") or entry.get("source") or "unattended policy")
    )[:model_content_limit(8000)]


def _fallback_question_resolutions(questions: tuple[dict[str, object], ...], reason: str) -> dict[str, object]:
    return _normalize_question_resolutions(None, questions, fallback_reason=reason)


def _proposed_question_assumptions(
    questions: tuple[dict[str, object], ...], reason: str
) -> tuple[dict[str, object], dict[str, object]]:
    """Record the conservative defaults a budget-starved stop would have used."""
    resolution = _fallback_question_resolutions(questions, reason)
    proposed = [
        {
            **entry,
            "recorded_as": "proposed_unattended_policy_assumption",
            "applied": False,
        }
        for entry in cast(list[dict[str, object]], resolution["resolutions"])
    ]
    return resolution, {"assumptions": proposed, "implementation_gaps": []}


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
        f"{build_runtime_handoff_instructions()}" + worker_evidence_guidance()
    )


def _first_review_context(state: WorkSnapshot, purpose: str) -> str:
    instruction = (
        "Diagnose the recorded public result for this first candidate and check method validity. "
        "Identify the violated original requirement, cite the recorded evidence, specify the concrete "
        "implementation change or evidence-backed check-method correction, and say how to validate it. "
        "A recorded timeout is not a functional failure or permission to rerun the unchanged check. "
        "This is feedback, not permission to waive supplied checks or the original task."
        if purpose == "diagnostic"
        else "Review this first candidate against the original task. Identify missing behavior and "
        "actionable next steps; this is feedback only."
    )
    return (
        f"{instruction}\nThe acceptance brief is a clarification of the original task derived only from the "
        "unmodified baseline (S0). It is advisory analysis, not a new user requirement, and it does not "
        f"answer open questions. Brief source: {state.acceptance_brief.get('source', 'none')}"
        + reviewer_ui_state_evidence_guidance()
        + reviewer_delivery_hygiene_guidance()
    )
