# Copyright (c) 2026 Chrys. All rights reserved.

"""Time-and-progress review governance shared by both Reviewer backends.

This module deliberately contains no Core authority. It only decides whether the
controller may open another Worker/check round or another Reviewer admission,
and it derives stable gap identities from structured review evidence so the
controller can distinguish "no further useful review" from "the old quota ran
out".
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

DEFAULT_ACCEPTANCE_RESERVE_SECONDS = 10 * 60.0
ACCEPTANCE_RESERVE_RATIO = 0.05
ACCEPTANCE_DURATION_MULTIPLIER = 1.25
DEFAULT_WORKER_SECONDS = 5 * 60.0
DEFAULT_PUBLIC_CHECK_SECONDS = 2 * 60.0
RECENT_SAMPLES = 3
PROGRESSION_STOP_ROUNDS = 3
DIAGNOSIS_PAUSE_ROUNDS = 2

_GAP_BLOCK = re.compile(
    r"<pact-gaps\b[^>]*>\s*(.*?)\s*</pact-gaps\s*>",
    re.DOTALL | re.IGNORECASE,
)
_LIST_PREFIX = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)")
_FALLBACK_GAP_LINE = re.compile(r"(?m)^\s*(?:#{1,6}\s+|[-*+]\s+|\d+[.)]\s+)(.+?)\s*$")
_WHITESPACE = re.compile(r"\s+")
_TRAILING_PUNCTUATION = re.compile(r"[\s.;:,!?，。；：！？]+$")


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _call_duration(call: Mapping[str, Any]) -> float | None:
    return _finite(call.get("observed_duration_seconds"))


def recent_acceptance_durations(snapshot: Mapping[str, Any]) -> list[float]:
    """Return total durations of the last contiguous acceptance admissions.

    Core's attached verdict-format repair is a second Reviewer call with the
    same ``acceptance`` purpose. Summing a contiguous run charges that repair to
    the admission it belongs to instead of letting a 30-second repair hide the
    real cost of the next final decision.
    """
    calls = snapshot.get("calls")
    if not isinstance(calls, list):
        return []
    groups: list[float] = []
    current: float | None = None
    for call in calls:
        if not isinstance(call, dict):
            continue
        if call.get("purpose") != "acceptance":
            if current is not None:
                groups.append(current)
                current = None
            continue
        duration = _call_duration(call)
        if call.get("status") != "completed" or duration is None:
            continue
        current = (current or 0.0) + max(0.0, duration)
    if current is not None:
        groups.append(current)
    return groups[-RECENT_SAMPLES:]


def worker_check_estimate(
    snapshot: Mapping[str, Any],
    *,
    check_durations: Sequence[float] = (),
) -> float:
    """Estimate the next Worker plus public check from completed history.

    The latest three completed implementation calls are averaged, not maximised,
    so one pathological timeout does not permanently freeze all repair. With no
    usable Worker history the estimate is deliberately conservative. Public
    check durations are used when the controller recorded them; otherwise a
    conservative fixed allowance is added.
    """
    calls = snapshot.get("calls")
    worker_durations: list[float] = []
    if isinstance(calls, list):
        for call in calls:
            if not isinstance(call, dict):
                continue
            if call.get("role") != "worker" or call.get("purpose") != "implementation":
                continue
            if call.get("status") != "completed":
                continue
            duration = _call_duration(call)
            if duration is not None:
                worker_durations.append(max(0.0, duration))
    recent = worker_durations[-RECENT_SAMPLES:]
    worker = sum(recent) / len(recent) if recent else DEFAULT_WORKER_SECONDS
    checks = [value for value in (_finite(item) for item in check_durations) if value is not None]
    check_recent = checks[-RECENT_SAMPLES:]
    check = sum(check_recent) / len(check_recent) if check_recent else DEFAULT_PUBLIC_CHECK_SECONDS
    return max(0.0, worker) + max(0.0, check)


@dataclass(frozen=True, slots=True)
class TimeAdmission:
    now: float
    soft_remaining: float
    original_remaining: float
    total_seconds: float
    acceptance_reserve: float
    worker_estimate: float

    @property
    def required_before_worker(self) -> float:
        return self.worker_estimate + self.acceptance_reserve

    @property
    def worker_allowed(self) -> bool:
        return self.soft_remaining > self.required_before_worker

    @property
    def diagnosis_allowed(self) -> bool:
        return self.soft_remaining > self.required_before_worker

    @property
    def acceptance_allowed(self) -> bool:
        return self.soft_remaining >= self.acceptance_reserve

    def evidence(self) -> dict[str, Any]:
        return {
            "schema": "long-horizon/time-admission/v1",
            "now_unix": round(self.now, 3),
            "original_remaining_seconds": round(self.original_remaining, 3),
            "soft_remaining_seconds": round(self.soft_remaining, 3),
            "total_seconds": round(self.total_seconds, 3),
            "acceptance_reserve_seconds": round(self.acceptance_reserve, 3),
            "worker_estimate_seconds": round(self.worker_estimate, 3),
            "required_before_worker_seconds": round(self.required_before_worker, 3),
            "worker_allowed": self.worker_allowed,
            "diagnosis_allowed": self.diagnosis_allowed,
            "acceptance_allowed": self.acceptance_allowed,
        }


def acceptance_reserve(
    *,
    total_seconds: float,
    recent_acceptance: Iterable[float],
    full_acceptance: Iterable[float] = (),
) -> float:
    """Use the full-operation reserve after three comparable, separately sealed candidates."""
    full = [v for item in full_acceptance if (v := _finite(item)) is not None and v > 0]
    recent = [v for item in recent_acceptance if (v := _finite(item)) is not None and v > 0]
    observed = max([*recent, *full[-RECENT_SAMPLES:]], default=0.0) * ACCEPTANCE_DURATION_MULTIPLIER
    cold = ACCEPTANCE_RESERVE_RATIO * max(0.0, _finite(total_seconds) or 0.0)
    return max(DEFAULT_ACCEPTANCE_RESERVE_SECONDS, observed, cold if len(full) < RECENT_SAMPLES else 0.0)


def time_admission(
    *,
    now: float,
    soft_deadline: float,
    original_deadline: float,
    total_seconds: float,
    snapshot: Mapping[str, Any],
    check_durations: Sequence[float] = (),
    full_acceptance: Iterable[float] = (),
) -> TimeAdmission:
    """Compute S, W and A without deducting the finish reserve twice."""
    current = _finite(now) or 0.0
    soft = _finite(soft_deadline)
    original = _finite(original_deadline)
    soft_remaining = max(0.0, (soft if soft is not None else original if original is not None else current) - current)
    original_remaining = max(0.0, (original if original is not None else current) - current)
    return TimeAdmission(
        now=current,
        soft_remaining=soft_remaining,
        original_remaining=original_remaining,
        total_seconds=max(0.0, _finite(total_seconds) or 0.0),
        acceptance_reserve=acceptance_reserve(
            total_seconds=total_seconds,
            recent_acceptance=recent_acceptance_durations(snapshot),
            full_acceptance=full_acceptance,
        ),
        worker_estimate=worker_check_estimate(snapshot, check_durations=check_durations),
    )


def normalize_gap_id(text: str) -> str:
    """Return a stable fingerprint for one semantic gap description."""
    normalized = _LIST_PREFIX.sub("", str(text).strip())
    normalized = _WHITESPACE.sub(" ", normalized).strip()
    normalized = _TRAILING_PUNCTUATION.sub("", normalized).lower()
    if not normalized:
        return ""
    return "gap:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _gap_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        for key in ("text", "gap", "description", "signature", "reason", "id"):
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                return [item]
        return []
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            result.extend(_gap_values(item))
        return result
    return []


def extract_gap_ids(review_text: str) -> list[str]:
    """Extract stable gap IDs from an optional structured ``<pact-gaps>`` block.

    Reviewers that do not emit the block use :func:`extract_text_gap_ids` as a
    conservative fallback; no semantic gap is invented.
    """
    ids: list[str] = []
    for match in _GAP_BLOCK.finditer(review_text or ""):
        try:
            payload = json.loads(match.group(1))
        except (TypeError, ValueError):
            continue
        for description in _gap_values(payload):
            gap_id = normalize_gap_id(description)
            if gap_id and gap_id not in ids:
                ids.append(gap_id)
    return ids


def has_structured_gap_block(review_text: str) -> bool:
    """Return whether the Reviewer supplied the structured gap channel."""
    return bool(_GAP_BLOCK.search(review_text or ""))


def extract_text_gap_ids(review_text: str) -> list[str]:
    """Fingerprint list items and headings when no structured gap block exists.

    These are deliberately presentation-level identities, not inferred
    semantics. They let the progress guard notice that the written gap list
    changed without treating arbitrary prose rewording as progress.
    """
    ids: list[str] = []
    for match in _FALLBACK_GAP_LINE.finditer(review_text or ""):
        gap_id = normalize_gap_id(match.group(1))
        if gap_id and gap_id not in ids:
            ids.append(gap_id)
    return ids


def plan_challenge_gap_ids(decision: Any) -> list[str]:
    """Use Core's structured plan-challenge signature when it is present."""
    challenge = getattr(decision, "plan_challenge", None)
    signature = getattr(challenge, "gap_signature", None) if challenge is not None else None
    if not isinstance(signature, str) or not signature.strip():
        return []
    gap_id = normalize_gap_id(signature)
    return [gap_id] if gap_id else []


def failed_check_count(snapshot: Any) -> int:
    """Count currently failing public check rows for improvement comparison."""
    verification = getattr(snapshot, "verification", None)
    rows = getattr(verification, "check_results", None) if verification is not None else None
    if not isinstance(rows, (list, tuple)):
        return 0
    count = 0
    for row in rows:
        status = row.get("status") if isinstance(row, dict) else getattr(row, "status", None)
        if status not in (None, "passed", "skipped", "not_run"):
            count += 1
    return count


def verification_signature(snapshot: Any) -> tuple[Any, ...]:
    verification = getattr(snapshot, "verification", None)
    if verification is None:
        return ("missing",)
    rows = getattr(verification, "check_results", None) or ()
    normalized = []
    for row in rows:
        if isinstance(row, dict):
            normalized.append((row.get("name"), row.get("version"), row.get("status"), row.get("exit_code")))
        else:
            normalized.append(
                (
                    getattr(row, "name", None),
                    getattr(row, "version", None),
                    getattr(row, "status", None),
                    getattr(row, "exit_code", None),
                )
            )
    return (getattr(verification, "status", None), tuple(normalized))


__all__ = [
    "ACCEPTANCE_DURATION_MULTIPLIER",
    "ACCEPTANCE_RESERVE_RATIO",
    "DEFAULT_ACCEPTANCE_RESERVE_SECONDS",
    "DEFAULT_PUBLIC_CHECK_SECONDS",
    "DEFAULT_WORKER_SECONDS",
    "DIAGNOSIS_PAUSE_ROUNDS",
    "PROGRESSION_STOP_ROUNDS",
    "TimeAdmission",
    "acceptance_reserve",
    "extract_gap_ids",
    "extract_text_gap_ids",
    "failed_check_count",
    "has_structured_gap_block",
    "normalize_gap_id",
    "plan_challenge_gap_ids",
    "recent_acceptance_durations",
    "time_admission",
    "verification_signature",
    "worker_check_estimate",
]
