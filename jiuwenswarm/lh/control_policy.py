# Copyright (c) 2026 Chrys. All rights reserved.

"""Task-neutral phase reservations inside one original trial ledger."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from pact_core.artifacts import write_json_atomic

from .review_governance import acceptance_reserve

# Default finish work when this run has not yet observed a seal and host check.
DEFAULT_ARTIFACT_SECONDS = 60.0
DEFAULT_CHECK_SECONDS = 120.0
DEFAULT_FINISH_RESERVE_SECONDS = DEFAULT_ARTIFACT_SECONDS + DEFAULT_CHECK_SECONDS
FINISH_RESERVE_SAFETY_FACTOR = 1.5
MAX_FINISH_RESERVE_SECONDS = 10 * 60.0
FINISH_RESERVE_REMAINING_RATIO = 0.15


class TurnBudgetExhausted(TimeoutError):
    """A role was refused before native execution; admitted calls stay counted."""

    def __init__(self, message: str, *, remaining_seconds: float, total_seconds: float) -> None:
        super().__init__(message)
        self.remaining_seconds = remaining_seconds
        self.total_seconds = total_seconds

    def receipt(self) -> dict[str, object]:
        return {
            "schema": "long-horizon/turn-window-stop/v1",
            "reason": "no role turn fits the residual execution window",
            "remaining_seconds": self.remaining_seconds,
            "total_seconds": self.total_seconds,
            "internal_failure": False,
        }


TurnWindowSpent = TurnBudgetExhausted


def official_residual(deadline: float, total: float, now: float) -> float:
    """Measure the original window without replacing its full trial duration."""
    for name, value in (("deadline", deadline), ("total", total), ("now", now)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{name} must be a finite timestamp or duration")
    if total <= 0:
        raise ValueError("total must be a positive duration")
    return deadline - now


@dataclass
class PhasePolicy:
    started: float
    deadline: float
    execution_deadline: float
    total: float
    path: Path
    clock: Callable[[], float] = time.time
    admissions: list[dict] = field(default_factory=list)
    side_calls_use_execution_deadline: bool = False
    # Each entry is one completed seal/export plus its bound host public check.
    finish_observations: list[float] = field(default_factory=list)
    # Completed full acceptances observed in this trial. The same values feed
    # review_governance.acceptance_reserve(), so Worker admission and final
    # acceptance use one reserve rather than two competing estimates.
    recent_acceptance: list[float] = field(default_factory=list)
    # Kept for callers that still pass an explicit model-window duration. It no
    # longer changes phase fractions: recovery must not mix the original start
    # with a shortened residual window.
    window_seconds: float | None = None
    # Latest completed acceptance history, read from the durable ledger each
    # time a Worker cutoff is computed. This keeps the runtime cutoff and final
    # admission on the same A=max(...) calculation and the same observations.
    acceptance_history_provider: Callable[[], list[float]] | None = None

    full_acceptance_provider: Callable[[], list[float]] | None = None

    @property
    def hard_deadline(self) -> float:
        """The original clock: admitted checks, seals and exports stop here."""
        return self.deadline

    @property
    def soft_deadline(self) -> float:
        """The model-admission boundary: no new Worker/Reviewer/diagnosis turn."""
        return self.execution_deadline

    @property
    def stage_total(self) -> float:
        """The full trial duration used by every absolute phase fraction."""
        return float(self.total)

    def _acceptance_history(self) -> list[float]:
        """Return the latest completed-acceptance durations.

        The controller supplies the durable ledger reader. When no provider is
        installed (unit-level policies and older call sites), the in-memory list
        remains the source.
        """
        provider = self.acceptance_history_provider
        if provider is None:
            return list(self.recent_acceptance)
        try:
            values = provider()
        except Exception:
            return list(self.recent_acceptance)
        result: list[float] = []
        for value in values:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            if not math.isfinite(float(value)) or float(value) < 0:
                continue
            result.append(float(value))
        return result

    @property
    def acceptance_reserve_seconds(self) -> float:
        """The shared A=max(10min, 5%T, slowest recent acceptance*1.25)."""
        return acceptance_reserve(
            total_seconds=self.total,
            recent_acceptance=self._acceptance_history(),
            full_acceptance=self.full_acceptance_provider() if self.full_acceptance_provider else (),
        )

    def observe_acceptance(self, seconds: float) -> None:
        """Record one completed full acceptance for the shared reserve."""
        if (
            isinstance(seconds, bool)
            or not isinstance(seconds, (int, float))
            or not math.isfinite(float(seconds))
            or float(seconds) < 0
        ):
            raise ValueError("acceptance observation must be a finite nonnegative duration")
        self.recent_acceptance.append(float(seconds))
        write_json_atomic(self.path, self.payload())

    @property
    def review_reserve_seconds(self) -> float:
        """Reuse the existing minimum Reviewer window; no new quota is minted.

        On very short synthetic windows the same constant is capped by the
        window's own size so a mandatory review reservation cannot make P1
        impossible before it starts.
        """
        from .reviewer_execution import REVIEWER_FINISH_RESERVE_SECONDS

        return min(float(REVIEWER_FINISH_RESERVE_SECONDS), max(0.0, self.total * 0.10))

    def observe_finish(self, seconds: float) -> None:
        """Record one observed seal/export plus public-check duration."""
        if (
            isinstance(seconds, bool)
            or not isinstance(seconds, (int, float))
            or not math.isfinite(float(seconds))
            or float(seconds) < 0
        ):
            raise ValueError("finish observation must be a finite nonnegative duration")
        self.finish_observations.append(float(seconds))
        write_json_atomic(self.path, self.payload())

    def finish_reserve(self) -> tuple[float, str]:
        """Return the finish reserve and whether it came from observation.

        The reserve is max(fixed floor, observed maximum * safety factor), then
        capped by a fraction of the time still left and by ten minutes. A run
        that has not checked anything uses the conservative 60s seal + 120s
        check default. Public-check timeouts themselves are never shortened.
        """
        now = self.clock()
        remaining = max(0.0, float(self.execution_deadline) - now)
        cap = min(remaining * FINISH_RESERVE_REMAINING_RATIO, MAX_FINISH_RESERVE_SECONDS)
        observed = max((value for value in self.finish_observations if value >= 0), default=None)
        if observed is None:
            estimate = DEFAULT_FINISH_RESERVE_SECONDS
            source = "default"
        else:
            estimate = max(DEFAULT_FINISH_RESERVE_SECONDS, observed * FINISH_RESERVE_SAFETY_FACTOR)
            source = "observed"
        return min(estimate, cap), source

    @property
    def finish_reserve_seconds(self) -> float:
        return self.finish_reserve()[0]

    @property
    def finish_reserve_source(self) -> str:
        return self.finish_reserve()[1]

    @property
    def check_reserve_seconds(self):
        """Compatibility view: the check share inside the combined reserve."""
        reserve = self.finish_reserve_seconds
        return min(DEFAULT_CHECK_SECONDS, reserve)

    @property
    def artifact_seconds(self):
        """Compatibility view: the seal share inside the combined reserve."""
        reserve = self.finish_reserve_seconds
        return min(DEFAULT_ARTIFACT_SECONDS, max(0.0, reserve - self.check_reserve_seconds))

    def _worker_deadline_decision(self) -> tuple[float, bool, float, float]:
        """Return (deadline, acceptance_skipped, worker_seconds, minimum_seconds).

        Normally a Worker stops at ``soft - (seal/check reserve + A)`` so one
        full acceptance and one host check still fit. If that would leave too
        little Worker time, keep the Worker launchable and reserve only the
        seal/check budget; the evidence records that A was skipped.
        """
        now = self.clock()
        finish = self.finish_reserve_seconds
        acceptance = self.acceptance_reserve_seconds
        remaining = max(0.0, float(self.soft_deadline) - now)
        candidate = min(float(self.soft_deadline) - finish - acceptance, float(self.hard_deadline))
        minimum = max(600.0, remaining * 0.20)
        if candidate - now < minimum:
            deadline = min(float(self.soft_deadline) - finish, float(self.hard_deadline))
            return deadline, True, max(0.0, deadline - now), minimum
        return candidate, False, max(0.0, candidate - now), minimum

    def worker_deadline(self) -> float:
        """A Worker whose result still needs one acceptance and one host check."""
        deadline, _skipped, _worker_seconds, _minimum = self._worker_deadline_decision()
        return deadline

    def worker_deadline_evidence(self) -> dict[str, object]:
        deadline, skipped, worker_seconds, minimum = self._worker_deadline_decision()
        return {
            "deadline": deadline,
            "acceptance_reserve_seconds": self.acceptance_reserve_seconds,
            "acceptance_reserve_skipped": skipped,
            "worker_seconds_after_reserve": worker_seconds,
            "worker_minimum_seconds": minimum,
        }

    def deadline_for(self, stage: str) -> float:
        now = self.clock()
        reserve = self.finish_reserve_seconds
        worker_deadline, acceptance_skipped, worker_seconds, worker_minimum = self._worker_deadline_decision()
        stage_total = self.stage_total
        if stage == "p0":
            deadline = min(
                self.started + stage_total * 0.35,
                self.execution_deadline - stage_total * 0.50 - reserve,
                worker_deadline,
            )
            # A stale original-start fraction must not veto a Worker while the
            # absolute soft window still has launchable time after recovery.
            if deadline <= now < worker_deadline:
                deadline = worker_deadline
        elif stage == "planning":
            deadline = min(
                now + stage_total * 0.15,
                self.execution_deadline - stage_total * 0.35 - reserve,
                worker_deadline,
            )
            if deadline <= now < worker_deadline:
                deadline = worker_deadline
        elif stage == "p1":
            deadline = worker_deadline
        elif stage in ("check", "latest-check"):
            # Checks and seals already hold a candidate; they may use the
            # original hard clock even after model admission has closed.
            deadline = self.hard_deadline
        elif stage in ("manager", "planner"):
            deadline = (
                self.soft_deadline
                if self.side_calls_use_execution_deadline
                else min(now + min(15.0, self.stage_total * 0.02), self.hard_deadline)
            )
        elif stage == "worker":
            deadline = worker_deadline
        else:
            deadline = self.soft_deadline
        if stage in ("check", "latest-check"):
            deadline = min(deadline, self.hard_deadline)
        else:
            # Every new model turn is also bounded by the model-admission
            # boundary, never only by the later hard clock.
            deadline = min(deadline, self.soft_deadline, self.hard_deadline)
        self.admissions.append(
            {
                "stage": stage,
                "at": now,
                "deadline": deadline,
                "admitted_seconds": max(0.0, deadline - now),
                "finish_reserve_seconds": reserve,
                "finish_reserve_source": self.finish_reserve_source,
                "acceptance_reserve_seconds": self.acceptance_reserve_seconds,
                "acceptance_reserve_skipped": acceptance_skipped
                if stage in ("p0", "p1", "planning", "worker")
                else False,
                "worker_seconds_after_reserve": worker_seconds,
                "worker_minimum_seconds": worker_minimum,
            }
        )
        write_json_atomic(self.path, self.payload())
        return deadline

    def payload(self):
        reserve, source = self.finish_reserve()
        worker = self.worker_deadline_evidence()
        return {
            "schema": "long-horizon/phase-policy/v3",
            "started_at": self.started,
            "deadline": self.hard_deadline,
            "execution_deadline": self.soft_deadline,
            "soft_deadline": self.soft_deadline,
            "hard_deadline": self.hard_deadline,
            "fractions": {"p0": 0.35, "planning": 0.15, "repair_reserved": 0.35},
            "total_seconds": self.total,
            "window_seconds": self.stage_total,
            "check_cap_seconds": None,
            "check_budget_policy": "task_remaining_global_v1",
            "finish_reserve_seconds": reserve,
            "finish_reserve_source": source,
            "finish_reserve_policy": (
                "max(180s, observed_seal_plus_check_max*1.5), "
                "capped at min(remaining*15%, 600s)"
            ),
            "finish_observations_seconds": list(self.finish_observations),
            "review_reserve_seconds": self.review_reserve_seconds,
            "acceptance_reserve_seconds": self.acceptance_reserve_seconds,
            "acceptance_reserve_policy": "shared review_governance.acceptance_reserve/v2",
            "recent_acceptance_seconds": self._acceptance_history(),
            "full_acceptance_seconds": self.full_acceptance_provider() if self.full_acceptance_provider else [],
            "worker_deadline": worker["deadline"],
            "acceptance_reserve_skipped": worker["acceptance_reserve_skipped"],
            "worker_seconds_after_reserve": worker["worker_seconds_after_reserve"],
            "worker_minimum_seconds": worker["worker_minimum_seconds"],
            "check_reserve_seconds": self.check_reserve_seconds,
            "artifact_reserve_seconds": self.artifact_seconds,
            **(
                {"side_call_budget_policy": "original_execution_deadline_v1", "side_call_cap_seconds": None}
                if self.side_calls_use_execution_deadline
                else {}
            ),
            "admissions": self.admissions,
        }
