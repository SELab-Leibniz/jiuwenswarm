"""Core PACT domain types and schema version constants.

Schema identifiers for artifacts and public seam values follow
``pact-core/<name>/vN`` and advance independently. Changing a field shape means
a new version constant, never an in-place mutation (AGENTS.md invariant 9).
``role-result`` is currently v4, ``manifest`` is v4, ``state`` is v4, ``event``
is v2, ``reviewer-decision`` is v2, and ``agent-event`` is v1; final validation
starts at v1 and unchanged artifacts remain v1.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

SCHEMA_PREFIX = "pact-core"

MANIFEST_SCHEMA = "pact-core/manifest/v4"
STATE_SCHEMA = "pact-core/state/v4"
EVENT_SCHEMA = "pact-core/event/v2"
INITIALIZATION_SCHEMA = "pact-core/initialization/v1"
ROLE_RESULT_SCHEMA = "pact-core/role-result/v4"
AGENT_EVENT_SCHEMA = "pact-core/agent-event/v1"
CHECKPOINT_SCHEMA = "pact-core/checkpoint/v1"
REVIEWER_MUTATION_SCHEMA = "pact-core/reviewer-mutation/v1"
VERIFICATION_SCHEMA = "pact-core/verification/v1"
GATE_DECISION_SCHEMA = "pact-core/gate-decision/v1"
PROMOTION_SCHEMA = "pact-core/promotion/v1"
FINAL_VALIDATION_SCHEMA = "pact-core/final-validation/v1"
EXECUTION_REQUEST_SCHEMA = "pact-core/execution-request/v2"
EXECUTION_OUTCOME_SCHEMA = "pact-core/execution-outcome/v1"
PROMOTION_RECEIPT_SCHEMA = "pact-core/promotion-receipt/v1"
REVIEW_DECISION_SCHEMA = "pact-core/reviewer-decision/v2"

Role = Literal["worker", "reviewer"]

LoopStatus = Literal["initialized", "running", "complete", "failed", "cancelled"]

TurnStatus = Literal["completed", "output_missing", "timeout", "spawn_failed"]

EventParseStatus = Literal["complete", "partial", "unavailable"]

TurnTerminationReason = Literal[
    "completed",
    "output_missing",
    "process_exit_nonzero",
    "role_timeout",
    "no_progress_timeout",
    "permission_pending_timeout",
    "provider_idle_timeout",
    "output_limit_exceeded",
]

ReviewMarker = Literal["complete", "failed", "continue"]

ReviewDecisionCaptureStatus = Literal[
    "valid",
    "missing",
    "malformed",
    "unsupported",
    "not_applicable",
]

PlanChallengeCaptureStatus = Literal[
    "valid",
    "absent",
    "malformed",
    "unavailable",
    "unsupported",
    "not_applicable",
]

PlanChallengeRecommendedAction = Literal[
    "replan",
    "retry",
    "request_user",
    "block",
]

VerificationStatus = Literal["passed", "failed", "timeout", "unavailable"]

FinalValidationStatus = Literal[
    "not_configured", "passed", "failed", "timeout", "unavailable"
]

GateStatus = Literal["complete", "failed", "forced_continue"]

PromotionStatus = Literal["not_attempted", "applied", "no_changes", "blocked", "failed"]

PromotionMode = Literal["automatic", "deferred"]

OperationName = Literal[
    "worker_turn",
    "checkpoint",
    "public_verification",
    "reviewer_turn",
    "gate",
    "final_validation",
    "promotion",
]


@dataclass(frozen=True, slots=True)
class TurnRequest:
    """One agent turn: self-contained prompt in, process exit as the boundary."""

    role: Role
    prompt: str
    workdir: Path
    timeout_seconds: float
    artifact_dir: Path | None = None
    # Transport intent for reviewer turns; never inferred from prompt wording.
    review_mode: Literal["acceptance", "feedback"] = "acceptance"
    # Optional absolute UTC epoch deadline shared across externally bounded stages.
    deadline: float | None = None


@dataclass(frozen=True, slots=True)
class TurnTelemetry:
    """Privacy-safe process/activity metadata for one adapter turn."""

    process_started_at: str
    process_exited_at: str
    process_pid: int
    prompt_sent_at: str
    first_activity_at: str | None
    last_activity_at: str | None
    provider_first_activity_at: str | None
    permission_pending_at: str | None
    termination_reason: TurnTerminationReason
    no_progress_timeout_seconds: float
    process_group_terminated: bool
    stdout_bytes: int
    stdout_sha256: str
    stderr_bytes: int
    stderr_sha256: str


@dataclass(frozen=True, slots=True)
class PlanChallenge:
    """Reviewer proposal that the current execution route should be reconsidered."""

    reason: str
    gap_signature: str
    recommended_action: PlanChallengeRecommendedAction


@dataclass(frozen=True, slots=True)
class ReviewDecisionCapture:
    """Adapter-normalized, independently validated reviewer decision fields."""

    verdict_status: ReviewDecisionCaptureStatus
    verdict: ReviewMarker | None = None
    plan_challenge_status: PlanChallengeCaptureStatus = "unavailable"
    plan_challenge: PlanChallenge | None = None
    raw_text: str = ""
    error: str = ""
    plan_challenge_error: str = ""


@dataclass(frozen=True, slots=True)
class TurnResult:
    """Adapter-normalized outcome of one agent turn."""

    status: TurnStatus
    final_text: str
    output_source: str
    exit_code: int | None
    stdout_tail: str = ""
    stderr_tail: str = ""
    duration_seconds: float = 0.0
    events_path: Path | None = None
    telemetry: TurnTelemetry | None = None
    session_id: str | None = None
    native_store_path: Path | None = None
    event_parse_status: EventParseStatus = "unavailable"
    event_count: int = 0
    review_decision: ReviewDecisionCapture = field(
        default_factory=lambda: ReviewDecisionCapture(
            verdict_status="unsupported",
            plan_challenge_status="unsupported",
        )
    )


@dataclass(frozen=True, slots=True)
class AdapterProbe:
    """CLI reachability / version probe result for one adapter.

    ``version``/``detail`` cover reachability. The remaining fields are a
    *declared* provenance snapshot taken at loop start: what the adapter was
    configured with, never a guess inferred from the environment at manifest
    write time. Their defaults are always ``"unavailable"`` /
    ``"cli_default_unknown"`` — an adapter that does not know a value must
    say so, not omit it.
    """

    adapter_id: str
    available: bool
    version: str = ""
    detail: str = ""
    binary_name: str = "unavailable"
    model: str = "unavailable"
    model_source: str = "cli_default_unknown"
    provider: str = "unavailable"
    reasoning_variant: str = "unavailable"
    reasoning_variant_source: str = "cli_default_unknown"
    profile: str = "unavailable"
    isolation_mode: str = "unavailable"


@dataclass(frozen=True, slots=True)
class VerificationCheck:
    """One named deterministic check inside a verification run."""

    name: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """Deterministic verification outcome for one checkpoint (never LLM-based)."""

    status: VerificationStatus
    checks: list[VerificationCheck] = field(default_factory=list)
    command: str = ""
    exit_code: int | None = None
    duration_seconds: float = 0.0
    stdout_tail: str = ""
    stderr_tail: str = ""


@dataclass(frozen=True, slots=True)
class FinalValidationResult:
    """Opaque terminal validation outcome, never supplied to an agent role."""

    status: FinalValidationStatus
    command_sha256: str = ""
    exit_code: int | None = None
    duration_seconds: float = 0.0
    stdout_tail: str = ""
    stderr_tail: str = ""


@dataclass(frozen=True, slots=True)
class GateWaiver:
    """Audit record of an explicit exemption applied during gate evaluation."""

    kind: Literal["allow_unverified"]
    verification_status: VerificationStatus
    detail: str = ""


@dataclass(frozen=True, slots=True)
class GateDecision:
    """Pure-function completion authority output (FR-6)."""

    status: GateStatus
    marker: ReviewMarker
    terminal_line: str = ""
    blocked_by: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    waiver: GateWaiver | None = None


@dataclass(frozen=True, slots=True)
class WorkspaceSnapshot:
    """Git-visible state for a workspace at a PACT boundary."""

    head_commit: str
    branch: str | None
    index_tree: str
    workspace_tree: str
    status: str


@dataclass(frozen=True, slots=True)
class InitializationCheckpoint:
    """Round 0 baseline and private worker checkout details."""

    source_workspace: Path
    head_commit: str
    branch: str | None
    index_tree: str
    workspace_tree: str
    status: str
    baseline_commit: str
    baseline_tree: str
    checkpoint_ref: str
    worker_worktree: Path


@dataclass(frozen=True, slots=True)
class RoundCheckpoint:
    """Controller-created checkpoint for one worker round."""

    round_number: int
    parent_commit: str
    commit: str
    tree: str
    status: str
    changed_paths: list[str]
    namespace_violations: list[str]
    round_diff: str
    cumulative_diff: str


@dataclass(frozen=True, slots=True)
class ReviewerMutation:
    """Net reviewer checkout mutation captured before disposal."""

    changed: bool
    head_commit: str
    index_tree: str
    workspace_tree: str
    status: str
    diff: str
    namespace_violations: list[str]


@dataclass(frozen=True, slots=True)
class PromotionResult:
    """Outcome of applying a private checkpoint to the source workspace."""

    status: PromotionStatus
    checkpoint: str
    patch_path: Path
    reason: str = ""
