from __future__ import annotations

import json
import shlex
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_planning_resume import interrupted_plugin
from test_suite_flow import CONTRACT, PLAN, TASK, plugin, trial

from jiuwenswarm.lh import LongHorizonBenchmark
from jiuwenswarm.lh.plugins.native import NativeClarification
from jiuwenswarm.lh.reference_client_spec import PUBLIC_REFERENCE_COMMAND
from jiuwenswarm.lh.regression_handoff import merge_regressions, worker_regression_context
from jiuwenswarm.pact_core.runtime import WorkRuntime

RECORD = "name=regression-b031 source=public-reference first divergence=12; step 13 matched"


@pytest.mark.parametrize("profile", ["legacy", "bounded"])
@pytest.mark.asyncio
async def test_planning_and_independent_core_review_use_record_as_evidence_only(tmp_path, git_repo_factory, profile):
    args = replace(trial(tmp_path, git_repo_factory, control_profile=profile), external_regression_context=RECORD)
    calls = []
    outcome = await LongHorizonBenchmark(args, plugin=plugin(calls, p1_ready=False, later_ready=True)).run()
    report = json.loads(outcome.selection.read_text())
    assert not outcome.internal_failure, report["internal_failure"]
    suite = report["long_horizon"]["suite"]
    assert suite["goal_contract"] == CONTRACT
    assert suite["initial_plan"] == PLAN
    assert RECORD not in suite["brief"]
    assert args.task.read_text() == TASK
    by_role = {}
    for role, _, prompt in calls:
        by_role.setdefault(role, []).append(prompt)
    for role in ("[SUITE_INITIAL_PLAN]", "[SUITE_PLANNER]", "reviewer"):
        assert by_role[role]
        assert all(RECORD in prompt for prompt in by_role[role]), role
    for role in ("worker", "[SUITE_P1_REPAIR]"):
        assert by_role[role] and all(prompt.count(RECORD) == 1 for prompt in by_role[role])
    assert all("<pact-checks>" in prompt and "independently" in prompt.lower() for prompt in by_role["reviewer"])
    assert all(RECORD not in prompt for prompt in by_role["[SUITE_GOAL_CONTRACT]"])


@pytest.mark.asyncio
async def test_native_work_resume_carries_its_verified_public_failure(tmp_path, git_repo_factory):
    fixture = tmp_path / "case.json"
    fixture.write_text('{"input":{"stock":3},"checkpoints":[1,2]}\n')
    expected = "print('[{\"step\":1,\"state\":3},{\"step\":2,\"state\":3}]')"
    actual = (
        "from pathlib import Path; import json; "
        "value = 3 if Path('greeting.txt').read_text().strip() == 'hello' else 4; "
        "print(json.dumps([{'step': 1, 'state': 3}, {'step': 2, 'state': value}]))"
    )
    command = shlex.join([
        *shlex.split(PUBLIC_REFERENCE_COMMAND), "compare", str(fixture),
        "--reference", json.dumps([sys.executable, "-I", "-c", expected]),
        "--candidate", json.dumps([sys.executable, "-I", "-c", actual]),
    ])
    args = replace(trial(tmp_path, git_repo_factory), verify_command=command)
    calls = []
    first = await LongHorizonBenchmark(args, plugin=interrupted_plugin(calls)).run()
    report = json.loads(first.selection.read_text())
    assert not first.internal_failure and report["long_horizon"]["phase"] == "p0_saved"
    runtime = WorkRuntime(args.workdir, worktree_root=args.worktree_root)
    verified = runtime.verify_candidate(report["work_id"], expected_revision=report["revision"])
    context = worker_regression_context(merge_regressions(state=verified))
    assert verified.verification.status == "failed" and "checkpoint 2" in context
    assert all("Earlier public regression evidence" not in prompt for _, _, prompt in calls)

    resumed_calls = []
    resumed = await LongHorizonBenchmark(
        replace(args, work_id=verified.work_id, expected_revision=verified.revision),
        plugin=plugin(resumed_calls),
    ).run()
    after = json.loads(resumed.selection.read_text())
    assert not resumed.internal_failure and after["delivered"], after["internal_failure"]
    assert after["work_id"] == verified.work_id and after["budget"]["deadline"] == report["budget"]["deadline"]
    assert args.task.read_text() == TASK
    for stage in ("[SUITE_P1_REPAIR]", "reviewer"):
        prompts = [prompt for role, _, prompt in resumed_calls if role == stage]
        assert prompts and all(prompt.count(context) == 1 for prompt in prompts)


@pytest.mark.asyncio
async def test_native_public_check_generation_sees_record_with_original_criterion_ids():
    actual = []
    criteria = (SimpleNamespace(id="existing-ac"),)

    async def generate(prompt, *, acceptance_criteria):
        actual.append((prompt, acceptance_criteria))
        return SimpleNamespace(validate_coverage=lambda ids: None, model_dump=lambda **kw: {"status": "ready"}), {}

    subject = SimpleNamespace(
        _side_args=SimpleNamespace(external_regression_context=RECORD), requires_complete_public_review=False
    )
    pair = SimpleNamespace(
        goal_contract=SimpleNamespace(acceptance_criteria=criteria, model_dump_json=lambda **kw: "{}"),
        initial_plan=SimpleNamespace(model_dump_json=lambda **kw: "{}"),
    )
    result = await NativeClarification._bounded_public_verification(
        subject,
        SimpleNamespace(generate_public_verification=generate),
        SimpleNamespace(deadline=None),
        SimpleNamespace(),
        SimpleNamespace(rendered=TASK),
        SimpleNamespace(delta=""),
        pair,
    )
    assert result == {"status": "ready"}
    assert len(actual) == 1
    prompt, ids = actual[0]
    assert prompt.startswith("Original requirement:\n" + TASK)
    assert ids == criteria
    assert RECORD in prompt
