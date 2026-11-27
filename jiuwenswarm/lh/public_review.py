# Copyright (c) 2026 Chrys. All rights reserved.

"""Typed progress for an all-or-nothing full public criterion review."""

from __future__ import annotations

import errno
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Any, Mapping

# Reserve time for the validated Goal Contract and Initial Plan after review.
PUBLIC_VERIFICATION_DEADLINE_MARGIN_SECONDS = 15.0


@dataclass(frozen=True)
class PublicTransportStop:
    """Correlated native failures carried on the original worker exception."""

    reason: str
    criterion_id: str
    parent: dict[str, Any]
    retry_failure: dict[str, Any] | None = None


@dataclass(frozen=True)
class PublicModelOutputStop:
    criterion_id: str
    failure: dict[str, Any]


@dataclass(frozen=True)
class PublicReviewProgress:
    """One invocation's view of a full public review.

    ``started`` is set only after the public-review stage is entered, so an
    unrelated planning/optional timeout cannot be mistaken for a public-review
    timeout. ``reason`` is set only when the stage stopped on a recoverable
    limit. The value is carried by a ContextVar and by the raised budget
    exception; it is never stored in a plugin-wide or cross-trial flag.
    """

    started: bool
    reason: str | None = None
    detail: str = ""
    deadline_unix: float | None = None
    total_seconds: float | None = None
    completed_criteria: tuple[str, ...] = ()
    required_criteria: tuple[str, ...] = ()
    refused: bool = False
    degradation: Mapping[str, Any] | None = None

    def message(self) -> str:
        return (
            "full public criterion review is incomplete ("
            + (self.reason or "incomplete")
            + "); validated per-criterion checkpoints are retained and P1 did not start"
        )

    def receipt(self) -> dict[str, Any]:
        return {
            "schema": "long-horizon/public-review-recovery/v1",
            "reason": self.reason or "incomplete",
            "detail": self.detail[:1000],
            "stage": "public-verification",
            "review_started": self.started,
            "deadline_unix": self.deadline_unix,
            "total_seconds": self.total_seconds,
            "completed_criteria": list(self.completed_criteria),
            "required_criteria": list(self.required_criteria),
            "refused": self.refused,
            "degradation": dict(self.degradation or {}),
        }


# Call-local progress shared by the plugin stage and the suite wrapper. A
# ContextVar follows the current async invocation, so a resumed trial or a
# concurrent preparation cannot inherit another trial's marker.
_PROGRESS: ContextVar[PublicReviewProgress | None] = ContextVar("long_horizon_public_review", default=None)


_CAPACITY_ERRNOS = frozenset(
    value
    for value in (errno.ENOSPC, getattr(errno, "EDQUOT", None), getattr(errno, "EFBIG", None))
    if value is not None
)


def capacity_os_error(value: Any) -> OSError | None:
    """Return a real volume-capacity failure, never a permission/path error."""
    if isinstance(value, OSError) and value.errno in _CAPACITY_ERRNOS:
        return value
    if isinstance(value, BaseExceptionGroup):
        for item in value.exceptions:
            found = capacity_os_error(item)
            if found is not None:
                return found
    return None


def storage_capacity_progress(
    exc: OSError,
    *,
    started: bool = False,
    deadline_unix: float | None = None,
    total_seconds: float | None = None,
) -> PublicReviewProgress:
    return PublicReviewProgress(
        started=started,
        reason="storage_capacity",
        detail=f"{type(exc).__name__}: {exc}"[:1000],
        deadline_unix=deadline_unix,
        total_seconds=total_seconds,
    )


def recoverable_public_review_error(exhausted: type[BaseException], progress: PublicReviewProgress):
    """Keep the controller's existing ``artifacts.exhausted -> p0_saved`` path."""
    error = exhausted(progress.message())
    error.public_review = progress
    error.reason = progress.reason or "incomplete"
    error.recoverable_public_review = True
    return error


def clear_public_review() -> None:
    _PROGRESS.set(None)


def set_public_review(progress: PublicReviewProgress) -> None:
    _PROGRESS.set(progress)


def current_public_review() -> PublicReviewProgress | None:
    progress = _PROGRESS.get()
    return progress if isinstance(progress, PublicReviewProgress) else None


def public_review_failure(value: Any) -> PublicReviewProgress | None:
    """Return the typed recoverable reason carried by a planning failure."""
    progress = getattr(value, "public_review", None)
    if isinstance(progress, PublicReviewProgress) and progress.reason is not None:
        return progress
    return None


def started_public_review() -> PublicReviewProgress | None:
    """Return call-local review progress only after the review stage was entered."""
    progress = current_public_review()
    if isinstance(progress, PublicReviewProgress) and progress.started:
        return progress
    return None


def with_reason(progress: PublicReviewProgress, reason: str, detail: str = "") -> PublicReviewProgress:
    return replace(progress, reason=reason, detail=detail or progress.detail)
