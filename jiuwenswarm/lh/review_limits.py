# Copyright (c) 2026 Chrys. All rights reserved.

"""Apply the durable Reviewer admission limit to every BenchDriver review."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Any, Iterator

from jiuwenswarm.pact_core.benchmark import review_progress
from jiuwenswarm.pact_core.benchmark.budget import BenchBudget, BudgetWindow
from jiuwenswarm.pact_core.benchmark.driver import (
    BenchDriver,
    _acceptance_input,
    _progress,
    _public_passed,
)
from jiuwenswarm.pact_core.runtime.store import data_digest

from .method_routing import method_diagnosis, only_method_failures
from .review_admissions import ReviewAdmissions

_PINNED_WINDOW = threading.local()
_PATCH_LOCK = threading.Lock()
_PATCHED_BUDGETS = 0


class _PinnedWindowBudget(BenchBudget):
    """BenchBudget whose window is the already admitted absolute boundary."""

    def window(self, seconds: float | None = None) -> BudgetWindow:
        pinned = getattr(_PINNED_WINDOW, "window", None)
        if pinned is not None:
            return pinned
        return super().window(seconds)


@contextmanager
def _pinned_budget_window(window: BudgetWindow) -> Iterator[None]:
    """Reuse one admitted window inside BenchDriver.run without re-opening it.

    BenchDriver constructs its own budget from the trial ledger and normally
    calls ``window(seconds)``. Pinning the class symbol lets the driver keep the
    exact absolute deadline the controller already admitted; the thread-local
    window leaves unrelated callers unchanged.
    """
    from jiuwenswarm.pact_core.benchmark import driver as driver_module

    previous = getattr(_PINNED_WINDOW, "window", None)
    global _PATCHED_BUDGETS
    with _PATCH_LOCK:
        if _PATCHED_BUDGETS == 0:
            driver_module.BenchBudget = _PinnedWindowBudget
        _PATCHED_BUDGETS += 1
    _PINNED_WINDOW.window = window
    try:
        yield
    finally:
        _PINNED_WINDOW.window = previous
        with _PATCH_LOCK:
            _PATCHED_BUDGETS -= 1
            if _PATCHED_BUDGETS == 0:
                driver_module.BenchBudget = BenchBudget


class ReviewLimitedDriver(BenchDriver):
    """BenchDriver whose review attempts share the controller's durable cap."""

    review_admissions: ReviewAdmissions | None = None
    review_gate: Any = None
    worker_gate: Any = None

    def _retry_public_reference(self, state, budget, window):
        """Recheck a supplemental tool on the same candidate once, before spending a model admission."""
        methods, other = method_diagnosis(state)
        if (
            other
            or not methods
            or not all(
                method["kind"] == "public_reference"
                and method["value"] in ("timeout", "service_unavailable", "access_denied")
                for method in methods.values()
            )
        ):
            return state, False
        identity = {"checkpoint": state.candidate.checkpoint, "definitions": data_digest(list(state.checks))}
        progress = _progress(state)
        if progress.get("public_reference_retry") == identity or budget.stop_reason(window.deadline):
            return state, False
        # Persist before executing so retries and resume cannot replenish this allowance.
        state = self._progress(state, {**progress, "public_reference_retry": identity})
        return self.runtime.verify_candidate(
            state.work_id, expected_revision=state.revision, deadline=window.deadline
        ), True

    def _worker(self, state: Any, budget: Any, window: Any) -> tuple[Any, str | None]:
        state, retried = self._retry_public_reference(state, budget, window)
        if retried and _public_passed(state):
            return state, None
        progress = _progress(state)
        revised = progress.get("recheck_method_input")
        if (
            isinstance(revised, dict)
            and state.verification is None
            and revised["checkpoint"] == state.candidate.checkpoint
            and revised["previous_definitions"] != data_digest(list(state.checks))
        ):
            # BenchDriver's next statement verifies this exact saved candidate.
            return state, None
        methods, other = method_diagnosis(state)
        if methods:
            diagnosed = progress.get("last_feedback_input") == {**_acceptance_input(state), "scope": "diagnostic"}
            if not diagnosed:
                state, stop = self._review(state, budget, window, purpose="diagnostic")
                if stop or state.verification is None:
                    return state, stop
            if not other:
                return state, "public_check_method_unresolved"
        # A diagnostic may consume the time that was available on entry.
        # Apply the Worker's own admission immediately before its native call.
        if self.worker_gate is not None:
            reason = self.worker_gate(state, budget, window)
            if reason:
                return state, reason
        return super()._worker(state, budget, window)

    def _complete_review(self, state, attempt, *, valid, acceptance_finished):
        methods, _ = method_diagnosis(state)
        if methods and valid and not attempt["full"] and state.review is not None:
            # Record the original input before Core applies the Reviewer's
            # decisions. Recovery may enter between each registration.
            progress = _progress(state)
            progress["recheck_method_input"] = {
                "checkpoint": state.candidate.checkpoint,
                "previous_definitions": data_digest(list(state.checks)),
            }
            state = self._progress(state, progress)
        return super()._complete_review(state, attempt, valid=valid, acceptance_finished=acceptance_finished)

    def _review(
        self,
        state: Any,
        budget: Any,
        window: Any,
        *,
        purpose: str,
        full: bool = False,
        force: bool = False,
    ) -> tuple[Any, str | None]:
        if purpose == "diagnostic":
            state, retried = self._retry_public_reference(state, budget, window)
            if retried and _public_passed(state):
                return state, None
        if self.review_gate is not None:
            gate_reason = self.review_gate(state, budget, window, purpose)
            if gate_reason == "diagnosis_paused" and purpose != "acceptance":
                # Do not consume an admission on a repeat diagnosis that has
                # produced no new evidence; let the caller spend the time on an
                # executable repair instead.
                return state, None
            if gate_reason:
                return state, gate_reason
        if self.review_admissions is not None:
            # Match the parent's own preconditions before charging the durable
            # ledger: a budget stop or a deduplicated feedback pass is not an
            # admitted Reviewer call.
            deadline = budget.purpose_deadline(window, purpose)
            stop = budget.stop_reason(deadline, purpose=purpose)
            if stop:
                return state, stop
            basis = _acceptance_input(state)
            feedback_basis = {**basis, "scope": purpose}
            progress = _progress(state)
            if (
                not full
                and not force
                and _public_passed(state)
                and not review_progress.pending(progress)
                and progress.get("last_feedback_input") == feedback_basis
            ):
                return state, None
            decision = self.review_admissions.admit(
                state,
                purpose=purpose,
                full=full,
                force=force,
                check=lambda: budget.stop_reason(deadline, purpose=purpose),
            )
            if not decision["admitted"]:
                # A spent diagnosis/repair allowance hands over to the reserved
                # acceptance allowance instead of ending the trial. A same-input
                # refusal remains a refusal: it is the anti-retry guard, not a
                # signal that the candidate has earned a final decision.
                if (
                    not method_diagnosis(state)[0]
                    and decision.get("category") == "diagnosis"
                    and decision.get("reason") in {"diagnosis_limit", "diagnosis_fuse"}
                    and decision.get("acceptance_remaining", 0) > 0
                ):
                    acceptance_deadline = budget.purpose_deadline(window, "acceptance")
                    acceptance_stop = budget.stop_reason(acceptance_deadline, purpose="acceptance")
                    if acceptance_stop:
                        return state, acceptance_stop
                    accepted = self.review_admissions.admit(
                        state,
                        purpose="acceptance",
                        full=True,
                        force=True,
                        check=lambda: budget.stop_reason(acceptance_deadline, purpose="acceptance"),
                    )
                    if not accepted["admitted"]:
                        return state, accepted.get("stop") or "review_limit_reached"
                    return super()._review(state, budget, window, purpose="acceptance", full=True, force=True)
                if purpose == "diagnostic" and only_method_failures(state):
                    return state, "public_check_method_unresolved"
                return state, decision.get("stop") or "review_limit_reached"
        return super()._review(state, budget, window, purpose=purpose, full=full, force=force)
