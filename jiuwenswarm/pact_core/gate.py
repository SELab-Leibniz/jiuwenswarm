"""The pure-function completion authority (PRD FR-6).

``evaluate_gate`` decides complete / failed / forced_continue from data
alone: no LLM calls, no subprocess, no filesystem access (AGENTS.md
invariant 1). Completion is never self-reported — a reviewer marker only
counts alongside verification and the structural invariants.
"""

from __future__ import annotations

from dataclasses import dataclass

from .schemas import (
    GateDecision,
    GateWaiver,
    ReviewMarker,
    VerificationStatus,
)

_COMPLETE_MARKER = "PACT_COMPLETE"
_FAILED_MARKER = "PACT_FAILED"


def parse_review_marker(review_text: str) -> tuple[ReviewMarker, str]:
    """Read the terminal marker from the last non-empty line.

    Anything other than an exact ``PACT_COMPLETE`` / ``PACT_FAILED`` line —
    including an empty review or a parse-resistant one — is an implicit
    continue; unparseable reviewer output must never hard-fail the loop.
    """
    lines = [line.strip() for line in review_text.splitlines() if line.strip()]
    if not lines:
        return "continue", ""
    terminal_line = lines[-1]
    if terminal_line == _COMPLETE_MARKER:
        return "complete", terminal_line
    if terminal_line == _FAILED_MARKER:
        return "failed", terminal_line
    return "continue", terminal_line


@dataclass(frozen=True, slots=True)
class GateInputs:
    """Everything the gate is allowed to know, provided as data."""

    review_text: str
    worker_summary: str
    worker_output_missing: bool
    verification_status: VerificationStatus
    allow_unverified: bool
    reviewer_mutated: bool
    namespace_violations: tuple[str, ...]
    immutable_goal_sha256_expected: str
    immutable_goal_sha256_current: str
    normalized_review_marker: ReviewMarker | None = None
    normalized_terminal_line: str = ""
    review_protocol_error: bool = False


def evaluate_gate(inputs: GateInputs) -> GateDecision:
    """Aggregate verification, reviewer marker, and structural invariants."""
    if inputs.normalized_review_marker is None:
        marker, terminal_line = parse_review_marker(inputs.review_text)
    else:
        marker = inputs.normalized_review_marker
        terminal_line = inputs.normalized_terminal_line or {
            "complete": _COMPLETE_MARKER,
            "failed": _FAILED_MARKER,
            "continue": "",
        }[marker]

    if inputs.review_protocol_error:
        return GateDecision(
            status="failed",
            marker="continue",
            blocked_by=["reviewer_protocol_error"],
            reasons=["reviewer decision remained missing or malformed after repair"],
        )

    if marker == "failed":
        return GateDecision(
            status="failed",
            marker=marker,
            terminal_line=terminal_line,
            reasons=["reviewer marked PACT_FAILED"],
        )

    blocked_by: list[str] = []
    reasons: list[str] = []
    waiver: GateWaiver | None = None

    if inputs.worker_output_missing:
        blocked_by.append("worker_output_missing")
        reasons.append("worker output was missing; summary was synthesized")
    if not inputs.worker_summary.strip():
        blocked_by.append("worker_summary_empty")
        reasons.append("worker summary is empty")

    verification_satisfied = inputs.verification_status == "passed"
    if inputs.verification_status == "unavailable" and inputs.allow_unverified:
        verification_satisfied = True
        waiver = GateWaiver(
            kind="allow_unverified",
            verification_status="unavailable",
            detail="completion permitted without task verification",
        )
    if not verification_satisfied:
        blocked_by.append(f"verification_{inputs.verification_status}")
        reasons.append(f"verification status is {inputs.verification_status}")

    if marker != "complete":
        blocked_by.append("reviewer_marker_not_complete")
        reasons.append(f"reviewer terminal line is {terminal_line or 'missing'}")
    if inputs.reviewer_mutated:
        blocked_by.append("reviewer_mutation")
        reasons.append("reviewer modified its worktree; changes were discarded")
    if inputs.namespace_violations:
        blocked_by.append("namespace_violation")
        reasons.append(
            "reserved namespace files in checkpoint: "
            + ", ".join(inputs.namespace_violations)
        )
    if inputs.immutable_goal_sha256_current != inputs.immutable_goal_sha256_expected:
        blocked_by.append("plan_hash_changed")
        reasons.append("immutable goal hash changed since initialization")

    if blocked_by:
        return GateDecision(
            status="forced_continue",
            marker=marker,
            terminal_line=terminal_line,
            blocked_by=blocked_by,
            reasons=reasons,
            waiver=waiver,
        )

    return GateDecision(
        status="complete",
        marker=marker,
        terminal_line=terminal_line,
        waiver=waiver,
    )
