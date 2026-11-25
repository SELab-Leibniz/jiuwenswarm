# Copyright (c) 2026 Chrys. All rights reserved.

"""An upstream deadline must survive setup delay, role admission and resume."""

from __future__ import annotations

from pathlib import Path

import pytest
from jiuwenswarm.pact_core.adapters.base import TurnRequest
from jiuwenswarm.pact_core.benchmark.budget import BenchBudget, BudgetedAdapter
from jiuwenswarm.pact_core.benchmark.types import BenchConfig
from jiuwenswarm.pact_core.runtime import RuntimeContractError

from jiuwenswarm.lh.budget_origin import create_budget, remaining_seconds, validate_origin


@pytest.mark.parametrize("delay", [0.0, 0.6192691326141357, 3.1390960216522217, 120.0])
def test_preparation_delay_never_extends_core_or_admits_expired_review(tmp_path, delay):
    origin, seconds = 1000.0, 28800.0
    now = [origin + delay]
    config = BenchConfig(task_id="synthetic", trial_id="one", budget_seconds=seconds)
    budget = create_budget(tmp_path, config, "work", origin, clock=lambda: now[0])
    assert budget.snapshot()["deadline"] == origin + seconds
    assert budget.snapshot()["remaining_seconds"] == seconds - delay
    window = budget.window()
    assert window.started_at == now[0] and window.deadline == origin + seconds
    now[0] = origin + seconds

    class ForbiddenNative:
        id = "no-model"

        def run_turn(self, request):
            pytest.fail("An expired role must not reach native execution")

    role = BudgetedAdapter(ForbiddenNative(), budget, purpose="acceptance", deadline=window.deadline)
    result = role.run_turn(TurnRequest("reviewer", "synthetic", Path(tmp_path), seconds))
    assert result.status == "timeout" and result.output_source == "budget"
    assert budget.snapshot()["calls"] == []
    original_bytes = budget.path.read_bytes()
    now[0] += 300
    resumed = create_budget(tmp_path, config, "work", origin, clock=lambda: now[0])
    assert resumed.path.read_bytes() == original_bytes
    assert resumed.snapshot()["remaining_seconds"] == 0
    with pytest.raises(RuntimeContractError, match="different upstream origin"):
        create_budget(tmp_path, config, "work", origin + 1, clock=lambda: now[0])
    assert resumed.path.read_bytes() == original_bytes


def test_legacy_callers_keep_core_ledger_bytes(tmp_path):
    config = BenchConfig(task_id="synthetic", trial_id="one", budget_seconds=120)
    expected = BenchBudget(tmp_path / "old", config, "work", clock=lambda: 1000.0)
    actual = create_budget(tmp_path / "new", config, "work", None, clock=lambda: 1000.0)
    assert actual.path.read_bytes() == expected.path.read_bytes()


@pytest.mark.parametrize("invalid", [True, 0, -1, float("inf"), float("nan")])
def test_invalid_origins_fail_closed(invalid):
    with pytest.raises(RuntimeContractError):
        validate_origin(invalid)


def test_future_origin_and_expiry_fail_before_native_admission(tmp_path):
    config = BenchConfig(task_id="synthetic", trial_id="one", budget_seconds=120)
    with pytest.raises(RuntimeContractError, match="future"):
        create_budget(tmp_path, config, "work", 1001, clock=lambda: 1000)
    assert not list(tmp_path.rglob("budget.json"))
    with pytest.raises(TimeoutError):
        remaining_seconds(120, 1000, now=1120)
    assert remaining_seconds(120, 1000, now=1030) == 90
