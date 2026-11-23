# Copyright (c) 2026 Chrys. All rights reserved.

"""Worker-only evidence advice cannot clear Core questions or guide the Reviewer."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from pact_core.runtime import WorkRuntime
from test_suite_flow import SuiteAgent, plugin, trial

from jiuwenswarm.lh import Capabilities, ExecutionBinding, LongHorizonBenchmark
from jiuwenswarm.lh.plugins.generic import GenericPlugin
from jiuwenswarm.lh.worker_evidence import WORKER_EVIDENCE_GUIDANCE_VERSION, worker_evidence_guidance


@pytest.mark.parametrize("control_profile", ["bounded", "legacy"])
@pytest.mark.asyncio
async def test_advice_reaches_three_worker_stages_without_changing_other_role_inputs(
    tmp_path, git_repo_factory, control_profile
):
    args = trial(tmp_path, git_repo_factory, control_profile=control_profile)
    calls = []
    outcome = await LongHorizonBenchmark(args, plugin=plugin(calls, p1_ready=False, later_ready=True)).run()
    report = json.loads(outcome.selection.read_text())
    assert not outcome.internal_failure, report["internal_failure"]
    assert report["internal_accepted"] and report["external_grade"]["status"] == "not_run"
    guidance = worker_evidence_guidance()
    workers = [prompt for label, _, prompt in calls if label in ("worker", "[SUITE_P1_REPAIR]")]
    assert len(workers) == 3
    assert "first implementation" in workers[0]
    assert workers[1].startswith("[SUITE_P1_REPAIR]")
    assert "Continue the fixed benchmark task" in workers[2]
    assert all(prompt.endswith(guidance) for prompt in workers)
    for label, _, prompt in calls:
        if label not in ("worker", "[SUITE_P1_REPAIR]"):
            assert guidance not in prompt, label
    suite = report["long_horizon"]["suite"]
    assert guidance not in suite["brief"]
    assert (args.output / suite["brief_path"]).read_text() == suite["brief"]
    assert guidance not in json.dumps(suite["goal_contract"])


@pytest.mark.parametrize("material", [True, False])
@pytest.mark.asyncio
async def test_original_material_question_is_not_cleared_by_later_handoff_or_complete_reviewer(
    tmp_path, git_repo_factory, material
):
    args = replace(trial(tmp_path, git_repo_factory), allow_user_interaction=True)
    calls = []
    assumption = {
        "requirement_ids": ["plan-fulfilled"],
        "assumption": "A named shared delivery branch needs the controller's permission.",
        "basis": "The Worker has no authority to mutate shared refs.",
        "material": material,
    }

    class QuestionAgent(SuiteAgent):
        def run_turn(self, request):
            response = super().run_turn(request)
            if self.role == "worker":
                assumptions = [] if request.prompt.startswith("[SUITE_P1_REPAIR]") else [assumption]
                return replace(
                    response,
                    final_text=response.final_text
                    + "\n<pact-handoff>"
                    + json.dumps({"assumptions": assumptions})
                    + "</pact-handoff>",
                )
            return response

    subject = GenericPlugin(
        name="third-agent",
        capabilities=Capabilities(True, True, True, True),
        resolve=lambda workspace, args: ExecutionBinding("fixture-suite-v1", {"runtime": "scripted-suite"}),
        adapter_factory=lambda role, **kwargs: QuestionAgent(role, calls, expect_memory=False),
    )
    outcome = await LongHorizonBenchmark(args, plugin=subject).run()
    report = json.loads(outcome.selection.read_text())
    assert not outcome.internal_failure, report["internal_failure"]
    state = WorkRuntime(args.workdir).read_work(outcome.work_id)
    assert not state.user_decisions
    assert state.handoff["assumptions"] == []
    if material:
        assert report["stop_reason"] == "acceptance_blocked_unresolved_questions"
        assert not report["internal_accepted"] and state.acceptance is None
        assert len(state.unresolved_assumptions) == 1
        question = state.unresolved_assumptions[0]
        assert {key: question[key] for key in assumption} == assumption
        revisions = sorted((state.artifacts / "work-state-revisions").glob("*.json"))
        persisted = [json.loads(path.read_text())["unresolved_assumptions"] for path in revisions]
        assert all(row[0] == question for row in persisted if row)
        assert state.review is not None and not state.review.complete
        assert set(state.review.reasons) == {"gate_complete", "unresolved_user_questions"}
    else:
        assert not state.unresolved_assumptions
        assert report["internal_accepted"]


@pytest.mark.asyncio
async def test_guidance_version_is_bound_for_resume(tmp_path, git_repo_factory, monkeypatch):
    args = trial(tmp_path, git_repo_factory)
    calls = []
    outcome = await LongHorizonBenchmark(args, plugin=plugin(calls)).run()
    state = WorkRuntime(args.workdir).read_work(outcome.work_id)
    inputs = state.advisory["third-agent_long_horizon_benchmark"]
    assert inputs["worker_evidence_guidance"] == WORKER_EVIDENCE_GUIDANCE_VERSION
    calls.clear()
    monkeypatch.setattr("jiuwenswarm.lh.controller.WORKER_EVIDENCE_GUIDANCE_VERSION", "worker-evidence-triage/v2")
    resumed = replace(args, work_id=state.work_id, expected_revision=state.revision)
    result = await LongHorizonBenchmark(resumed, plugin=plugin(calls)).run()
    assert result.internal_failure is not None
    assert not calls
