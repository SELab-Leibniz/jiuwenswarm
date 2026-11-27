# Copyright (c) 2026 Chrys. All rights reserved.

"""Every budgeted role turn leaves the Core budget ledger readable.

Real Core ``BenchBudget`` ledgers; scripted roles, no model.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from jiuwenswarm.pact_core.adapters.base import TurnRequest
from jiuwenswarm.pact_core.benchmark.budget import BenchBudget
from jiuwenswarm.pact_core.benchmark.budget import BudgetedAdapter as CoreBudgetedAdapter
from jiuwenswarm.pact_core.benchmark.types import BenchConfig
from jiuwenswarm.pact_core.runtime import RuntimeContractError
from jiuwenswarm.pact_core.schemas import TurnResult

from jiuwenswarm.lh.budget_boundary import BudgetedAdapter, LedgerStatusBoundary


class _ScriptedReviewer:
    id = "scripted-reviewer"
    marker = "forwarded"

    def __init__(self, status: str) -> None:
        self.status = status

    def probe(self):
        return None

    def run_turn(self, request):
        return TurnResult(
            status=self.status,
            final_text="partial",
            output_source="native",
            exit_code=None,
            stderr_tail="reviewer preparation cancelled",
            review_decision=None,
        )


def _budget(root: Path) -> BenchBudget:
    config = BenchConfig(task_id="synthetic", trial_id="one", budget_seconds=120, max_role_calls=10)
    return BenchBudget(root, config, "work")


def _turn(adapter, tmp_path):
    return adapter.run_turn(TurnRequest("reviewer", "synthetic", Path(tmp_path), 60))


def test_core_ledger_rejects_a_cancelled_turn(tmp_path):
    budget = _budget(tmp_path / "core")
    _turn(
        CoreBudgetedAdapter(_ScriptedReviewer("cancelled"), budget, purpose="acceptance", deadline=time.time() + 60),
        tmp_path,
    )
    with pytest.raises(RuntimeContractError, match="Invalid benchmark call role or status"):
        budget.snapshot()


def test_cancelled_turn_is_recorded_as_output_missing(tmp_path):
    budget = _budget(tmp_path / "lh")
    role = BudgetedAdapter(_ScriptedReviewer("cancelled"), budget, purpose="acceptance", deadline=time.time() + 60)
    result = _turn(role, tmp_path)
    assert result.status == "output_missing" and result.final_text == ""
    assert result.stderr_tail == "native_status=cancelled; reviewer preparation cancelled"
    assert [call["status"] for call in budget.snapshot()["calls"]] == ["output_missing"]
    assert role.results == [result]


@pytest.mark.parametrize("status", ["completed", "timeout", "output_missing", "spawn_failed", "error"])
def test_ledger_statuses_pass_through_unchanged(tmp_path, status):
    budget = _budget(tmp_path / status)
    native = _ScriptedReviewer(status)
    result = _turn(BudgetedAdapter(native, budget, purpose="acceptance", deadline=time.time() + 60), tmp_path)
    assert result.status == status and result.final_text == "partial"
    assert [call["status"] for call in budget.snapshot()["calls"]] == [status]


def test_driver_roles_keep_their_native_surface(tmp_path):
    native = _ScriptedReviewer("cancelled")
    role = LedgerStatusBoundary(native)
    assert role.id == native.id and role.marker == "forwarded" and role.probe() is None
    budget = _budget(tmp_path / "driver")
    # Core's BenchDriver wraps the roles it is given in Core's own BudgetedAdapter.
    _turn(CoreBudgetedAdapter(role, budget, purpose="acceptance", deadline=time.time() + 60), tmp_path)
    assert [call["status"] for call in budget.snapshot()["calls"]] == ["output_missing"]
