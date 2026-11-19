# Copyright (c) 2026 Chrys. All rights reserved.

"""Carry an upstream phase origin into the existing immutable Core ledger."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from pathlib import Path

from pact_core.benchmark.budget import BenchBudget
from pact_core.benchmark.types import BenchConfig
from pact_core.runtime import RuntimeContractError


def validate_origin(started_at: float) -> None:
    if (
        isinstance(started_at, bool)
        or not isinstance(started_at, (int, float))
        or not math.isfinite(started_at)
        or started_at <= 0
    ):
        raise RuntimeContractError("budget_started_at_unix must be a positive finite epoch")


def remaining_seconds(seconds: float, started_at: float, *, now: float | None = None) -> float:
    validate_origin(started_at)
    if isinstance(seconds, bool) or not math.isfinite(seconds) or seconds <= 0:
        raise RuntimeContractError("budget_seconds must be positive and finite")
    current = time.time() if now is None else now
    if started_at > current:
        raise RuntimeContractError("upstream budget origin is in the future")
    remaining = started_at + seconds - current
    if remaining <= 0:
        raise TimeoutError("official agent budget expired before native admission")
    return remaining


def create_budget(
    root: Path,
    config: BenchConfig,
    work_id: str,
    started_at: float | None,
    *,
    clock: Callable[[], float] | None = None,
) -> BenchBudget:
    current_clock = time.time if clock is None else clock
    if started_at is None:
        return BenchBudget(root, config, work_id, clock=current_clock)
    validate_origin(started_at)
    if started_at > current_clock():
        raise RuntimeContractError("upstream budget origin is in the future")
    # Core's supported clock injection seeds new ledgers only. Immediately
    # restore the spending clock; every window/admission reads current time.
    # Existing ledgers are read unchanged, then checked against the bound origin.
    budget = BenchBudget(root, config, work_id, clock=lambda: started_at)
    budget.clock = current_clock
    if budget.snapshot()["started_at"] != started_at:
        raise RuntimeContractError("benchmark ledger belongs to a different upstream origin")
    return budget
