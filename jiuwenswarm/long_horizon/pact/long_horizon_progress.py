"""Stop repeat cycles only after completed Workers with unchanged reviewed inputs.

Adapted from the Chrys long-horizon progress guard. Core remains authoritative
for acceptance, verification and export; this caller only stops scheduling.
"""
from __future__ import annotations

import hashlib
import json
from collections import deque
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ReviewProgressGuard:
    _reviews: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=6))
    _last_completed_workers: int = -1

    def observe(self, state: Any, completed_workers: int) -> bool:
        review = state.review
        if (review is None or review.candidate.tree != state.candidate.tree
                or state.acceptance is not None or state.delivery is not None
                or completed_workers < 1 or completed_workers <= self._last_completed_workers):
            return False
        verification = state.verification
        if verification is None and (state.policy.verify_command or state.checks):
            return False
        if verification is not None and verification.candidate != state.candidate:
            return False
        signature = hashlib.sha256(json.dumps({
            "tree": state.candidate.tree,
            "contract_revision": state.contract_revision, "contract": state.contract,
            "constraints": state.constraints, "checks": state.checks,
            "verification": None if verification is None else verification.status,
            "check_results": [] if verification is None else [
                {key: row.get(key) for key in ("name", "version", "status", "exit_code")}
                for row in verification.check_results],
            "review_reasons": sorted(review.reasons),
            "feedback": " ".join(review.feedback.split()),
            "questions": state.unresolved_assumptions, "decisions": state.user_decisions,
        }, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
        self._last_completed_workers = completed_workers
        self._reviews.append({"review": str(review.artifact), "input_sha256": signature,
                              "completed_workers": completed_workers})
        recent = list(self._reviews)[-3:]
        return len(recent) == 3 and all(row['input_sha256'] == signature for row in recent)

    def evidence(self) -> dict[str, Any]:
        return {"schema": "jiuwenswarm-pact/no-progress-stop/v1",
                "policy": "three consecutive identical reviewed inputs in completed Worker rounds",
                "reviewed_inputs": list(self._reviews), "acceptance_waived": False}
