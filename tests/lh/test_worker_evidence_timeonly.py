# Copyright (c) 2026 Chrys. All rights reserved.

"""Worker advice and the original Harbor deadline coexist in one frozen work."""

from __future__ import annotations

import json
import time
from dataclasses import replace

import pytest
from jiuwenswarm.pact_core.runtime import WorkRuntime
from test_suite_flow import plugin, trial

from jiuwenswarm.lh import LongHorizonBenchmark
from jiuwenswarm.lh.worker_evidence import WORKER_EVIDENCE_GUIDANCE_VERSION, worker_evidence_guidance


@pytest.mark.parametrize("control_profile", ["bounded", "legacy"])
@pytest.mark.asyncio
async def test_timeonly_worker_advice_keeps_both_frozen_identities_and_resume_guard(
    tmp_path, git_repo_factory, monkeypatch, control_profile
):
    original_start = time.time()
    # Leave room for real Git worktrees during concurrent CI. This test checks
    # origin/deadline identity and the resume guard, not wall-clock exhaustion.
    task_seconds = 600
    args = replace(
        trial(tmp_path, git_repo_factory, control_profile=control_profile),
        max_turns=None,
        budget_seconds=task_seconds,
        deadline_only_budget_seconds=task_seconds,
        budget_started_at_unix=original_start,
        turn_timeout_seconds=task_seconds,
    )
    calls = []
    outcome = await LongHorizonBenchmark(args, plugin=plugin(calls, p1_ready=False, later_ready=True)).run()
    assert not outcome.internal_failure
    report = json.loads(outcome.selection.read_text())
    assert report["internal_accepted"] and report["external_grade"]["status"] == "not_run"
    assert report["budget"]["started_at"] == original_start
    assert report["budget"]["deadline"] == original_start + task_seconds
    state = WorkRuntime(args.workdir).read_work(outcome.work_id)
    frozen = state.advisory["third-agent_long_horizon_benchmark"]
    assert frozen["max_turns"] is None
    assert frozen["turn_budget_policy"] == "original_global_deadline_v1"
    assert frozen["deadline_only_budget_seconds"] == task_seconds
    assert frozen["worker_evidence_guidance"] == WORKER_EVIDENCE_GUIDANCE_VERSION
    workers = [prompt for label, _, prompt in calls if label in ("worker", "[SUITE_P1_REPAIR]")]
    assert len(workers) == 3 and all(prompt.endswith(worker_evidence_guidance()) for prompt in workers)
    assert all(
        worker_evidence_guidance() not in prompt
        for label, _, prompt in calls
        if label not in ("worker", "[SUITE_P1_REPAIR]")
    )
    calls.clear()
    monkeypatch.setattr("jiuwenswarm.lh.controller.WORKER_EVIDENCE_GUIDANCE_VERSION", "worker-evidence-triage/v2")
    resumed = replace(args, work_id=state.work_id, expected_revision=state.revision)
    refused = await LongHorizonBenchmark(resumed, plugin=plugin(calls)).run()
    assert refused.internal_failure and not calls
