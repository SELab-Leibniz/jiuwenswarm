"""A bounded noninteractive workflow for fixed benchmark requirements.

Scheduling belongs here; requirement authority, evidence and acceptance remain
Core operations. Only a Core acceptance receipt can authorize delivery; the
caller can separately export an unaccepted candidate for external grading.
"""
from __future__ import annotations

import json
import math
import re
from collections.abc import Callable
from typing import cast

from ..adapters.base import AgentAdapter, TurnRequest
from .budget import BenchBudget, BudgetedAdapter, BudgetWindow
from .types import BenchConfig, BenchResult
from . import review_progress
from ..kernel import read_work_candidate
from ..prompts import build_runtime_handoff_instructions
from ..runtime import WorkRuntime
from ..runtime.schemas import RuntimeContractError
from ..runtime.types import WorkSnapshot

_PROGRESS_SCHEMA = "pact-core/benchmark-progress/v2"
_CHECK_DECISION_HINT = (
    "Review supplemental check methods together with the work. Optionally return "
    '<pact-check-decisions>[{"name":"check name","version":1,"action":"admit",'
    '"reason":"why this method is valid"}]</pact-check-decisions>. '
    "Use action suspend for an irrelevant, duplicate or unusable derived method, with a concrete reason. "
    "Do not waive supplied checks or original requirements. A withdrawn method needs no replacement "
    "unless the original task still has a material evidence gap; explain any such gap in feedback. "
    "A valid check may fail because implementation is incomplete. Exit zero alone does not prove adequacy. "
)
_CHECK_HINT = (
    "Optionally propose executable requirement checks in <pact-checks>JSON array</pact-checks>. "
    "Each item has name, command, requirement (an exact quote from the original requirement), "
    "and optionally files (relative paths of test definitions, whose content hashes are pinned at registration). "
    "Do not list mutable implementation files under test in files; omit files for a self-contained command. "
    "Use assertions that exit 0 when the required behavior holds and nonzero for a concrete violation, "
    "not unconditional success or failure placeholders. If evidence is unavailable, explain the gap instead. "
    "Run externally supplied or injected fixtures through the supplied command; do not copy them into the candidate "
    "merely to run them again. Preserve supplied checks. "
    "If a check reports definition_changed, inspect the registered definition and its binding. "
    "To correct a mistaken binding or revise a test definition, propose the revised check under the same name "
    "and explain the reason outside the block, tied to the original requirement. "
    "Do not weaken or discard a failing check to obtain a pass. Omit the block if no concrete check is ready. "
    "These are extra evidence, never replacement requirements or proof of completion."
)


class BenchDriver:
    def __init__(self, runtime: WorkRuntime, worker: AgentAdapter, reviewer: AgentAdapter, *, config: BenchConfig) -> None:
        self.runtime = runtime
        self.worker = worker
        self.reviewer = reviewer
        self.config = config

    def run(self, work_id: str, *, expected_revision: int | None = None,
            max_turns: int | None = 3, window_seconds: float | None = None,
            stop_requested: Callable[[], bool] | None = None) -> BenchResult:
        """Run one window, retaining admitted results before a cooperative stop.

        Revision admission occurs inside the trial execution lock and before a
        window is recorded. Stopping never refunds trial time/calls or interrupts
        an admitted delivery.
        """
        def stopping() -> bool:
            return stop_requested is not None and stop_requested()

        if max_turns is not None and (isinstance(max_turns, bool) or not isinstance(max_turns, int) or max_turns < 1):
            raise RuntimeContractError("max_turns must be a positive integer or null")
        budget = BenchBudget(self.runtime.workspace / ".pact" / "bench" / "trials", self.config, work_id)
        with budget.execution():
            state = self.runtime.recover(work_id, expected_revision=expected_revision)
            budget.bind_work_inputs({"contract": state.contract, "constraints": list(state.constraints),
                "policy": state.policy.payload(), "worker_adapter": self.worker.id, "reviewer_adapter": self.reviewer.id})
            state = self._recover_review(state)
            window = budget.window(window_seconds)
            if max_turns is None and not all(
                isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
                for value in (window.started_at, window.execution_deadline, window.deadline)
            ):
                raise RuntimeContractError("deadline-only turns require a finite original budget window")
            if state.acceptance is not None:
                if stopping() and state.delivery is None:
                    return self._result(state, "invocation_stopped", budget)
                state = self.runtime.deliver(work_id, expected_revision=state.revision, acceptance=state.acceptance)
                return self._result(state, "accepted", budget)
            if not _can_continue(state):
                return self._result(state, state.status, budget)
            if stopping():
                return self._result(state, "invocation_stopped", budget)
            if "initial_tree" not in _progress(state):
                state = self._progress(state, {**_progress(state), "initial_tree": state.candidate.tree})
            if stopping():
                return self._result(state, "invocation_stopped", budget)
            state = self._prepare(state, budget, window)
            if stopping():
                return self._result(state, "invocation_stopped", budget)
            if not _can_continue(state):
                return self._result(state, state.status, budget)
            stop = "max_turns"
            review_attempts_on_entry = _count(_progress(state), "review_attempts")
            iterations = 0
            while max_turns is None or iterations < max_turns:
                iterations += 1
                if max_turns is None and not math.isfinite(budget.clock()):
                    raise RuntimeContractError("deadline-only turns require a finite spending clock")
                if stopping():
                    return self._result(state, "invocation_stopped", budget)
                execution_deadline = budget.purpose_deadline(window, 'implementation')
                blocked = budget.stop_reason(execution_deadline, purpose='implementation')
                if blocked == 'review_reserved':
                    full = bool(_progress(state).get('ready')) and _public_passed(state) and not self._already_accepted_input(state)
                    purpose = 'acceptance' if full else 'feedback' if _public_passed(state) else 'diagnostic'
                    state, role_stop = self._review(state, budget, window, purpose=purpose, full=full, force=True)
                    if stopping():
                        return self._result(state, 'invocation_stopped', budget)
                    if state.acceptance is not None:
                        state = self.runtime.deliver(work_id, expected_revision=state.revision, acceptance=state.acceptance)
                        return self._result(state, 'accepted', budget)
                    stop = budget.stop_reason(window.deadline) or role_stop or ('max_turns' if max_turns is not None else blocked)
                    break
                if blocked:
                    stop = blocked
                    break
                state, role_stop = self._worker(state, budget, window)
                if stopping():
                    return self._result(state, "invocation_stopped", budget)
                if not _can_continue(state):
                    return self._result(state, state.status, budget)
                if role_stop:
                    stop = role_stop
                    break
                if budget.stop_reason(execution_deadline):
                    stop = budget.stop_reason(execution_deadline) or "window_budget_exhausted"
                    break
                state = self.runtime.verify_candidate(work_id, expected_revision=state.revision, deadline=execution_deadline)
                state = self._observe(state)
                if stopping():
                    return self._result(state, "invocation_stopped", budget)
                progress = _progress(state)
                if progress.get("ready") and _public_passed(state):
                    if not self._already_accepted_input(state):
                        state, role_stop = self._review(state, budget, window, purpose="acceptance", full=True)
                        # Acceptance can fail before the Reviewer executes (for
                        # example a check regresses during its mandatory re-run).
                        if (not stopping() and not _public_passed(state) and state.acceptance is None
                                and not budget.stop_reason(window.deadline)):
                            state = self._observe(state)
                            state, role_stop = self._review(state, budget, window, purpose="diagnostic")
                    elif review_progress.pending(progress) and self._feedback_due(state):
                        state, role_stop = self._review(state, budget, window, purpose="diagnostic")
                elif self._feedback_due(state) or (progress.get('ready') and
                        progress.get('last_ready_failure_input') != _acceptance_input(state)):
                    purpose = "diagnostic" if not _public_passed(state) else "feedback"
                    state, role_stop = self._review(state, budget, window, purpose=purpose)
                if stopping():
                    return self._result(state, "invocation_stopped", budget)
                if state.acceptance is not None:
                    state = self.runtime.deliver(work_id, expected_revision=state.revision, acceptance=state.acceptance)
                    return self._result(state, "accepted", budget)
                if not _can_continue(state):
                    return self._result(state, state.status, budget)
                if role_stop:
                    stop = role_stop
                    break
            # The reserve is for a bounded final decision and export. Repeating
            # rejection of the exact same input does not buy new evidence.
            if stopping():
                return self._result(state, "invocation_stopped", budget)
            if (budget.clock() >= window.execution_deadline and not budget.stop_reason(window.deadline)
                    and (state.candidate.tree != _progress(state).get("initial_tree") or review_progress.pending(_progress(state)))
                    and (not self._already_accepted_input(state) or review_progress.pending(_progress(state)))
                    and (_count(_progress(state), 'review_attempts') == review_attempts_on_entry
                         or _count(_progress(state), 'worker_turns') > _count(_progress(state), 'last_feedback_attempt_turn'))):
                diagnose = review_progress.pending(_progress(state)) or (state.verification is not None and not _public_passed(state))
                state, role_stop = self._review(state, budget, window,
                    purpose="diagnostic" if diagnose else "acceptance", full=not diagnose, force=True)
                if stopping():
                    return self._result(state, "invocation_stopped", budget)
                if (not diagnose and not _public_passed(state) and not budget.stop_reason(window.deadline)):
                    state = self._observe(state)
                    state, role_stop = self._review(state, budget, window, purpose='diagnostic')
                if stopping():
                    return self._result(state, "invocation_stopped", budget)
                if state.acceptance is not None:
                    state = self.runtime.deliver(work_id, expected_revision=state.revision, acceptance=state.acceptance)
                    stop = "accepted"
                elif role_stop:
                    stop = role_stop
            return self._result(state, stop, budget)

    def _prepare(self, state: WorkSnapshot, budget: BenchBudget, window: BudgetWindow) -> WorkSnapshot:
        progress = _progress(state)
        if state.acceptance_brief:
            return state
        if progress.get("analysis_attempted"):
            if (progress.get("analysis_pending") and state.review is not None
                    and str(state.review.artifact) != progress.get("analysis_previous_review")
                    and state.review.candidate.tree == progress.get("analysis_tree")):
                return self._consume_analysis(state, state.review.feedback)
            return state
        # Commit the attempt before invoking a model. An interrupted analysis
        # must not silently consume another call on each resumed window.
        state = self._progress(state, {**progress, "analysis_attempted": True, "analysis_pending": True,
            "analysis_tree": state.candidate.tree, "analysis_previous_review": str(state.review.artifact) if state.review else None})
        if budget.stop_reason(window.execution_deadline, purpose="acceptance-analysis"):
            state = self._progress(state, {**_progress(state), "analysis_pending": False})
            return self._diagnostic(state, "Acceptance analysis skipped: preserve candidate/review budget.")
        wrapped = BudgetedAdapter(self.reviewer, budget, purpose="acceptance-analysis", deadline=window.execution_deadline)
        context = (
            "This is an acceptance-analysis pass before implementation, not final acceptance. "
            "Inspect the original task and repository, and write a concise acceptance brief: "
            "observable required behaviors, boundary cases, existing public checks, and missing evidence. "
            "Do not edit files, invent user answers, or expand the requirement. Analysis failure will not block implementation.\n"
            + _CHECK_HINT
        )
        state = self.runtime.review_candidate(state.work_id, expected_revision=state.revision, reviewer=wrapped,
            timeout_seconds=self.config.turn_timeout_seconds, context=context, deadline=window.execution_deadline)
        text = next((result.final_text for result in reversed(wrapped.results) if result.status == "completed" and result.final_text.strip()), "")
        state = self._consume_analysis(state, text)
        return state

    def _consume_analysis(self, state: WorkSnapshot, text: str) -> WorkSnapshot:
        if _review_mutated(state):
            state = self._diagnostic(state, "Acceptance analysis mutated files; its brief and check proposals were rejected.")
            return self._progress(state, {**_progress(state), "analysis_pending": False})
        if text:
            state = self.runtime.set_acceptance_brief(state.work_id, expected_revision=state.revision,
                text=text, source="benchmark acceptance analysis")
            state = self._register_checks(state, text)
        else:
            state = self._diagnostic(state, "Acceptance analysis produced no usable brief: role failure or budget exhaustion.")
        return self._progress(state, {**_progress(state), "analysis_pending": False})

    def _worker(self, state: WorkSnapshot, budget: BenchBudget, window: BudgetWindow) -> tuple[WorkSnapshot, str | None]:
        prompt = _worker_prompt(state)
        handle = self.runtime.begin_execution(state.work_id, expected_revision=state.revision, context=prompt, draft=True)
        wrapped = BudgetedAdapter(self.worker, budget, purpose="implementation",
            deadline=budget.purpose_deadline(window, "implementation"))
        try:
            result = wrapped.run_turn(TurnRequest(role="worker", prompt=prompt, workdir=handle.workdir,
                timeout_seconds=self.config.turn_timeout_seconds, artifact_dir=handle.artifact_dir))
            progress = _progress(state)
            progress.update({"worker_turns": _count(progress, "worker_turns") + len(wrapped.results),
                "ready": result.status == "completed" and re.search(r"(?im)^\s*PACT_REVIEW_READY\s*$", result.final_text) is not None,
                "milestone_ready": result.status == "completed" and re.search(r"(?im)^\s*PACT_MILESTONE_READY\s*$", result.final_text) is not None,
                "last_worker_status": result.status})
            state = self.runtime.save_candidate(handle, summary=result.final_text or result.stderr_tail or f"Worker {result.status}; partial files retained.",
                advisory={**state.advisory, "benchmark": progress})
        finally:
            handle.close()
        if result.final_text:
            state = self._register_checks(state, result.final_text)
        return state, wrapped.stopped or ("worker_" + result.status if result.status != "completed" else None)

    def _observe(self, state: WorkSnapshot) -> WorkSnapshot:
        return self._progress(state, review_progress.observe(_progress(state), state))

    def _feedback_due(self, state: WorkSnapshot) -> bool:
        progress = _progress(state)
        since = _count(progress, "worker_turns") - _count(progress, "last_feedback_attempt_turn")
        priority = any(item.get('pending') and item.get('priority') for item in review_progress.records(progress).values())
        return bool(priority or progress.get('milestone_ready') or self.config.review_each_round
                    or since >= self.config.review_interval)

    def _review(self, state: WorkSnapshot, budget: BenchBudget, window: BudgetWindow, *,
                purpose: str, full: bool = False, force: bool = False) -> tuple[WorkSnapshot, str | None]:
        deadline = budget.purpose_deadline(window, purpose)
        stop = budget.stop_reason(deadline, purpose=purpose)
        if stop:
            return state, stop
        wrapped = BudgetedAdapter(self.reviewer, budget, purpose=purpose, deadline=deadline)
        basis = _acceptance_input(state)
        feedback_basis = {**basis, "scope": purpose}
        if (not full and not force and _public_passed(state) and not review_progress.pending(_progress(state))
                and _progress(state).get("last_feedback_input") == feedback_basis):
            return state, None
        observations = {key: value for key, value in review_progress.records(_progress(state)).items()
                        if value.get('pending')}
        covered_keys = [key for key, value in observations.items() if not full or (
            state.verification is not None and value.get('evidence') == str(state.verification.artifact)
            and value.get('candidate_tree') == state.candidate.tree)]
        attempt: dict[str, object] = {'purpose': purpose, 'full': full, 'basis': basis,
            'feedback_basis': feedback_basis, 'tree': state.candidate.tree,
            'previous_review': str(state.review.artifact) if state.review else None,
            'keys': covered_keys}
        progress = review_progress.mark_attempt(_progress(state))
        progress['review_pending'] = attempt
        progress['review_attempts'] = _count(progress, 'review_attempts') + 1
        if purpose == 'diagnostic' and progress.get('ready'):
            progress['last_ready_failure_input'] = basis
        state = self._progress(state, progress)
        if full:
            state = self.runtime.accept_candidate(state.work_id, expected_revision=state.revision, reviewer=wrapped,
                timeout_seconds=self.config.turn_timeout_seconds, deadline=deadline)
        else:
            instruction = (
                "Diagnose the recorded public failure and check method validity. Propose a concrete next experiment; "
                "this is feedback, not permission to waive supplied checks or the original task."
                if purpose == "diagnostic" else
                "Review intermediate progress against the original task. Identify missing behavior and actionable next steps; this is feedback only."
            )
            state = self.runtime.review_candidate(state.work_id, expected_revision=state.revision, reviewer=wrapped,
                timeout_seconds=self.config.turn_timeout_seconds,
                context=(f"{instruction}\n{_CHECK_DECISION_HINT}\n{_CHECK_HINT}\n"
                    "Address these pending observations together. Prior candidate/evidence references are "
                    "historical diagnostic context, never current passing evidence. Inspect the referenced "
                    "recorded evidence where needed and distinguish resolved failures from remaining gaps.\n"
                    f"{json.dumps(observations, ensure_ascii=False)}"), deadline=deadline,
                verification_evidence=state.verification)
        # Persist useful completion separately from attempts. Recovery can finish
        # this acknowledgement from the completed Runtime review receipt.
        valid = _valid_review(state)
        state = self._complete_review(state, attempt, valid=valid,
            acceptance_finished=full and not _acceptance_incomplete(state, wrapped))
        return state, wrapped.stopped

    def _recover_review(self, state: WorkSnapshot) -> WorkSnapshot:
        attempt = _progress(state).get('review_pending')
        if not isinstance(attempt, dict) or state.review is None:
            return state
        if (str(state.review.artifact) == attempt.get('previous_review')
                or state.review.candidate.tree != attempt.get('tree')):
            return state
        valid = _valid_review(state)
        finished = bool(attempt.get('full')) and (valid or 'reviewer_protocol_error' in state.review.reasons)
        return self._complete_review(state, attempt, valid=valid, acceptance_finished=finished)

    def _complete_review(self, state: WorkSnapshot, attempt: dict[str, object], *,
                         valid: bool, acceptance_finished: bool) -> WorkSnapshot:
        progress = _progress(state)
        if acceptance_finished:
            progress['last_acceptance_input'] = attempt['basis']
        if valid and state.review is not None:
            progress = review_progress.mark_reviewed(progress, cast(list[str], attempt['keys']), str(state.review.artifact))
            progress['last_feedback_turn'] = _count(progress, 'worker_turns')
            if not attempt['full']:
                progress['last_feedback_input'] = attempt['feedback_basis']
            # A successful acceptance review is also useful feedback for cadence.
        if valid and state.review is not None and not attempt['full']:
            # Public operations validate the review receipt, check version and
            # dispositions. Invalid optional proposals do not trigger repair calls.
            text = state.review.feedback
            state = self._review_checks(state, text)
            state = self._register_checks(state, text)
        progress['review_pending'] = None
        # Registration diagnostics may have updated other advisory fields.
        latest = _progress(state)
        if 'diagnostics' in latest:
            progress['diagnostics'] = latest['diagnostics']
        return self._progress(state, progress)

    def _review_checks(self, state: WorkSnapshot, text: str) -> WorkSnapshot:
        decisions: list[dict[str, object]] = []
        for block in re.findall(r"<pact-check-decisions>\s*(.*?)\s*</pact-check-decisions>", text, re.DOTALL | re.IGNORECASE):
            try:
                value = json.loads(block)
                if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
                    raise ValueError('expected check decisions array')
                decisions.extend(value)
            except ValueError as exc:
                state = self._diagnostic(state, f'Ignored invalid check disposition: {exc}')
        if decisions:
            try:
                assert state.review is not None
                state = self.runtime.review_checks(state.work_id, expected_revision=state.revision,
                    decisions=decisions, review=state.review)
            except RuntimeContractError as exc:
                state = self._diagnostic(state, f'Ignored invalid check disposition: {exc}')
        return state

    def _already_accepted_input(self, state: WorkSnapshot) -> bool:
        return _progress(state).get("last_acceptance_input") == _acceptance_input(state)

    def _register_checks(self, state: WorkSnapshot, text: str) -> WorkSnapshot:
        explanation = re.sub(r"<pact-checks>.*?</pact-checks>", "", text, flags=re.DOTALL | re.IGNORECASE).strip()
        explanation = re.sub(r"(?im)^\s*PACT_(?:REVIEW_READY|COMPLETE|FAILED|CONTINUE)\s*$", "", explanation).strip()
        reason = explanation[:2000] or "Benchmark role proposed executable evidence for the fixed requirement."
        for block in re.findall(r"<pact-checks>\s*(.*?)\s*</pact-checks>", text, re.DOTALL | re.IGNORECASE):
            try:
                checks = json.loads(block)
                if not isinstance(checks, list):
                    raise ValueError("expected an array")
            except ValueError as exc:
                state = self._diagnostic(state, f"Ignored malformed check proposal: {exc}")
                continue
            for check in checks:
                if not isinstance(check, dict):
                    state = self._diagnostic(state, "Ignored check proposal: item must be an object.")
                    continue
                try:
                    state = self.runtime.register_checks(state.work_id, expected_revision=state.revision,
                        checks=[check], reason=reason)
                except RuntimeContractError as exc:
                    state = self._diagnostic(state, f"Ignored invalid check proposal: {exc}")
        if re.search(r"<pact-checks>", text, re.IGNORECASE) and not re.search(r"</pact-checks>", text, re.IGNORECASE):
            state = self._diagnostic(state, "Ignored incomplete check proposal; no format-repair call was made.")
        return state

    def _progress(self, state: WorkSnapshot, progress: dict[str, object]) -> WorkSnapshot:
        return self.runtime.update_advisory(state.work_id, expected_revision=state.revision,
            advisory={**state.advisory, "benchmark": progress})

    def _diagnostic(self, state: WorkSnapshot, text: str) -> WorkSnapshot:
        progress = _progress(state)
        diagnostics = cast(list[object], progress.get("diagnostics", []))
        return self._progress(state, {**progress, "diagnostics": [*diagnostics, text]})

    def _result(self, state: WorkSnapshot, reason: str, budget: BenchBudget) -> BenchResult:
        return BenchResult(state, reason, budget.snapshot(), budget.trial_dir)


def _can_continue(state: WorkSnapshot) -> bool:
    if state.status == "active":
        return True
    # A question blocks acceptance, but the public Runtime still permits
    # candidate execution. Continue only this known review-induced block;
    # pauses and invalid candidates are not unattended scheduling decisions.
    if (state.status != "blocked" or not state.unresolved_assumptions
            or state.review is None or state.review.candidate != state.candidate
            or "unresolved_user_questions" not in state.review.reasons):
        return False
    # Questions and namespace violations can coexist. Validate the sealed
    # candidate through the Kernel reader; never infer safety from review text
    # or turn a corrupt/missing evidence error into permission to run a model.
    return not read_work_candidate(state.candidate.artifact).namespace_violations


def _progress(state: WorkSnapshot) -> dict[str, object]:
    raw = state.advisory.get("benchmark", {"schema": _PROGRESS_SCHEMA})
    if not isinstance(raw, dict) or raw.get("schema") != _PROGRESS_SCHEMA:
        raise RuntimeContractError("Unsupported benchmark progress schema")
    return cast(dict[str, object], dict(raw))


def _count(progress: dict[str, object], name: str) -> int:
    value = progress.get(name, 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeContractError(f"Invalid benchmark {name}")
    return value


def _public_passed(state: WorkSnapshot) -> bool:
    return state.verification is not None and (state.verification.status == "passed"
        or state.verification.status == "unavailable" and state.policy.allow_unverified)


def _valid_review(state: WorkSnapshot) -> bool:
    if state.review is None:
        return False
    reasons = set(state.review.reasons)
    return not bool(reasons & {'reviewer_mutation', 'reviewer_output_missing',
        'reviewer_protocol_error', 'budget_exhausted'}) and bool(reasons & {
            'feedback_recorded', 'reviewer_failed', 'max_rounds', 'gate_complete',
        } or state.review.complete)


def _acceptance_input(state: WorkSnapshot) -> dict[str, object]:
    return {"candidate_tree": state.candidate.tree, "contract_revision": state.contract_revision,
        "checks": list(state.checks), "brief": state.acceptance_brief,
        "verification_status": state.verification.status if state.verification else None,
        "questions": list(state.unresolved_assumptions), "decisions": list(state.user_decisions)}


def _acceptance_incomplete(state: WorkSnapshot, adapter: BudgetedAdapter) -> bool:
    # Only classify this just-finished acceptance, whose adapter results and
    # public review belong to the same operation. A persisted review can later
    # be replaced by feedback, so it cannot reinterpret an older dedupe marker.
    if state.acceptance is not None or _review_mutated(state):
        return False
    initial = adapter.results[0] if adapter.results else None
    # A completed repair needs its structured verdict, not another review body.
    return (not adapter.results or adapter.stopped is not None
        or state.review is not None and "budget_exhausted" in state.review.reasons
        or initial is not None and initial.status == "completed" and not initial.final_text.strip()
        or any(turn.status in {"timeout", "output_missing", "spawn_failed"}
            for turn in adapter.results))


def _review_mutated(state: WorkSnapshot) -> bool:
    return state.review is not None and any("reviewer_mutation" in reason for reason in state.review.reasons)


def _worker_prompt(state: WorkSnapshot) -> str:
    verification = state.verification
    evidence = ({"status": verification.status, "command": verification.command, "exit_code": verification.exit_code,
        "stdout_tail": verification.stdout_tail, "stderr_tail": verification.stderr_tail,
        "check_results": verification.check_results} if verification else None)
    review = ({"feedback": state.review.feedback, "reasons": state.review.reasons,
        "candidate_tree": state.review.candidate.tree} if state.review and not _review_mutated(state) else None)
    return (
        "Continue the fixed benchmark task in this isolated checkout. Preserve accumulated useful work. "
        "The original requirement below remains authoritative. No interactive user is available; "
        "document uncertainty without inventing an answer or weakening the task. "
        "Investigate unresolved questions using the original task and repository evidence, and continue useful work within the budget. "
        "Do not wait for a user reply or invent authorization for a requirement decision. "
        "Repair concrete public failures before expanding implementation. Existing green tests do not prove new requirements. "
        "When the full task is ready for independent acceptance, optionally put PACT_REVIEW_READY on its own line.\n\n"
        "For an intermediate functional milestone, optionally put PACT_MILESTONE_READY on its own line for feedback. "
        "Both hints are advisory; their absence is valid.\n\n"
        f"Original requirement:\n{json.dumps(state.contract, ensure_ascii=False, indent=2)}\n"
        f"Explicit constraints:\n{json.dumps(state.constraints, ensure_ascii=False)}\n"
        f"Recorded User decisions:\n{json.dumps(state.user_decisions, ensure_ascii=False)}\n"
        f"Acceptance brief (advisory):\n{json.dumps(state.acceptance_brief, ensure_ascii=False)}\n"
        f"Registered checks:\n{json.dumps(state.checks, ensure_ascii=False)}\n"
        f"Latest deterministic result:\n{json.dumps(evidence, ensure_ascii=False)}\n"
        f"Latest independent feedback:\n{json.dumps(review, ensure_ascii=False)}\n"
        f"Pending diagnostic observations (do not wait for approval):\n{json.dumps(review_progress.records(_progress(state)), ensure_ascii=False)}\n"
        f"Saved handoff:\n{json.dumps(state.handoff, ensure_ascii=False)}\n"
        f"Unresolved questions:\n{json.dumps(state.unresolved_assumptions, ensure_ascii=False)}\n\n"
        f"{_CHECK_HINT}\n{build_runtime_handoff_instructions()}"
    )
