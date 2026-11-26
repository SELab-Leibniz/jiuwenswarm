"""Persistent gap and repair progress for repeated Reviewer scheduling.

The guard never grants acceptance or overrides Core. It only answers whether
another reviewed Worker round can still add evidence. Gap identities come from
a structured ``<pact-gaps>`` block when the Reviewer supplies one; otherwise the
review's existing text/reason identity is used. Only hashes and gap IDs are
persisted, never Reviewer prose.
"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .round_progress import observe_gap_change, observe_verification
from .review_governance import (
    DIAGNOSIS_PAUSE_ROUNDS,
    PROGRESSION_STOP_ROUNDS,
    extract_gap_ids,
    extract_text_gap_ids,
    failed_check_count,
    has_structured_gap_block,
    structured_review_gaps,
    verification_signature,
)

INVALID_REVIEW_REASONS = {
    "reviewer_mutation",
    "budget_exhausted",
    "reviewer_output_missing",
    "reviewer_protocol_error",
}
PROTOCOL_REASON = "reviewer_protocol_error"
_SCHEMA = "long-horizon/review-progress/v2"


@dataclass(slots=True)
class ReviewProgressGuard:
    review_interval: int = 1
    path: Path | None = None
    _reviews: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=6))
    _last_completed_workers: int = -1
    _seen_reviews: set[str] = field(default_factory=set)
    _seen_gaps: set[str] = field(default_factory=set)
    _last_gaps: list[str] = field(default_factory=list)
    _last_failed: int = 0
    _verification_history: dict[str, Any] = field(default_factory=dict)
    _last_inputs: str | None = None
    _epoch: int = 0
    _last_tree: str | None = None
    _no_progress_rounds: int = 0
    _same_gap_rounds: int = 0
    _diagnosis_streak: int = 0
    _protocol_streak: int = 0
    _diagnosis_paused: bool = False
    _stop_reason: str | None = None
    _events: list[dict[str, Any]] = field(default_factory=list)
    _initialized: bool = False

    def __post_init__(self) -> None:
        if self.path is not None:
            self._load()

    @property
    def stop_reason(self) -> str | None:
        return self._stop_reason

    @property
    def diagnosis_paused(self) -> bool:
        return self._diagnosis_paused

    def recover(self, epoch: int) -> None:
        """Reopen review after a new, Work-bound verification or gap-closure receipt."""
        if epoch <= self._epoch:
            return
        self._epoch = epoch
        self._stop_reason = None
        self._diagnosis_paused = False
        self._protocol_streak = self._diagnosis_streak = 0
        self._same_gap_rounds = self._no_progress_rounds = 0
        self._persist_if_changed(False)

    def observe(self, state: Any, completed_workers: int, *, purpose: str | None = None) -> bool:
        review = state.review
        if (
            review is None
            or review.candidate != state.candidate
            or state.acceptance is not None
            or state.delivery is not None
            or completed_workers < 1
            or completed_workers <= self._last_completed_workers
        ):
            return self._persist_if_changed(False)
        review_id = str(review.artifact)
        if review_id in self._seen_reviews:
            return self._persist_if_changed(False)
        if not review.feedback.strip():
            return self._discard_review(review_id, completed_workers)
        reasons = set(review.reasons)
        if reasons & INVALID_REVIEW_REASONS:
            return self._observe_invalid(review_id, completed_workers, reasons)
        verification = state.verification
        if verification is None and (state.policy.verify_command or state.checks):
            return self._persist_if_changed(False)
        if verification is not None and verification.candidate != state.candidate:
            return self._persist_if_changed(False)
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
        structured_gaps = has_structured_gap_block(review_text)
        gaps = extract_gap_ids(review_text) if structured_gaps else extract_text_gap_ids(review_text)
        failed = failed_check_count(state)
        verification = verification_signature(state)
        check_improved = observe_verification(self._verification_history, state)
        inputs = (
            state.contract_revision,
            json.dumps(state.contract, sort_keys=True, default=str),
            tuple(state.constraints),
            json.dumps(state.checks, sort_keys=True, default=str),
            tuple(state.unresolved_assumptions),
            tuple(state.user_decisions),
        )
        input_digest = hashlib.sha256(json.dumps(inputs, sort_keys=True, default=str).encode()).hexdigest()
        inputs_changed = bool(self._last_inputs) and input_digest != self._last_inputs
        self._last_inputs = input_digest
        tree_changed = self._last_tree is not None and state.candidate.tree != self._last_tree
        new_gaps = [gap for gap in gaps if gap not in self._seen_gaps]
        closed_gaps = [gap for gap in self._last_gaps if gap not in gaps]
        # Free-form feedback and new inputs can distinguish a fresh review.
        # Only comparable check results or a strict contraction of structured gaps earn a reset.
        narrowed = observe_gap_change(self._verification_history, state.contract_revision, structured_review_gaps(review.artifact))
        advanced = check_improved or narrowed
        changed = bool(new_gaps or closed_gaps or inputs_changed or tree_changed)
        self._seen_gaps.update(gaps)
        if advanced:
            self._stop_reason = None
            self._diagnosis_paused = False
            self._diagnosis_streak = self._no_progress_rounds = self._same_gap_rounds = 0
        elif gaps and set(gaps) == set(self._last_gaps):
            self._same_gap_rounds += 1
        elif gaps:
            self._same_gap_rounds = 1
        else:
            self._same_gap_rounds = 0
        if not advanced and not changed:
            self._no_progress_rounds += 1
        if purpose == "diagnostic":
            if not advanced and not changed:
                self._diagnosis_streak += 1
            if self._diagnosis_streak >= DIAGNOSIS_PAUSE_ROUNDS:
                self._diagnosis_paused = True
        else:
            self._diagnosis_streak = 0
        self._protocol_streak = 0
        if self._stop_reason == "review_protocol_stalled":
            self._stop_reason = None
        self._last_gaps = list(gaps)
        self._last_failed = failed
        self._last_tree = state.candidate.tree
        self._last_completed_workers = completed_workers
        self._seen_reviews.add(review_id)
        signature = hashlib.sha256(
            json.dumps(
                {
                    "tree": state.candidate.tree,
                    "contract_revision": state.contract_revision,
                    "contract": state.contract,
                    "constraints": state.constraints,
                    "checks": state.checks,
                    "verification": verification,
                    "review_reasons": sorted(review.reasons),
                    "feedback": " ".join(review.feedback.split()),
                    "questions": state.unresolved_assumptions,
                    "decisions": state.user_decisions,
                },
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode()
        ).hexdigest()
        self._reviews.append(
            {
                "review": review_id,
                "candidate_checkpoint": state.candidate.checkpoint,
                "input_sha256": signature,
                "completed_workers": completed_workers,
                "gaps": list(gaps),
                "new_gaps": list(new_gaps),
                "closed_gaps": list(closed_gaps),
                "check_improved": bool(check_improved),
                "failed_checks": failed,
            }
        )
        self._events.append(
            {
                "review": review_id,
                "completed_workers": completed_workers,
                "gaps": list(gaps),
                "new_gaps": list(new_gaps),
                "closed_gaps": list(closed_gaps),
                "check_improved": bool(check_improved),
                "failed_checks": failed,
                "no_progress_rounds": self._no_progress_rounds,
                "same_gap_rounds": self._same_gap_rounds,
                "diagnosis_streak": self._diagnosis_streak,
            }
        )
        self._events = self._events[-32:]
        if purpose == "diagnostic":
            # A repeated diagnosis is paused, not converted into a trial stop:
            # the caller should spend the remaining time on an executable
            # repair. Protocol stalls are still handled above/invalid reviews.
            return self._persist_if_changed(False)
        if self._same_gap_rounds >= PROGRESSION_STOP_ROUNDS and not advanced and not changed:
            self._stop_reason = "review_no_progress_same_gap"
        elif self._no_progress_rounds >= PROGRESSION_STOP_ROUNDS:
            self._stop_reason = "review_no_progress"
        return self._persist_if_changed(bool(self._stop_reason))

    def _observe_invalid(self, review_id: str, completed_workers: int, reasons: set[str]) -> bool:
        self._seen_reviews.add(review_id)
        self._last_completed_workers = completed_workers
        self._reviews.clear()
        self._no_progress_rounds = 0
        self._same_gap_rounds = 0
        if PROTOCOL_REASON in reasons:
            # The attached format repair is already inside one admission; two
            # consecutive admissions that still fail the protocol are a distinct
            # stall, never a business rejection.
            self._protocol_streak += 1
            if self._protocol_streak >= 2:
                self._stop_reason = "review_protocol_stalled"
                return self._persist_if_changed(True)
        else:
            self._protocol_streak = 0
        return self._persist_if_changed(False)

    def _discard_review(self, review_id: str, completed_workers: int) -> bool:
        self._seen_reviews.add(review_id)
        self._last_completed_workers = completed_workers
        self._reviews.clear()
        self._no_progress_rounds = 0
        self._same_gap_rounds = 0
        return self._persist_if_changed(False)

    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(value, dict) or value.get("schema") != _SCHEMA:
            return
        counters = {
            "_no_progress_rounds": "no_progress_rounds",
            "_same_gap_rounds": "same_gap_rounds",
            "_diagnosis_streak": "diagnosis_streak",
            "_protocol_streak": "protocol_streak",
        }
        for attribute, key in counters.items():
            # Persisted keys are the public, non-underscored names. Accept the
            # old underscored spelling as a migration fallback.
            item = value.get(key, value.get(attribute, 0))
            if isinstance(item, int) and not isinstance(item, bool) and item >= 0:
                setattr(self, attribute, item)
        self._diagnosis_paused = bool(value.get("diagnosis_paused"))
        stop = value.get("stop_reason")
        self._stop_reason = stop if isinstance(stop, str) else None
        gaps = value.get("seen_gaps")
        if isinstance(gaps, list) and all(isinstance(item, str) for item in gaps):
            self._seen_gaps = set(gaps)
        last = value.get("last_gaps")
        if isinstance(last, list) and all(isinstance(item, str) for item in last):
            self._last_gaps = list(last)
        failed = value.get("last_failed")
        if isinstance(failed, int) and not isinstance(failed, bool) and failed >= 0:
            self._last_failed = failed
        last_tree = value.get("last_tree")
        if isinstance(last_tree, str):
            self._last_tree = last_tree
        events = value.get("events")
        if isinstance(events, list):
            self._events = [item for item in events if isinstance(item, dict)][-32:]
        last_workers = value.get("last_completed_workers")
        if isinstance(last_workers, int) and not isinstance(last_workers, bool):
            self._last_completed_workers = last_workers
        reviews = value.get("reviewed_inputs")
        if isinstance(reviews, list):
            self._reviews = deque(
                (item for item in reviews if isinstance(item, dict)),
                maxlen=6,
            )
        seen = value.get("seen_reviews")
        if isinstance(seen, list):
            self._seen_reviews = {item for item in seen if isinstance(item, str)}
        self._epoch = int(value.get("progress_epoch", 0))
        self._last_inputs = value.get("last_input_digest")
        history = value.get("verification_history")
        if isinstance(history, dict):
            self._verification_history = history
        self._initialized = True

    def _snapshot(self) -> dict[str, Any]:
        return {
            "schema": _SCHEMA,
            "progress_epoch": self._epoch,
            "last_input_digest": self._last_inputs,
            "verification_history": self._verification_history,
            "stop_reason": self._stop_reason,
            "diagnosis_paused": self._diagnosis_paused,
            "no_progress_rounds": self._no_progress_rounds,
            "same_gap_rounds": self._same_gap_rounds,
            "diagnosis_streak": self._diagnosis_streak,
            "protocol_streak": self._protocol_streak,
            "seen_gaps": sorted(self._seen_gaps),
            "last_gaps": list(self._last_gaps),
            "last_failed": self._last_failed,
            "last_tree": self._last_tree,
            "last_completed_workers": self._last_completed_workers,
            "reviewed_inputs": list(self._reviews),
            "seen_reviews": sorted(self._seen_reviews),
            "events": list(self._events),
        }

    def _persist_if_changed(self, result: bool) -> bool:
        if self.path is not None:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.path.with_name(self.path.name + ".tmp")
                temporary.write_text(
                    json.dumps(self._snapshot(), indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                temporary.replace(self.path)
            except OSError:
                pass
        return result

    def evidence(self) -> dict[str, Any]:
        return {
            "schema": _SCHEMA,
            "policy": (
                "pause diagnosis after two evidence-free rounds and stop after three ineffective rejections; "
                "a verified check improvement or a strictly smaller set of reviewed gaps restores the counters"
            ),
            "review_interval": self.review_interval,
            "reviewed_inputs": list(self._reviews),
            "seen_gaps": sorted(self._seen_gaps),
            "last_gaps": list(self._last_gaps),
            "last_tree": self._last_tree,
            "no_progress_rounds": self._no_progress_rounds,
            "same_gap_rounds": self._same_gap_rounds,
            "diagnosis_streak": self._diagnosis_streak,
            "diagnosis_paused": self._diagnosis_paused,
            "protocol_streak": self._protocol_streak,
            "stop_reason": self._stop_reason,
            "events": list(self._events),
            "acceptance_waived": False,
        }
