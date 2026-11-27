"""Strict reviewer decision transport parsing and normalization."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal, cast

from ._role_output import RoleOutputError, decode_role_object
from .gate import parse_review_marker
from .schemas import (
    PlanChallenge,
    PlanChallengeCaptureStatus,
    PlanChallengeRecommendedAction,
    ReviewDecisionCapture,
    ReviewDecisionCaptureStatus,
    ReviewMarker,
    TurnResult,
)

DecisionStatus = Literal["valid", "unavailable", "protocol_error"]
DecisionSource = Literal[
    "structured",
    "structured_repair",
    "legacy_marker",
    "controller_synthesized",
]
PlanChallengeSource = Literal["structured", "structured_repair", "none"]


@dataclass(frozen=True, slots=True)
class ResolvedReviewDecision:
    """One normalized reviewer proposal plus its transport provenance."""

    verdict_status: DecisionStatus
    verdict: ReviewMarker | None
    verdict_source: DecisionSource
    initial_verdict_capture_status: str
    repair_attempted: bool
    repair_verdict_capture_status: str
    plan_challenge_status: DecisionStatus
    plan_challenge: PlanChallenge | None
    plan_challenge_source: PlanChallengeSource
    initial_plan_challenge_capture_status: str
    repair_plan_challenge_capture_status: str
    terminal_line: str = ""
    initial_verdict_error: str = ""
    repair_verdict_error: str = ""
    plan_challenge_error: str = ""


def _parse_plan_challenge(
    payload: object,
) -> tuple[PlanChallengeCaptureStatus, PlanChallenge | None, str]:
    if not isinstance(payload, dict):
        return "malformed", None, "plan_challenge must be a JSON object"
    expected_fields = {"reason", "gap_signature", "recommended_action"}
    if set(payload) != expected_fields:
        return (
            "malformed",
            None,
            "plan_challenge must contain exactly reason, gap_signature, and "
            "recommended_action",
        )
    reason = payload["reason"]
    gap_signature = payload["gap_signature"]
    recommended_action = payload["recommended_action"]
    if not isinstance(reason, str) or not reason.strip():
        return "malformed", None, "plan_challenge.reason must be a non-empty string"
    if not isinstance(gap_signature, str) or not gap_signature.strip():
        return (
            "malformed",
            None,
            "plan_challenge.gap_signature must be a non-empty string",
        )
    allowed_actions = {"replan", "retry", "request_user", "block"}
    if (
        not isinstance(recommended_action, str)
        or recommended_action not in allowed_actions
    ):
        return (
            "malformed",
            None,
            "plan_challenge.recommended_action must be replan, retry, request_user, "
            "or block",
        )
    return (
        "valid",
        PlanChallenge(
            reason=reason.strip(),
            gap_signature=gap_signature.strip(),
            recommended_action=cast(
                PlanChallengeRecommendedAction,
                recommended_action,
            ),
        ),
        "",
    )


def parse_review_decision(raw_text: str, *, existed: bool) -> ReviewDecisionCapture:
    """Parse verdict and optional challenge without coupling their validity."""
    if not existed:
        return ReviewDecisionCapture(
            verdict_status="missing",
            plan_challenge_status="unavailable",
        )
    try:
        payload = decode_role_object(raw_text)
    except RoleOutputError as exc:
        return ReviewDecisionCapture(
            verdict_status="malformed",
            plan_challenge_status="unavailable",
            raw_text=raw_text,
            error=f"invalid JSON: {exc}",
        )
    if not isinstance(payload, dict):
        return ReviewDecisionCapture(
            verdict_status="malformed",
            plan_challenge_status="unavailable",
            raw_text=raw_text,
            error="decision must be a JSON object",
        )

    if payload.get("plan_challenge") is not None:
        challenge_status, challenge, challenge_error = _parse_plan_challenge(
            payload["plan_challenge"]
        )
    else:
        challenge_status, challenge, challenge_error = "absent", None, ""
    # Explanation stays in raw_text, never in the canonical verdict fields.
    unknown_fields = set(payload).difference({"verdict", "plan_challenge", "reason"})
    verdict = payload.get("verdict")
    verdict_error = ""
    verdict_status: ReviewDecisionCaptureStatus = "valid"
    if unknown_fields:
        verdict_status = "malformed"
        verdict_error = f"decision contains unsupported field(s): {sorted(unknown_fields)}"
        verdict = None
    elif payload.get("reason") is not None and not isinstance(payload["reason"], str):
        verdict_status = "malformed"
        verdict_error = "reason must be a diagnostic string or null"
        verdict = None
    elif not isinstance(verdict, str) or verdict not in {
        "complete",
        "continue",
        "failed",
    }:
        verdict_status = "malformed"
        verdict_error = "verdict must be complete, continue, or failed"
        verdict = None
    return ReviewDecisionCapture(
        verdict_status=verdict_status,
        verdict=cast(ReviewMarker | None, verdict),
        plan_challenge_status=challenge_status,
        plan_challenge=challenge,
        raw_text=raw_text,
        error=verdict_error,
        plan_challenge_error=challenge_error,
    )


def needs_decision_repair(turn: TurnResult) -> bool:
    """Return whether a usable review has a broken structured decision."""
    return (
        turn.status == "completed"
        and bool(turn.final_text.strip())
        and (
            turn.review_decision.verdict_status in {"missing", "malformed"}
            or (
                turn.review_decision.verdict_status == "valid"
                and turn.review_decision.verdict is None
            )
        )
    )


def _resolve_plan_challenge(
    initial: ReviewDecisionCapture,
    repair: ReviewDecisionCapture | None,
) -> tuple[DecisionStatus, PlanChallenge | None, PlanChallengeSource, str]:
    if initial.plan_challenge_status == "valid":
        if initial.plan_challenge is None:
            return (
                "protocol_error",
                None,
                "none",
                "plan challenge capture claimed valid without a typed payload",
            )
        return "valid", initial.plan_challenge, "structured", ""
    if repair is not None and repair.plan_challenge_status == "valid":
        if repair.plan_challenge is None:
            return (
                "protocol_error",
                None,
                "none",
                "repaired plan challenge capture claimed valid without a typed payload",
            )
        return "valid", repair.plan_challenge, "structured_repair", ""
    if initial.plan_challenge_status == "malformed":
        return "protocol_error", None, "none", initial.plan_challenge_error
    if repair is not None and repair.plan_challenge_status == "malformed":
        return "protocol_error", None, "none", repair.plan_challenge_error
    return "unavailable", None, "none", ""


def _validate_resolution_consistency(
    resolution: ResolvedReviewDecision,
    *,
    review_text: str,
) -> ResolvedReviewDecision:
    """Reject repair/Challenge combinations that could change terminal semantics."""
    if (
        resolution.verdict_status == "valid"
        and resolution.plan_challenge_status == "valid"
        and resolution.verdict != "continue"
    ):
        message = "a valid plan challenge requires verdict continue"
        if resolution.verdict_source == "structured_repair":
            return replace(
                resolution,
                verdict_status="protocol_error",
                verdict=None,
                repair_verdict_error=message,
            )
        return replace(
            resolution,
            verdict_status="protocol_error",
            verdict=None,
            initial_verdict_error=message,
        )
    if (
        resolution.verdict_source == "structured_repair"
        and resolution.verdict in {"complete", "failed"}
    ):
        marker, terminal_line = parse_review_marker(review_text)
        if marker != resolution.verdict:
            return replace(
                resolution,
                verdict_status="protocol_error",
                verdict=None,
                repair_verdict_error=(
                    "format repair cannot introduce a terminal verdict absent from "
                    "the human-readable review"
                ),
            )
        return replace(resolution, terminal_line=terminal_line)
    return resolution


def resolve_review_decision(
    *,
    turn: TurnResult | None,
    review_text: str,
    repair_turn: TurnResult | None = None,
) -> ResolvedReviewDecision:
    """Normalize structured, repaired, legacy, and synthesized review outcomes."""
    if turn is None or turn.status != "completed" or not turn.final_text.strip():
        capture = turn.review_decision if turn is not None else None
        return ResolvedReviewDecision(
            verdict_status="unavailable",
            verdict="continue",
            verdict_source="controller_synthesized",
            initial_verdict_capture_status=(
                capture.verdict_status if capture is not None else "not_applicable"
            ),
            repair_attempted=False,
            repair_verdict_capture_status="not_attempted",
            plan_challenge_status="unavailable",
            plan_challenge=None,
            plan_challenge_source="none",
            initial_plan_challenge_capture_status=(
                capture.plan_challenge_status if capture is not None else "not_applicable"
            ),
            repair_plan_challenge_capture_status="not_attempted",
        )

    capture = turn.review_decision
    challenge_status, challenge, challenge_source, challenge_error = (
        _resolve_plan_challenge(capture, None)
    )
    if capture.verdict_status == "valid" and capture.verdict is not None:
        return _validate_resolution_consistency(
            ResolvedReviewDecision(
                verdict_status="valid",
                verdict=capture.verdict,
                verdict_source="structured",
                initial_verdict_capture_status=capture.verdict_status,
                repair_attempted=False,
                repair_verdict_capture_status="not_attempted",
                plan_challenge_status=challenge_status,
                plan_challenge=challenge,
                plan_challenge_source=challenge_source,
                initial_plan_challenge_capture_status=capture.plan_challenge_status,
                repair_plan_challenge_capture_status="not_attempted",
                plan_challenge_error=challenge_error,
            ),
            review_text=review_text,
        )

    if capture.verdict_status == "unsupported":
        marker, terminal_line = parse_review_marker(review_text)
        return ResolvedReviewDecision(
            verdict_status="valid",
            verdict=marker,
            verdict_source="legacy_marker",
            initial_verdict_capture_status=capture.verdict_status,
            repair_attempted=False,
            repair_verdict_capture_status="not_attempted",
            plan_challenge_status="unavailable",
            plan_challenge=None,
            plan_challenge_source="none",
            initial_plan_challenge_capture_status=capture.plan_challenge_status,
            repair_plan_challenge_capture_status="not_attempted",
            terminal_line=terminal_line,
        )

    if repair_turn is not None and repair_turn.status == "completed":
        repaired = repair_turn.review_decision
        if repaired.verdict_status == "valid" and repaired.verdict is not None:
            challenge_status, challenge, challenge_source, challenge_error = (
                _resolve_plan_challenge(capture, repaired)
            )
            return _validate_resolution_consistency(
                ResolvedReviewDecision(
                    verdict_status="valid",
                    verdict=repaired.verdict,
                    verdict_source="structured_repair",
                    initial_verdict_capture_status=capture.verdict_status,
                    repair_attempted=True,
                    repair_verdict_capture_status=repaired.verdict_status,
                    plan_challenge_status=challenge_status,
                    plan_challenge=challenge,
                    plan_challenge_source=challenge_source,
                    initial_plan_challenge_capture_status=capture.plan_challenge_status,
                    repair_plan_challenge_capture_status=repaired.plan_challenge_status,
                    initial_verdict_error=capture.error,
                    plan_challenge_error=challenge_error,
                ),
                review_text=review_text,
            )

    repair_capture = (
        repair_turn.review_decision
        if repair_turn is not None
        else ReviewDecisionCapture(
            verdict_status="missing",
            plan_challenge_status="unavailable",
        )
    )
    challenge_status, challenge, challenge_source, challenge_error = (
        _resolve_plan_challenge(capture, repair_capture if repair_turn is not None else None)
    )
    return ResolvedReviewDecision(
        verdict_status="protocol_error",
        verdict=None,
        verdict_source="structured_repair",
        initial_verdict_capture_status=capture.verdict_status,
        repair_attempted=repair_turn is not None,
        repair_verdict_capture_status=(
            repair_capture.verdict_status if repair_turn is not None else "not_attempted"
        ),
        plan_challenge_status=challenge_status,
        plan_challenge=challenge,
        plan_challenge_source=challenge_source,
        initial_plan_challenge_capture_status=capture.plan_challenge_status,
        repair_plan_challenge_capture_status=(
            repair_capture.plan_challenge_status
            if repair_turn is not None
            else "not_attempted"
        ),
        initial_verdict_error=capture.error,
        repair_verdict_error=(
            repair_capture.error
            or (
                f"repair turn status was {repair_turn.status}"
                if repair_turn is not None
                else ""
            )
        ),
        plan_challenge_error=challenge_error,
    )
