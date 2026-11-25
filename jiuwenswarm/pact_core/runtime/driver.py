"""Optional Worker/Reviewer workflow built exclusively on the public work API.

Plan tasks and readiness guide scheduling. Only full-work acceptance and delivery
can change the source repository; this driver owns no canonical completion rule.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
import re
from typing import TYPE_CHECKING

from ..adapters.base import AgentAdapter, TurnRequest
from ..prompts import build_runtime_handoff_instructions
from .configuration import DriverConfiguration
from .schemas import RuntimeContractError, RuntimeStateError

if TYPE_CHECKING:
    from .control_plane import WorkRuntime
    from .types import WorkSnapshot


class DefaultDriver:
    def __init__(self, runtime: WorkRuntime, worker: AgentAdapter, reviewer: AgentAdapter) -> None:
        self.runtime = runtime
        self.worker = worker
        self.reviewer = reviewer

    def run(
        self, work_id: str, *, expected_revision: int | None = None,
        max_turns: int | None = None, worktree_root: Path | None = None,
        stop_requested: Callable[[], bool] | None = None,
    ) -> WorkSnapshot:
        """Run until a workflow yield or cooperative operation-boundary stop.

        An admitted operation finishes and retains its result. In particular,
        cancellation cannot undo an already admitted delivery.
        """
        state = self._read(work_id, expected_revision, worktree_root)
        config = _configuration(state)
        budget = config.max_rounds if max_turns is None else max_turns
        if isinstance(budget, bool) or not isinstance(budget, int) or budget < 1:
            raise RuntimeContractError("max_turns must be a positive integer")
        if state.status == "active":
            progress = _progress(state)
            if progress.get("yield_reason") == "invocation_stopped":
                # The old invocation ended; its stop request is not a durable
                # instruction to terminate this caller's later review-only turn.
                state = self.runtime.update_advisory(state.work_id, expected_revision=state.revision,
                    advisory={**state.advisory, "default_driver": {**progress, "yield_reason": None}})
        unchanged = 0
        for _ in range(budget):
            if state.status != "active":
                return state
            before = state.candidate.tree if state.candidate else None
            before_verification = state.verification.status if state.verification else None
            state = self._advance(state, config, stop_requested=stop_requested)
            if state.status != "active":
                return state
            progress = _progress(state)
            if progress.get("yield_reason") in {"worker_incomplete", "invocation_stopped"}:
                return state
            after = state.candidate.tree if state.candidate else None
            verification_improved = (before_verification in {"failed", "error", "unavailable"}
                and state.verification is not None and state.verification.status == "passed")
            unchanged = unchanged + 1 if before is not None and before == after and not verification_improved else 0
            if unchanged >= config.max_unchanged_rounds:
                return self._yield(state, "unchanged")
        return self._yield(state, "max_rounds")

    def advance(
        self, work_id: str, *, expected_revision: int | None = None,
        max_turns: int | None = None, worktree_root: Path | None = None,
        stop_requested: Callable[[], bool] | None = None,
    ) -> WorkSnapshot:
        """Perform at most one Worker turn, preserving the same API as run."""
        if max_turns is not None and max_turns != 1:
            raise RuntimeContractError("advance accepts at most one Worker turn")
        return self.run(work_id, expected_revision=expected_revision,
                        max_turns=1, worktree_root=worktree_root, stop_requested=stop_requested)

    def _read(self, work_id: str, expected_revision: int | None, worktree_root: Path | None) -> WorkSnapshot:
        # Worktree placement is a runtime construction setting, never a second
        # execution path. The compatibility argument must agree when supplied.
        if worktree_root is not None:
            configured = self.runtime.worktree_root
            if configured is None or Path(configured).resolve() != worktree_root.resolve():
                raise RuntimeContractError("worktree_root must match the WorkRuntime constructor")
        state = self.runtime.read_work(work_id)
        if expected_revision is not None and state.revision != expected_revision:
            raise RuntimeStateError("stale work revision")
        return state

    def _advance(self, state: WorkSnapshot, config: DriverConfiguration, *,
                 stop_requested: Callable[[], bool] | None) -> WorkSnapshot:
        if stop_requested is not None and stop_requested():
            return self._yield(state, "invocation_stopped")
        if state.acceptance is not None:
            return self.runtime.deliver(state.work_id, expected_revision=state.revision, acceptance=state.acceptance)
        if state.review_requested and state.candidate is not None:
            state = self.runtime.verify_candidate(state.work_id, expected_revision=state.revision)
            if stop_requested is not None and stop_requested():
                return self._yield(state, "invocation_stopped")
            if _public_passed(state):
                return self._review(state, config, scope="whole-work", full=True, force=True,
                                    stop_requested=stop_requested)
        progress = _progress(state)
        selected, all_tasks = _select_task(state, progress)
        prompt = _worker_prompt(state, selected)
        if stop_requested is not None and stop_requested():
            return self._yield(state, "invocation_stopped")
        handle = self.runtime.begin_execution(state.work_id, expected_revision=state.revision, context=prompt)
        try:
            result = self.worker.run_turn(TurnRequest(
                role="worker", prompt=prompt, workdir=handle.workdir,
                timeout_seconds=config.turn_timeout_seconds, artifact_dir=handle.artifact_dir,
            ))
            previous_turns = progress.get("worker_turns", 0)
            assert isinstance(previous_turns, int)
            progress.update({
                "selected": selected["id"],
                "worker_turns": previous_turns + 1,
                "worker_status": result.status,
                "yield_reason": None,
            })
            incomplete = result.status != "completed" or not result.final_text.strip()
            if incomplete:
                progress["yield_reason"] = "worker_incomplete"
            state = self.runtime.save_candidate(handle, summary=result.final_text,
                advisory={**state.advisory, "default_driver": progress})
        finally:
            handle.close()
        if stop_requested is not None and stop_requested():
            return self._yield(state, "invocation_stopped")
        if incomplete:
            return state
        state = self.runtime.verify_candidate(state.work_id, expected_revision=state.revision)
        if stop_requested is not None and stop_requested():
            return self._yield(state, "invocation_stopped")
        if not _public_passed(state):
            return state
        ready = re.search(r"(?im)^\s*PACT_REVIEW_READY\s*$", result.final_text) is not None
        if ready:
            previous_done = progress["done_missions"]
            assert isinstance(previous_done, list)
            done = list(previous_done)
            if selected["id"] not in done:
                done.append(selected["id"])
            progress.update({"done_missions": done, "selected": None})
            state = self.runtime.update_advisory(state.work_id, expected_revision=state.revision,
                advisory={**state.advisory, "default_driver": progress})
            if all(task["id"] in done for task in all_tasks):
                return self._review(state, config, scope="whole-work", full=True,
                                    stop_requested=stop_requested)
        turns = progress["worker_turns"]
        assert isinstance(turns, int)
        if (ready or state.review_requested or config.execution_policy == "review_each_round"
                or config.review_interval is not None and turns % config.review_interval == 0):
            return self._review(state, config, scope=f"task:{selected['id']}", full=False,
                                stop_requested=stop_requested)
        return state

    def _review(self, state: WorkSnapshot, config: DriverConfiguration, *,
                scope: str, full: bool, force: bool = False,
                stop_requested: Callable[[], bool] | None) -> WorkSnapshot:
        if stop_requested is not None and stop_requested():
            return self._yield(state, "invocation_stopped")
        progress = _progress(state)
        review_input = {"tree": state.candidate.tree, "scope": scope,
                        "verification_status": state.verification.status if state.verification else None,
                        "user_decisions": len(state.user_decisions)}
        if not force and progress.get("last_review_input") == review_input:
            return state
        operation = self.runtime.accept_candidate if full else self.runtime.review_candidate
        state = operation(state.work_id, expected_revision=state.revision,
                          reviewer=self.reviewer, timeout_seconds=config.turn_timeout_seconds)
        stopped = stop_requested is not None and stop_requested()
        if not _public_passed(state):
            # Full public checks can fail before any Reviewer is invoked. They
            # may recover without changing the tree and are not model retries.
            return self._yield(state, "invocation_stopped") if stopped else state
        if state.acceptance is not None:
            if stopped:
                return self._yield(state, "invocation_stopped")
            return self.runtime.deliver(state.work_id, expected_revision=state.revision, acceptance=state.acceptance)
        # Finish the admitted review's scheduling record before yielding, so a
        # resumed invocation does not repeat feedback for the identical input.
        state = self.runtime.update_advisory(state.work_id, expected_revision=state.revision,
            advisory={**state.advisory, "default_driver": {**progress, "last_review_input": review_input}})
        if stopped or stop_requested is not None and stop_requested():
            return self._yield(state, "invocation_stopped")
        return state

    def _yield(self, state: WorkSnapshot, reason: str) -> WorkSnapshot:
        if state.status != "active" and not (state.status == "blocked" and reason == "invocation_stopped"):
            return state
        return self.runtime.update_advisory(state.work_id, expected_revision=state.revision,
            advisory={**state.advisory, "default_driver": {**_progress(state), "yield_reason": reason}})


def _configuration(state: WorkSnapshot) -> DriverConfiguration:
    return DriverConfiguration.parse(state.advisory.get("driver_configuration", DriverConfiguration().payload()))


def _progress(state: WorkSnapshot) -> dict[str, object]:
    raw = state.advisory.get("default_driver", {})
    if not isinstance(raw, dict):
        raise RuntimeContractError("default_driver advisory must be an object")
    if raw.get("contract_revision") != state.contract_revision:
        return {"contract_revision": state.contract_revision, "done_missions": [], "worker_turns": 0}
    done = raw.get("done_missions", [])
    turns = raw.get("worker_turns", 0)
    if (not isinstance(done, list) or any(not isinstance(item, str) for item in done)
            or isinstance(turns, bool) or not isinstance(turns, int) or turns < 0):
        raise RuntimeContractError("invalid default_driver advisory progress")
    return dict(raw)


def _select_task(state: WorkSnapshot, progress: dict[str, object]) -> tuple[dict[str, object], list[dict[str, object]]]:
    plan = state.advisory.get("plan", {})
    raw = plan.get("missions", []) if isinstance(plan, dict) else []
    tasks: list[dict[str, object]] = []
    if isinstance(raw, list):
        for task in raw:
            if (not isinstance(task, dict) or not isinstance(task.get("id"), str)
                    or not isinstance(task.get("objective"), str)
                    or not isinstance(task.get("dependencies", []), list)):
                raise RuntimeContractError("invalid advisory plan task")
            tasks.append(task)
    if not tasks:
        tasks = [{"id": "whole-work", "objective": "Implement the full user Goal and hard acceptance criteria.", "dependencies": []}]
    done = progress["done_missions"]
    assert isinstance(done, list)
    for task in tasks:
        dependencies = task.get("dependencies", [])
        assert isinstance(dependencies, list)
        if task["id"] not in done and all(dependency in done for dependency in dependencies):
            return task, tasks
    if all(task["id"] in done for task in tasks):
        return {"id": tasks[-1]["id"], "objective": "Resolve full-work verification and review feedback.", "dependencies": []}, tasks
    raise RuntimeContractError("advisory plan has no eligible task; revise its dependencies")


def _public_passed(state: WorkSnapshot) -> bool:
    return state.verification is not None and (state.verification.status == "passed"
        or state.verification.status == "unavailable" and state.policy.allow_unverified)


def _worker_prompt(state: WorkSnapshot, task: dict[str, object]) -> str:
    feedback = (f"Reviewed checkpoint: {state.review.candidate.checkpoint}\n"
                f"Reviewed tree: {state.review.candidate.tree}\n"
                f"Core review/acceptance reasons: {json.dumps(state.review.reasons)}\n{state.review.feedback}"
                if state.review else "")
    verification = state.verification
    public = ({"status": verification.status, "command": verification.command, "exit_code": verification.exit_code,
               "stdout_tail": verification.stdout_tail, "stderr_tail": verification.stderr_tail,
               "check_results": verification.check_results}
              if verification else None)
    return (
        "You are the Worker continuing durable work in this isolated checkout.\n"
        "The user Goal and hard acceptance criteria below govern final acceptance.\n"
        "The selected plan task is advisory implementation guidance. Preserve accumulated work.\n"
        "Address public failures and substantive review findings; suggestions do not add user requirements.\n"
        "Provide a concise account of changes and remaining work. Optionally put PACT_REVIEW_READY on a line\n"
        "when this selected task is ready. This hint does not establish acceptance or authorize delivery.\n\n"
        f"User contract:\n{json.dumps(state.contract, ensure_ascii=False, indent=2)}\n\n"
        f"User constraints:\n{json.dumps(state.constraints, ensure_ascii=False)}\n\n"
        f"Advisory acceptance brief:\n{json.dumps(state.acceptance_brief, ensure_ascii=False)}\n\n"
        f"Registered derived checks:\n{json.dumps(state.checks, ensure_ascii=False)}\n\n"
        f"Selected task:\n{json.dumps(task, ensure_ascii=False, indent=2)}\n\n"
        f"Public verification command: {state.policy.verify_command or 'unavailable'}\n"
        f"Full-work public verification: {state.policy.closeout_verify_command or state.policy.verify_command or 'unavailable'}\n"
        f"Latest public verification:\n{json.dumps(public, ensure_ascii=False)}\n\n"
        f"Review feedback:\n{feedback}\n\n"
        f"Handoff:\n{json.dumps(state.handoff, ensure_ascii=False)}\n"
        f"User answers:\n{json.dumps(state.user_decisions, ensure_ascii=False)}\n"
        f"Unresolved user questions:\n{json.dumps(state.unresolved_assumptions, ensure_ascii=False)}\n"
        f"\n{build_runtime_handoff_instructions()}\n"
    )
