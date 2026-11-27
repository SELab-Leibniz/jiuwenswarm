"""Unattended question resolution on the shared controller for both backends."""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

import pytest
from jiuwenswarm.pact_core.review_decision import parse_review_decision
from jiuwenswarm.pact_core.runtime import WorkRuntime
from jiuwenswarm.pact_core.schemas import AdapterProbe, TurnResult

from jiuwenswarm.lh import BenchmarkArguments, Capabilities, ExecutionBinding, LongHorizonBenchmark
from jiuwenswarm.lh.plugins.generic import GenericPlugin
from jiuwenswarm.lh.side_calls import SideCallIntegrityError

QUESTION = "A named delivery branch needs a controller decision."
HANDOFF = (
    "<pact-handoff>"
    + json.dumps(
        {
            "assumptions": [
                {
                    "requirement_ids": ["plan-fulfilled"],
                    "assumption": QUESTION,
                    "basis": "Only the controller may modify shared refs.",
                    "material": True,
                }
            ]
        }
    )
    + "</pact-handoff>"
)


class QuestionAgent:
    budget_failure = None
    configuration_failure = None

    def __init__(self, role, calls, state, **context):
        self.id = "question-" + role
        self.role, self.calls, self.state = role, calls, state
        self.context = context
        self.state.setdefault("worker", 0)

    def probe(self):
        return AdapterProbe(adapter_id=self.id, available=True, version="scripted-v1")

    def _question_ids(self, prompt: str) -> list[str]:
        try:
            block = prompt.split("Questions:\n", 1)[1].split("\n\nRecorded evidence:", 1)[0]
            questions = json.loads(block)
        except (IndexError, ValueError):
            return []
        return [str(row["id"]) for row in questions if isinstance(row, dict) and row.get("id")]

    def run_turn(self, request):
        self.calls.append((self.role, request.prompt, request.workdir))
        if "[SUITE_COVERAGE_AUDIT]" in request.prompt:
            self.state["audit_calls"] = self.state.get("audit_calls", 0) + 1
            text = json.dumps(
                {
                    "verdict": "clean",
                    "checked": [
                        {
                            "requirement": "Create greeting.txt containing hello.",
                            "verified": True,
                            "evidence": "executed the supplied public check",
                        }
                    ],
                    "gaps": [],
                }
            )
            return TurnResult(
                status="completed",
                final_text="<pact-coverage-audit>" + text + "</pact-coverage-audit>",
                output_source="scripted",
                exit_code=0,
            )
        if request.prompt.startswith("Read the frozen"):
            assert not (request.workdir / "greeting.txt").exists()
            return TurnResult(
                status="completed",
                final_text="Place the greeting at the repository root; preserve existing behavior.",
                output_source="scripted",
                exit_code=0,
            )
        if "[UNATTENDED_QUESTION_RESOLUTION]" in request.prompt:
            self.state["resolution_calls"] = self.state.get("resolution_calls", 0) + 1
            self.state.setdefault("resolution_prompts", []).append(request.prompt)
            replies = self.state.get("resolution_replies", [])
            template = (
                replies.pop(0)
                if replies
                else {
                    "kind": "user_decision",
                    "assumption": "Choose the repository-root greeting and preserve all other behavior.",
                    "basis": "original task plus frozen baseline",
                }
            )
            rows = []
            for question_id in self._question_ids(request.prompt):
                rows.append({"id": question_id, **template})
            return TurnResult(
                status="completed",
                final_text=json.dumps({"resolutions": rows}),
                output_source="scripted",
                exit_code=0,
            )
        if self.role == "worker":
            self.state["worker"] += 1
            if self.state["worker"] == 1:
                (request.workdir / "greeting.txt").write_text("draft\n", encoding="utf-8")
                text = "Draft. " + HANDOFF
            else:
                (request.workdir / "greeting.txt").write_text("hello\n", encoding="utf-8")
                text = "Fixed.\nPACT_REVIEW_READY\n"
            return TurnResult(status="completed", final_text=text, output_source="scripted", exit_code=0)
        if request.review_mode == "acceptance":
            text = json.dumps({"verdict": "complete", "reason": "Supplied checks and original task verified"})
            return TurnResult(
                status="completed",
                final_text=text,
                output_source="scripted",
                exit_code=0,
                review_decision=parse_review_decision(text, existed=True),
            )
        return TurnResult(
            status="completed",
            final_text="Change draft to hello.",
            output_source="scripted",
            exit_code=0,
        )

    async def cancel_current_turn(self):
        self.context["abort"].set()


def trial(tmp_path, git_repo_factory, *, interaction=False, max_turns=4, **overrides):
    workspace = git_repo_factory(tmp_path / "repo")
    task = tmp_path / "task.md"
    task.write_text("Create greeting.txt containing hello.\nPreserve all other existing behavior.\n", encoding="utf-8")
    verify = shlex.join(
        [sys.executable, "-c", "from pathlib import Path; assert Path('greeting.txt').read_text().strip() == 'hello'"]
    )
    values = dict(
        workflow="benchmark",
        task=task,
        workdir=workspace,
        output=tmp_path / "output",
        task_id="question-task",
        trial_id="question-trial",
        budget_seconds=600.0,
        turn_timeout_seconds=60.0,
        max_turns=max_turns,
        max_role_calls=40,
        review_interval=1,
        verify_command=verify,
        worktree_root=tmp_path / "worktrees",
        model=None,
        allow_user_interaction=interaction,
    )
    values.update(overrides)
    return BenchmarkArguments(**values)


def plugin(name, calls, state):
    return GenericPlugin(
        name=name,
        capabilities=Capabilities(True, True, True, True),
        resolve=lambda workspace, args: ExecutionBinding("fixture-v1", {"runtime": "unattended-test"}),
        adapter_factory=lambda role, **context: QuestionAgent(role, calls, state, **context),
    )


async def run_case(tmp_path, git_repo_factory, name, state, **overrides):
    args = trial(tmp_path, git_repo_factory, **overrides)
    outcome = await LongHorizonBenchmark(args, plugin=plugin(name, [], state)).run()
    assert outcome.selection is not None
    return (
        args,
        outcome,
        json.loads(outcome.selection.read_text()),
        WorkRuntime(args.workdir).read_work(outcome.work_id),
    )


@pytest.mark.asyncio
async def test_unrecovered_integrity_failure_in_unattended_resolution_is_fatal(
    tmp_path, git_repo_factory
):
    state = {}
    value = plugin("chrys", [], state)
    original = value.investigate

    async def investigate(context, prompt, *, stage):
        if "[UNATTENDED_QUESTION_RESOLUTION]" in prompt:
            raise SideCallIntegrityError(
                "read-only side call mutation could not be recovered",
                changed_paths=["src/app.py"],
                branch="git_state",
                checks={"content_hash": True, "git_state": True},
                recovery={"action": "restore_captured_view", "verified": False, "result": "failed"},
                recovered=False,
                processes_terminated=True,
            )
        return await original(context, prompt, stage=stage)

    value.investigate = investigate
    args = trial(tmp_path, git_repo_factory)
    outcome = await LongHorizonBenchmark(args, plugin=value).run()

    assert outcome.internal_failure is True
    assert outcome.stop_reason == "integrity_failure"
    assert outcome.candidate_available is False, "an integrity failure must refuse export"


@pytest.mark.parametrize("backend", ["chrys", "jiuwenswarm"])
@pytest.mark.asyncio
async def test_persistent_questions_are_resolved_then_accepted(tmp_path, git_repo_factory, backend):
    state = {
        "resolution_replies": [
            {
                "kind": "evidence_resolved",
                "resolution": "The task requires the greeting at the repository root.",
                "assumption": "Use the repository root and preserve all other files.",
                "basis": "original task plus frozen baseline",
            }
        ]
    }
    args, outcome, selection, work = await run_case(tmp_path, git_repo_factory, backend, state)

    assert not outcome.internal_failure, selection["internal_failure"]
    assert selection["stop_reason"] != "acceptance_blocked_unresolved_questions"
    assert selection["internal_accepted"] and selection["delivered"]
    assert selection["external_grade"] == {"status": "not_run", "reward": None}
    assert state["resolution_calls"] == 1
    assert selection["long_horizon"]["assumptions"]
    assert selection["long_horizon"]["assumptions"][0]["recorded_as"] == "unattended_policy_assumption"
    assert selection["long_horizon"]["resolution_round_count"] >= 1
    assert not selection["long_horizon"]["unresolved_questions"]
    assert any(
        call["purpose"] == "clarification:unattended-question-resolution-1" for call in selection["budget"]["calls"]
    )
    assert work.acceptance is not None and work.unresolved_assumptions == ()
    assert (args.workdir / "greeting.txt").read_text() == "hello\n"
    assert (Path(selection["export"]["snapshot"]) / "greeting.txt").read_text() == "hello\n"


@pytest.mark.parametrize("backend", ["chrys", "jiuwenswarm"])
@pytest.mark.asyncio
async def test_early_audit_triggers_after_unattended_resolution(tmp_path, git_repo_factory, backend):
    state = {
        "resolution_replies": [
            {
                "kind": "evidence_resolved",
                "resolution": "The task requires the greeting at the repository root.",
                "assumption": "Use the repository root and preserve all other files.",
                "basis": "original task plus frozen baseline",
            }
        ]
    }
    args, outcome, selection, work = await run_case(
        tmp_path,
        git_repo_factory,
        backend,
        state,
        budget_seconds=4 * 3600,
        early_audit_max_rounds=1,
    )

    assert not outcome.internal_failure, selection["internal_failure"]
    assert selection["internal_accepted"] and selection["delivered"]
    assert selection["long_horizon"]["unattended_resolution"]["status"] == "accepted"
    audit = selection["early_acceptance_audit"]
    assert audit is not None and audit["triggered"] is True
    assert audit["status"] == "accepted_fallback"
    assert audit["replaced_fallback"] is False
    assert state["audit_calls"] >= 1
    assert work.delivery is not None


@pytest.mark.parametrize("backend", ["chrys", "jiuwenswarm"])
@pytest.mark.asyncio
async def test_identical_verified_candidate_does_not_reopen_question_resolution(tmp_path, git_repo_factory, backend):
    state = {
        "resolution_replies": [
            {
                "kind": "implementation_gap",
                "resolution": "Implement the greeting file in the candidate.",
                "basis": "the candidate has no greeting file",
            }
        ]
    }
    args, outcome, selection, work = await run_case(
        tmp_path,
        git_repo_factory,
        backend,
        state,
        clarification_max_rounds=1,
    )

    assert not outcome.internal_failure, selection["internal_failure"]
    # The public check already passed when the resolver requested this repair.
    # The repeated Worker produces exactly the same tree and leaves the question open.
    assert outcome.stop_reason == "clarification_stalled"
    assert not selection["internal_accepted"] and not selection["delivered"]
    assert state["resolution_calls"] == 1
    rounds = selection["long_horizon"]["resolution_rounds"]
    assert [r["status"] for r in rounds] == ["worker_fix_required", "clarification_stalled"]
    details = [json.loads((args.output / r["record"]).read_bytes()) for r in rounds]
    assert details[0]["candidate_tree"] == details[1]["candidate_tree"] == work.candidate.tree
    assert rounds[0]["unresolved_question_ids"] == rounds[1]["unresolved_question_ids"]
    assert work.verification.status == "passed" and work.unresolved_assumptions


@pytest.mark.parametrize("backend", ["chrys", "jiuwenswarm"])
@pytest.mark.asyncio
async def test_interactive_mode_preserves_question_block_without_resolution(tmp_path, git_repo_factory, backend):
    state = {}
    args, outcome, selection, work = await run_case(
        tmp_path,
        git_repo_factory,
        backend,
        state,
        interaction=True,
        max_turns=2,
    )

    assert not outcome.internal_failure, selection["internal_failure"]
    assert selection["stop_reason"] == "acceptance_blocked_unresolved_questions"
    assert not selection["internal_accepted"] and not selection["delivered"]
    assert selection["export_status"] == "exported"
    assert state.get("resolution_calls", 0) == 0
    assert len(work.unresolved_assumptions) == 1
    assert not work.acceptance and work.delivery is None
    assert selection["long_horizon"]["assumptions"] == []
    assert not (args.workdir / "greeting.txt").exists()
