"""Stable Execution Kernel facade for standalone and Runtime callers.

The facade exposes versioned request/outcome identities and artifact references.
It deliberately knows nothing about Campaigns or Work State: a Runtime may
evaluate an outcome, authorize deferred promotion, and then commit its own
canonical state through a separate control-plane boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ._work_execution_support import workspace_identity as workspace_identity
from ._execution_finalization import finalize
from ._work_execution import (
    WorkCandidate as WorkCandidate,
    WorkExecutionHandle as WorkExecutionHandle,
    WorkVerification as WorkVerification,
    accept_work_candidate as accept_work_candidate,
    capture_work_candidate as capture_work_candidate,
    prepare_work_execution as prepare_work_execution,
    read_work_candidate as read_work_candidate,
    read_work_execution as read_work_execution,
    read_work_operation as read_work_operation,
    verify_work_candidate as verify_work_candidate,
)
from ._execution_operations import ExecutionIO
from .adapters.base import AgentAdapter
from .artifacts import (
    LoopPaths,
    append_event,
    atomic_write_text,
    build_loop_paths,
    read_json,
    round_paths,
    write_json_atomic,
)
from .checkpoint import CheckpointStore
from .checkpoint import default_worktree_root as default_worktree_root
from .config import LoopConfig
from .controller import _run_execution
from .schemas import (
    EXECUTION_OUTCOME_SCHEMA,
    EXECUTION_REQUEST_SCHEMA,
    FINAL_VALIDATION_SCHEMA,
    GATE_DECISION_SCHEMA,
    INITIALIZATION_SCHEMA,
    PROMOTION_RECEIPT_SCHEMA,
    PROMOTION_SCHEMA,
    STATE_SCHEMA,
    FinalValidationStatus,
    InitializationCheckpoint,
    LoopStatus,
    PromotionMode,
    PromotionStatus,
)


class KernelContractError(RuntimeError):
    """Raised when a caller supplies stale or inconsistent Kernel identity."""


@dataclass(frozen=True, slots=True)
class ExecutionRequest:
    """One versioned invocation of the bounded Execution Kernel."""

    config: LoopConfig
    worker: AgentAdapter
    reviewer: AgentAdapter
    promotion_mode: PromotionMode
    initial_feedback: str = ""
    schema: str = field(default=EXECUTION_REQUEST_SCHEMA, init=False)


@dataclass(frozen=True, slots=True)
class ExecutionEvidenceRefs:
    """Paths to canonical evidence; payloads remain in their native artifacts."""

    manifest: Path
    initialization: Path
    state: Path
    events: Path
    worker_result: Path | None
    verification: Path | None
    reviewer_result: Path | None
    gate_decision: Path | None
    final_validation: Path | None
    final_patch: Path
    promotion: Path


@dataclass(frozen=True, slots=True)
class ExecutionOutcome:
    """Identity and evidence references for one finished Kernel invocation."""

    loop_id: str
    loop_dir: Path
    source_workspace: Path
    source_head_commit: str
    source_index_tree: str
    source_workspace_tree: str
    status: LoopStatus
    stop_reason: str
    rounds: int
    candidate_checkpoint: str
    round_gate_status: str | None
    final_validation_status: FinalValidationStatus | None
    promotion_mode: PromotionMode
    promotion_status: PromotionStatus
    promotion_reason: str
    promotion_ready: bool
    evidence: ExecutionEvidenceRefs
    schema: str = field(default=EXECUTION_OUTCOME_SCHEMA, init=False)


@dataclass(frozen=True, slots=True)
class PromotionAuthorization:
    """Caller-supplied optimistic identity guard for deferred promotion."""

    outcome: ExecutionOutcome
    expected_source_workspace: Path
    expected_source_head_commit: str
    expected_source_index_tree: str
    expected_source_workspace_tree: str
    expected_candidate_checkpoint: str


@dataclass(frozen=True, slots=True)
class PromotionReceipt:
    """Durable result of resolving one deferred promotion candidate."""

    loop_id: str
    status: PromotionStatus
    reason: str
    checkpoint: str
    patch_path: Path
    artifact: Path
    schema: str = field(default=PROMOTION_RECEIPT_SCHEMA, init=False)


def execute(request: ExecutionRequest) -> ExecutionOutcome:
    """Execute one request and return references suitable for policy evaluation."""
    if request.schema != EXECUTION_REQUEST_SCHEMA:
        raise KernelContractError(f"unsupported execution request schema: {request.schema}")
    if request.promotion_mode not in {"automatic", "deferred"}:
        raise KernelContractError(f"unsupported promotion mode: {request.promotion_mode!r}")
    summary = _run_execution(
        request.config,
        worker=request.worker,
        reviewer=request.reviewer,
        promotion_mode=request.promotion_mode,
        yield_on_plan_challenge=request.promotion_mode == "deferred",
        initial_feedback=request.initial_feedback,
    )
    paths = build_loop_paths(request.config.workspace.expanduser().resolve(), summary.loop_id)
    initialization = _read_initialization(paths)
    current_round = round_paths(paths.loop_dir, summary.rounds) if summary.rounds else None
    evidence = ExecutionEvidenceRefs(
        manifest=paths.manifest,
        initialization=paths.initialization,
        state=paths.state,
        events=paths.events,
        worker_result=_existing(current_round.worker_result if current_round else None),
        verification=_existing(current_round.verification if current_round else None),
        reviewer_result=_existing(current_round.reviewer_result if current_round else None),
        gate_decision=_existing(current_round.gate if current_round else None),
        final_validation=_existing(paths.final_validation),
        final_patch=paths.final_patch,
        promotion=paths.promotion,
    )
    promotion_ready = (
        request.promotion_mode == "deferred"
        and summary.status == "complete"
        and summary.round_gate_status == "complete"
        and summary.final_validation_status in {"passed", "not_configured"}
        and summary.promotion_status == "not_attempted"
    )
    return ExecutionOutcome(
        loop_id=summary.loop_id,
        loop_dir=summary.loop_dir,
        source_workspace=initialization.source_workspace,
        source_head_commit=initialization.head_commit,
        source_index_tree=initialization.index_tree,
        source_workspace_tree=initialization.workspace_tree,
        status=summary.status,
        stop_reason=summary.stop_reason,
        rounds=summary.rounds,
        candidate_checkpoint=summary.final_checkpoint,
        round_gate_status=summary.round_gate_status,
        final_validation_status=summary.final_validation_status,
        promotion_mode=request.promotion_mode,
        promotion_status=summary.promotion_status,
        promotion_reason=summary.promotion_reason,
        promotion_ready=promotion_ready,
        evidence=evidence,
    )


def promote(authorization: PromotionAuthorization) -> PromotionReceipt:
    """Resolve one deferred candidate after validating caller and artifact identity."""
    outcome = authorization.outcome
    _validate_authorization(authorization)
    paths = build_loop_paths(outcome.source_workspace, outcome.loop_id)
    _validate_evidence_paths(outcome, paths)

    initialization = _read_initialization(paths)
    state = read_json(paths.state)
    promotion_artifact = read_json(paths.promotion)
    gate = _read_required(outcome.evidence.gate_decision, GATE_DECISION_SCHEMA, "gate")
    final_validation = _read_required(
        outcome.evidence.final_validation,
        FINAL_VALIDATION_SCHEMA,
        "final validation",
    )
    _validate_promotion_artifacts(
        outcome,
        initialization=initialization,
        state=state,
        promotion=promotion_artifact,
        gate=gate,
        final_validation=final_validation,
    )

    store = CheckpointStore(outcome.source_workspace, outcome.loop_id)
    store.restore_promotion_candidate(initialization, outcome.candidate_checkpoint)
    promotion_status, promotion_reason = finalize(
        _execution_io(),
        store,
        paths,
        state,
        status="complete",
        stop_reason=outcome.stop_reason,
        rounds=outcome.rounds,
        checkpoint=outcome.candidate_checkpoint,
        promote=True,
    )
    return PromotionReceipt(
        loop_id=outcome.loop_id,
        status=promotion_status,
        reason=promotion_reason,
        checkpoint=outcome.candidate_checkpoint,
        patch_path=paths.final_patch,
        artifact=paths.promotion,
    )


def recover_promotion(outcome: ExecutionOutcome) -> PromotionReceipt | None:
    """Read a resolved receipt after interruption; never apply a patch again.

    A source change without a complete, gate-bound receipt is ambiguous and
    remains a hard error. This facade is used only by Runtime recovery.
    """
    if not outcome.promotion_ready:
        return None
    paths = build_loop_paths(outcome.source_workspace, outcome.loop_id)
    _validate_evidence_paths(outcome, paths)
    initialization = _read_initialization(paths)
    promotion = _read_required(paths.promotion, PROMOTION_SCHEMA, "promotion")
    if promotion.get("status") == "not_attempted":
        return None
    status = promotion.get("status")
    if status not in {"applied", "no_changes", "blocked", "failed"}:
        raise KernelContractError("invalid resolved promotion status")
    state = read_json(paths.state)
    _validate_promotion_artifacts(outcome, initialization=initialization,
        state=state, promotion=dict(promotion, status="not_attempted"),
        gate=_read_required(outcome.evidence.gate_decision, GATE_DECISION_SCHEMA, "gate"),
        final_validation=_read_required(outcome.evidence.final_validation,
            FINAL_VALIDATION_SCHEMA, "final validation"))
    if (promotion.get("patch_path") != str(paths.final_patch)
            or state.get("promotion_status") != status
            or state.get("promotion_reason") != promotion.get("reason")):
        raise KernelContractError("resolved promotion state/receipt mismatch")
    store = CheckpointStore(outcome.source_workspace, outcome.loop_id)
    store.restore_promotion_candidate(initialization, outcome.candidate_checkpoint)
    current = workspace_identity(outcome.source_workspace)
    expected_tree = (store._output(["rev-parse", f"{outcome.candidate_checkpoint}^{{tree}}"])
                     if status in {"applied", "no_changes"} else initialization.workspace_tree)
    expected = {"head_commit": initialization.head_commit, "branch": initialization.branch,
        "index_tree": initialization.index_tree, "workspace_tree": expected_tree}
    if current != expected:
        raise KernelContractError("source does not match resolved promotion receipt")
    # Recompute the authorized patch without changing source or an artifact.
    if paths.final_patch.read_bytes().decode("utf-8", errors="surrogateescape") != store._diff(
            initialization.baseline_commit, outcome.candidate_checkpoint):
        raise KernelContractError("resolved promotion patch mismatch")
    return PromotionReceipt(loop_id=outcome.loop_id, status=status,
        reason=_required_str(promotion, "reason"), checkpoint=outcome.candidate_checkpoint,
        patch_path=paths.final_patch, artifact=paths.promotion)


def _execution_io() -> ExecutionIO:
    return ExecutionIO(
        write_json=write_json_atomic,
        write_text=atomic_write_text,
        append_event=append_event,
    )


def _existing(path: Path | None) -> Path | None:
    return path if path is not None and path.is_file() else None


def _read_required(path: Path | None, schema: str, label: str) -> dict[str, object]:
    if path is None or not path.is_file():
        raise KernelContractError(f"missing {label} evidence")
    payload = read_json(path)
    if payload.get("schema") != schema:
        raise KernelContractError(f"unsupported {label} schema: {payload.get('schema')!r}")
    return payload


def _read_initialization(paths: LoopPaths) -> InitializationCheckpoint:
    payload = _read_required(paths.initialization, INITIALIZATION_SCHEMA, "initialization")
    branch = payload.get("branch")
    if branch is not None and not isinstance(branch, str):
        raise KernelContractError("initialization branch must be a string or null")
    return InitializationCheckpoint(
        source_workspace=Path(_required_str(payload, "source_workspace")),
        head_commit=_required_str(payload, "head_commit"),
        branch=branch,
        index_tree=_required_str(payload, "index_tree"),
        workspace_tree=_required_str(payload, "workspace_tree"),
        status=_required_str(payload, "status"),
        baseline_commit=_required_str(payload, "baseline_commit"),
        baseline_tree=_required_str(payload, "baseline_tree"),
        checkpoint_ref=_required_str(payload, "checkpoint_ref"),
        worker_worktree=Path(_required_str(payload, "worker_worktree")),
    )


def _required_str(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise KernelContractError(f"artifact field {key!r} must be a string")
    return value


def _validate_authorization(authorization: PromotionAuthorization) -> None:
    outcome = authorization.outcome
    if outcome.schema != EXECUTION_OUTCOME_SCHEMA:
        raise KernelContractError(f"unsupported execution outcome schema: {outcome.schema}")
    if not outcome.promotion_ready:
        raise KernelContractError("execution outcome is not promotion-ready")
    expected_pairs = (
        (
            authorization.expected_source_workspace.expanduser().resolve(),
            outcome.source_workspace.expanduser().resolve(),
            "source workspace",
        ),
        (
            authorization.expected_source_head_commit,
            outcome.source_head_commit,
            "source HEAD",
        ),
        (
            authorization.expected_source_index_tree,
            outcome.source_index_tree,
            "source index tree",
        ),
        (
            authorization.expected_source_workspace_tree,
            outcome.source_workspace_tree,
            "source workspace tree",
        ),
        (
            authorization.expected_candidate_checkpoint,
            outcome.candidate_checkpoint,
            "candidate checkpoint",
        ),
    )
    for expected, actual, label in expected_pairs:
        if expected != actual:
            raise KernelContractError(f"expected {label} does not match execution outcome")


def _validate_evidence_paths(outcome: ExecutionOutcome, paths: LoopPaths) -> None:
    if outcome.loop_dir != paths.loop_dir:
        raise KernelContractError("execution outcome loop directory does not match loop identity")
    fixed_paths = (
        (outcome.evidence.manifest, paths.manifest, "manifest"),
        (outcome.evidence.initialization, paths.initialization, "initialization"),
        (outcome.evidence.state, paths.state, "state"),
        (outcome.evidence.events, paths.events, "events"),
        (outcome.evidence.final_patch, paths.final_patch, "final patch"),
        (outcome.evidence.promotion, paths.promotion, "promotion"),
    )
    for actual, expected, label in fixed_paths:
        if actual != expected:
            raise KernelContractError(f"execution outcome {label} path is inconsistent")


def _validate_promotion_artifacts(
    outcome: ExecutionOutcome,
    *,
    initialization: InitializationCheckpoint,
    state: dict[str, object],
    promotion: dict[str, object],
    gate: dict[str, object],
    final_validation: dict[str, object],
) -> None:
    if state.get("schema") != STATE_SCHEMA:
        raise KernelContractError(f"unsupported state schema: {state.get('schema')!r}")
    if promotion.get("schema") != PROMOTION_SCHEMA:
        raise KernelContractError(
            f"unsupported promotion schema: {promotion.get('schema')!r}"
        )
    if initialization.source_workspace.expanduser().resolve() != outcome.source_workspace:
        raise KernelContractError("initialization source workspace does not match outcome")
    source_identity = (
        initialization.head_commit,
        initialization.index_tree,
        initialization.workspace_tree,
    )
    if source_identity != (
        outcome.source_head_commit,
        outcome.source_index_tree,
        outcome.source_workspace_tree,
    ):
        raise KernelContractError("initialization source identity does not match outcome")
    if state.get("status") != "complete":
        raise KernelContractError("persisted execution state is not complete")
    if state.get("current_checkpoint") != outcome.candidate_checkpoint:
        raise KernelContractError("persisted candidate checkpoint does not match outcome")
    if state.get("round_gate_status") != "complete" or gate.get("status") != "complete":
        raise KernelContractError("persisted gate evidence is not complete")
    if final_validation.get("candidate_commit") != outcome.candidate_checkpoint:
        raise KernelContractError("final validation candidate does not match outcome")
    if final_validation.get("status") not in {"passed", "not_configured"}:
        raise KernelContractError("persisted final validation does not allow promotion")
    if promotion.get("checkpoint") != outcome.candidate_checkpoint:
        raise KernelContractError("promotion artifact candidate does not match outcome")
    if promotion.get("status") != "not_attempted":
        raise KernelContractError("deferred promotion is already resolved")


__all__ = [
    "ExecutionEvidenceRefs",
    "ExecutionOutcome",
    "ExecutionRequest",
    "KernelContractError",
    "PromotionAuthorization",
    "PromotionReceipt",
    "execute",
    "promote",
    "recover_promotion",
]
