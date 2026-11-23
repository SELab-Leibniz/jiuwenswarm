"""Budget-ledger boundary for role adapters.

``pact_core.benchmark.budget.BudgetedAdapter`` records ``result.status`` of every
turn verbatim, while the ledger reader rejects any status outside a fixed set and
then fails the whole trial with ``Invalid benchmark call role or status``. Several
controller layers above ``RoleBoundary`` (for example a cancelled Reviewer
preparation) can still produce other statuses, so every budgeted turn passes
through this boundary first, including the roles handed to Core's BenchDriver,
which wraps them in Core's own ``BudgetedAdapter``.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from pact_core.benchmark.budget import BudgetedAdapter as _CoreBudgetedAdapter

# Mirrors the finished-call statuses accepted by the vendored Core budget reader.
LEDGER_TURN_STATUSES = frozenset({"completed", "timeout", "output_missing", "spawn_failed", "error"})


def ledger_safe_result(result: Any) -> Any:
    """Map a status the ledger cannot record to ``output_missing``, keeping the original in the tail."""
    if result.status in LEDGER_TURN_STATUSES:
        return result
    return replace(
        result,
        status="output_missing",
        final_text="",
        stderr_tail=f"native_status={result.status}; {result.stderr_tail}",
    )


class LedgerStatusBoundary:
    """Role adapter whose turn results always carry a ledger-valid status."""

    def __init__(self, adapter: Any) -> None:
        self._adapter = adapter

    def __getattr__(self, name: str) -> Any:
        if name == "_adapter":
            raise AttributeError(name)
        return getattr(self._adapter, name)

    def run_turn(self, request: Any) -> Any:
        return ledger_safe_result(self._adapter.run_turn(request))


class BudgetedAdapter(_CoreBudgetedAdapter):
    """Core ``BudgetedAdapter`` whose recorded and returned statuses are always ledger-valid."""

    def __init__(self, adapter: Any, budget: Any, *, purpose: str, deadline: float) -> None:
        super().__init__(LedgerStatusBoundary(adapter), budget, purpose=purpose, deadline=deadline)
