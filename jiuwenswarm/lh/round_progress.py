"""Track consecutive ineffective implementation rounds against the current Work."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .review_governance import structured_review_gaps


def observe_verification(window: dict[str, Any], state: Any) -> bool:
    verified = state.verification
    if verified is None or verified.candidate != state.candidate or verified.status not in {"passed", "failed"}:
        return False
    # Compare like checks. Removing a check or replacing its definition
    # must not look like closing a failure.
    rows = {
        json.dumps([row["name"], row.get("version")]): row.get("status")
        for row in verified.check_results if row.get("name")
    }
    previous = window.get("checks", {})
    lost = any(rows.get(key) != "passed" for key, status in previous.items() if status == "passed")
    definitions = json.dumps(state.checks, sort_keys=True, default=str, separators=(",", ":"))
    check_scope = [state.contract_revision, getattr(verified, "command", None), hashlib.sha256(definitions.encode()).hexdigest()]
    comparable = window.get("check_scope") == check_scope and previous.keys() <= rows.keys()
    improved = any(status == "passed" and previous.get(key) != "passed" for key, status in rows.items())
    advanced = comparable and not lost and (
        improved or (verified.status == "passed" and window.get("verification") == "failed")
    )
    window.update(checks=rows, verification=verified.status, check_scope=check_scope)
    return advanced


def observe_gap_change(window: dict[str, Any], contract_revision: int, gaps: list[str] | None) -> bool:
    """Compare two complete, structured reviews of the same contract."""
    if gaps is None:
        return False
    previous = window.get("gaps")
    narrowed = previous is not None and window.get("gap_contract") == contract_revision and set(gaps) < set(previous)
    window.update(gaps=gaps, gap_contract=contract_revision)
    return narrowed


def observe_round(
    window: dict[str, Any], state: Any, workers: int | None = None, *,
    failed: bool = False, coverage_gaps: list[str] | None = None,
) -> bool:
    """Update the durable window and return whether this observation earned a reset.

    Calls, verification and review can arrive separately for one sealed candidate.
    A repeated poll, stale revision or a new tree alone supplies no new evidence.
    """
    revision = state.revision
    last_revision = window.get("revision", -1)
    if revision < last_revision:
        return False
    review = state.review
    new_coverage = (
        coverage_gaps is not None and review is not None and review.candidate == state.candidate
        and str(review.artifact) != window.get("coverage_receipt")
    )
    if revision == last_revision and not new_coverage and (workers is None or workers <= window.get("worker", 0)):
        return False
    window["revision"] = revision
    checkpoint = str(state.candidate.checkpoint)
    verified = state.verification
    current_check = verified is not None and verified.candidate == state.candidate
    blocked = failed
    advanced = False
    if current_check and verified.status in {"passed", "failed"}:
        advanced = observe_verification(window, state)
        blocked |= verified.status == "failed"

    if review is not None and review.candidate == state.candidate and review.feedback.strip() and not (
        set(review.reasons) & {"reviewer_mutation", "budget_exhausted", "reviewer_output_missing", "reviewer_protocol_error"}
    ):
        blocked |= not review.complete
        receipt = str(review.artifact)
        if receipt != window.get("review"):
            advanced |= (
                review.complete and window.get("review_complete") is False
                and window.get("review_contract") == state.contract_revision
            )
            window["review_contract"] = state.contract_revision
            window["review_complete"] = review.complete
            advanced |= observe_gap_change(window, state.contract_revision, structured_review_gaps(review.artifact))
            window["review"] = receipt
        if new_coverage:
            advanced |= observe_gap_change(window.setdefault("coverage", {}), state.contract_revision, coverage_gaps)
            window["coverage_receipt"] = str(review.artifact)

    if advanced:
        window["epoch"] = window.get("epoch", 0) + 1
        window["stalled"] = 0
        window["pending_reset"] = checkpoint if workers is None else None
    if workers is not None and workers > window.get("worker", 0):
        tree = str(state.candidate.tree)
        if advanced or (window.get("pending_reset") == checkpoint and not failed):
            window["pending_reset"] = None
        elif blocked or (current_check and verified.status in {"passed", "failed"} and tree == window.get("last_worker_tree")):
            window["stalled"] = window.get("stalled", 0) + 1
        elif not current_check:
            # Verification or a review for the new Worker can arrive on a later poll.
            return advanced
        window.update(worker=workers, last_worker_tree=tree)
    return advanced
