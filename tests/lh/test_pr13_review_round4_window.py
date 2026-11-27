# Copyright (c) 2026 Chrys. All rights reserved.

"""Fourth-review window regression: pass the admitted absolute deadline."""

from __future__ import annotations

from types import SimpleNamespace

from jiuwenswarm.pact_core.benchmark.budget import BudgetWindow

from jiuwenswarm.lh.controller import LongHorizonBenchmark


def test_driver_window_passes_the_original_absolute_deadline():
    controller = object.__new__(LongHorizonBenchmark)
    controller._args = SimpleNamespace(window_seconds=60.0)
    budget = SimpleNamespace(clock=lambda: 40.0)
    window = BudgetWindow(0.0, 100.0, 90.0, 10.0)
    assert controller._driver_window_seconds(window, budget) == 100.0
    controller._args = SimpleNamespace(window_seconds=None)
    assert controller._driver_window_seconds(window, budget) == 100.0
    assert controller._driver_window_seconds(None, budget) is None


def test_pinned_driver_window_keeps_the_admitted_deadline_without_drift():
    from jiuwenswarm.pact_core.benchmark import driver as driver_module

    from jiuwenswarm.lh.review_limits import _pinned_budget_window

    window = BudgetWindow(10.0, 100.0, 100.0, 0.0)
    with _pinned_budget_window(window):
        budget = object.__new__(driver_module.BenchBudget)
        budget.clock = lambda: 40.0
        assert budget.window(None) is window
