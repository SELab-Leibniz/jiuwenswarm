"""Stop repeat cycles only after completed Workers with unchanged reviewed inputs.

Adapted from the Chrys long-horizon progress guard. Core remains authoritative
for acceptance, verification and export; this caller only stops scheduling.
"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

INVALID_REVIEW_REASONS = {
    "reviewer_mutation",
    "budget_exhausted",
    "reviewer_output_missing",
    "reviewer_protocol_error",
}


@dataclass(slots=True)
class ReviewProgressGuard:
    review_interval: int = 1
    _reviews: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=6))
    _last_completed_workers: int = -1
    _seen_reviews: set[str] = field(default_factory=set)

    def observe(self, state: Any, completed_workers: int) -> bool:
        review = state.review
        if (
            review is None
            or review.candidate != state.candidate
            or state.acceptance is not None
            or state.delivery is not None
            or completed_workers < 1
            or completed_workers <= self._last_completed_workers
        ):
            return False
        review_id = str(review.artifact)
        if review_id in self._seen_reviews:
            return False
        if not review.feedback.strip() or INVALID_REVIEW_REASONS.intersection(review.reasons):
            return self._discard_review(review_id, completed_workers)
        verification = state.verification
        if verification is None and (state.policy.verify_command or state.checks):
            return False
        if verification is not None and verification.candidate != state.candidate:
            return False
        # Core's public summary can contain only reason codes. Bind the recorded
        # Review text so different feedback is not collapsed into the same input.
        try:
            review_text = Path(review.artifact).read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return self._discard_review(review_id, completed_workers)
        if not review_text.strip():
            return self._discard_review(review_id, completed_workers)
        # Polls while a Worker/Reviewer is in flight do not form a round. A
        # skipped completed Worker breaks the streak when a fresh Review arrives.
        if (
            self._last_completed_workers >= 0
            and completed_workers - self._last_completed_workers > self.review_interval
        ):
            self._reviews.clear()
        signature = hashlib.sha256(
            json.dumps(
                {
                    "tree": state.candidate.tree,
                    "contract_revision": state.contract_revision,
                    "contract": state.contract,
                    "constraints": state.constraints,
                    "checks": state.checks,
                    "verification": None if verification is None else verification.status,
                    "check_results": []
                    if verification is None
                    else [
                        {key: row.get(key) for key in ("name", "version", "status", "exit_code")}
                        for row in verification.check_results
                    ],
                    "review_reasons": sorted(review.reasons),
                    "feedback": " ".join(review.feedback.split()),
                    "review_text": " ".join(review_text.split()),
                    "questions": state.unresolved_assumptions,
                    "decisions": state.user_decisions,
                },
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode()
        ).hexdigest()
        self._last_completed_workers = completed_workers
        self._seen_reviews.add(review_id)
        self._reviews.append(
            {
                "review": review_id,
                "candidate_checkpoint": state.candidate.checkpoint,
                "input_sha256": signature,
                "completed_workers": completed_workers,
            }
        )
        recent = list(self._reviews)[-3:]
        return len(recent) == 3 and all(row["input_sha256"] == signature for row in recent)

    def _discard_review(self, review_id: str, completed_workers: int) -> bool:
        self._seen_reviews.add(review_id)
        self._last_completed_workers = completed_workers
        self._reviews.clear()
        return False

    def evidence(self) -> dict[str, Any]:
        return {
            "schema": "long-horizon/no-progress-stop/v1",
            "policy": "three consecutive reviewed Worker rounds with distinct valid current Reviews and identical inputs",
            "review_interval": self.review_interval,
            "reviewed_inputs": list(self._reviews),
            "acceptance_waived": False,
        }
