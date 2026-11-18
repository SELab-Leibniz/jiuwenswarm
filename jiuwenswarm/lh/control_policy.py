# Copyright (c) 2026 Chrys. All rights reserved.

"""Task-neutral phase reservations inside one original trial ledger."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from pact_core.artifacts import write_json_atomic


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

    @property
    def check_reserve_seconds(self):
        """Reserve planning headroom without capping an admitted check."""
        return min(30.0, self.total * 0.04)

    @property
    def artifact_seconds(self):
        return min(20.0, self.total * 0.025)

    def deadline_for(self, stage: str) -> float:
        now = self.clock()
        if stage == "p0":
            deadline = min(
                self.started + self.total * 0.35,
                self.execution_deadline - self.total * 0.50 - self.check_reserve_seconds - self.artifact_seconds,
            )
        elif stage == "planning":
            deadline = min(
                now + self.total * 0.15,
                self.execution_deadline - self.total * 0.35 - self.check_reserve_seconds - self.artifact_seconds,
            )
        elif stage == "p1":
            deadline = self.execution_deadline - self.check_reserve_seconds - self.artifact_seconds
        elif stage == "check":
            deadline = self.deadline
        elif stage in ("manager", "planner"):
            deadline = (
                self.execution_deadline
                if self.side_calls_use_execution_deadline
                else min(now + min(15.0, self.total * 0.02), self.deadline)
            )
        else:
            deadline = self.deadline
        deadline = min(deadline, self.deadline)
        self.admissions.append(
            {"stage": stage, "at": now, "deadline": deadline, "admitted_seconds": max(0.0, deadline - now)}
        )
        write_json_atomic(self.path, self.payload())
        return deadline

    def payload(self):
        return {
            "schema": "long-horizon/phase-policy/v2",
            "started_at": self.started,
            "deadline": self.deadline,
            "execution_deadline": self.execution_deadline,
            "fractions": {"p0": 0.35, "planning": 0.15, "repair_reserved": 0.35},
            "check_cap_seconds": None,
            "check_budget_policy": "task_remaining_global_v1",
            "check_reserve_seconds": self.check_reserve_seconds,
            "artifact_reserve_seconds": self.artifact_seconds,
            **(
                {"side_call_budget_policy": "original_execution_deadline_v1", "side_call_cap_seconds": None}
                if self.side_calls_use_execution_deadline
                else {}
            ),
            "admissions": self.admissions,
        }
