# Copyright (c) 2026 Chrys. All rights reserved.

"""Counterexamples from real suite window exhaustion, using no provider calls."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from pact_core.schemas import TurnResult
from test_suite_flow import SuiteAgent, plugin, trial

from jiuwenswarm.lh import LongHorizonBenchmark
from jiuwenswarm.lh.planning_checkpoint import PlanningCheckpoint, PlanningCheckpointError


def interrupted_plugin(calls, *, mutate=False, mutate_target=None):
    value = plugin(calls)

    class Interrupted(SuiteAgent):
        def run_turn(self, request):
            if request.prompt.startswith("[SUITE_PROPOSAL 1]"):
                calls.append(("[SUITE_PROPOSAL 1 timeout]", request.workdir, request.prompt))
                if mutate:
                    target = mutate_target() if mutate_target is not None else request.workdir
                    if target is not None:
                        (target / "src/README.md").write_text("modified by timed-out role")
                return TurnResult(status="timeout", final_text="", output_source="scripted", exit_code=1)
            return super().run_turn(request)

    value._factory = lambda role, **kwargs: Interrupted(role, calls)
    return value


@pytest.mark.asyncio
async def test_real_suite_timeout_resumes_completed_stages_without_replaying_p0(tmp_path, git_repo_factory):
    args, calls = trial(tmp_path, git_repo_factory), []
    first = await LongHorizonBenchmark(args, plugin=interrupted_plugin(calls)).run()
    before = json.loads(first.selection.read_text())
    assert not first.internal_failure, before["internal_failure"]
    assert before["long_horizon"]["phase"] == "p0_saved"
    assert before["long_horizon"]["clarification_attempts"][-1]["status"] == "budget_exhausted"
    assert any(row["status"] == "timeout" for row in before["budget"]["calls"])
    boundary = len(calls)
    second = await LongHorizonBenchmark(
        replace(args, work_id=before["work_id"], expected_revision=before["revision"]), plugin=plugin(calls)
    ).run()
    after = json.loads(second.selection.read_text())
    assert not second.internal_failure, after["internal_failure"]
    assert after["internal_accepted"] and after["delivered"]
    assert after["work_id"] == before["work_id"]
    assert after["budget"]["deadline"] == before["budget"]["deadline"]
    assert after["budget"]["started_at"] == before["budget"]["started_at"]
    assert len([r for r in calls if r[0] == "worker"]) == 1
    assert not any(r[0].startswith("[SUITE_INVESTIGATION") for r in calls[boundary:])
    assert [r[0] for r in calls[boundary:] if r[0].startswith("[SUITE_PROPOSAL")] == ["[SUITE_PROPOSAL 1]"]
    assert (args.workdir / "greeting.txt").read_text() == "hello\n"


@pytest.mark.asyncio
async def test_timeout_cannot_hide_readonly_mutation(tmp_path, git_repo_factory):
    args, calls = trial(tmp_path, git_repo_factory), []
    real_view: dict[str, Path] = {}
    value = interrupted_plugin(calls, mutate=True, mutate_target=lambda: real_view.get("view"))
    original_investigate = value.investigate

    async def capture_view(context, prompt, *, stage):
        real_view["view"] = Path(context.snapshot.roots[0].view_root)
        return await original_investigate(context, prompt, stage=stage)

    value.investigate = capture_view
    result = await LongHorizonBenchmark(args, plugin=value).run()
    report = json.loads(result.selection.read_text())
    assert result.internal_failure
    assert not report["internal_accepted"]
    assert "[SUITE_P1_REPAIR]" not in [r[0] for r in calls]


def test_completed_checkpoint_is_scoped_immutable_and_checked(tmp_path):
    a = PlanningCheckpoint(tmp_path / "work-one", {"task": "a", "snapshot": "s0", "model": "frozen"})
    a.save("proposal-1", {"guidance_points": []})
    assert a.read("proposal-1") == {"guidance_points": []}
    assert PlanningCheckpoint(tmp_path / "work-one", {"task": "b"}).read("proposal-1") is None
    assert (
        PlanningCheckpoint(tmp_path / "work-two", {"task": "a", "snapshot": "s0", "model": "frozen"}).read("proposal-1")
        is None
    )
    with pytest.raises(PlanningCheckpointError, match="immutable"):
        a.save("proposal-1", {"different": "result"})
    path = a._path("proposal-1")
    row = json.loads(path.read_text())
    row["payload"] = {"tampered": True}
    path.write_text(json.dumps(row))
    with pytest.raises(PlanningCheckpointError, match="evidence"):
        a.read("proposal-1")


def test_checkpoint_rejects_symlinks(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    (root / "completed-planning").symlink_to(tmp_path / "other")
    with pytest.raises(PlanningCheckpointError, match="symlink"):
        PlanningCheckpoint(root, {"task": "a"})


@pytest.mark.asyncio
async def test_invalid_proposals_are_not_cached_or_counted_valid(tmp_path, git_repo_factory):
    args, calls = trial(tmp_path, git_repo_factory), []
    value = plugin(calls)

    class Invalid(SuiteAgent):
        def run_turn(self, request):
            if request.prompt.startswith(("[SUITE_PROPOSAL 2]", "[SUITE_PROPOSAL 3]")):
                return TurnResult(
                    status="completed",
                    final_text='{"guidance_points": "invalid"}',
                    output_source="scripted",
                    exit_code=0,
                )
            return super().run_turn(request)

    value._factory = lambda role, **kwargs: Invalid(role, calls)
    outcome = await LongHorizonBenchmark(args, plugin=value).run()
    report = json.loads(outcome.selection.read_text())
    failure = report.get("internal_failure")
    if failure is not None:
        # Non-inspection suite path: the contract refusal still fails closed.
        assert "fewer than two valid" in failure["detail"]
    else:
        # Inspection (legacy) path: optional clarification degrades instead of ending the Work (PR #9).
        assert "fewer than two valid" not in json.dumps(report.get("internal_failure"))
    rows = [json.loads(p.read_text()) for p in args.output.rglob("completed-planning/*.json")]
    assert not any(row["stage"] in ("proposal-2:validated-proposal", "proposal-3:validated-proposal") for row in rows)


def test_jiuwen_fresh_adapter_never_reuses_a_previous_sequence_directory(tmp_path):
    import asyncio
    import threading
    from types import SimpleNamespace

    from jiuwenswarm.lh.plugins.jiuwen import JiuwenPlugin
    from jiuwenswarm.lh.ports import ExecutionBinding

    value = JiuwenPlugin(worker=SimpleNamespace(), adapter_factory=lambda **options: SimpleNamespace(**options))
    args = SimpleNamespace(output=tmp_path / "output", workflow="suite")
    context = dict(loop=asyncio.new_event_loop(), invocation_id="same-work", abort=threading.Event())
    try:
        first = value.make_adapter("worker", ExecutionBinding("fixed", {}), args, **context)
        first.output_dir.mkdir(parents=True)
        (first.output_dir / "old-result").write_text("retain")
        second = value.make_adapter("worker", ExecutionBinding("fixed", {}), args, **context)
        assert first.output_dir != second.output_dir
        assert (first.output_dir / "old-result").read_text() == "retain"
    finally:
        context["loop"].close()


@pytest.mark.parametrize("failure", ["mutation", "checkpoint"])
@pytest.mark.asyncio
async def test_fatal_concurrent_result_wins_over_an_earlier_timeout(tmp_path, git_repo_factory, failure):
    from jiuwenswarm.lh.file_artifacts import ClarificationBudgetExhausted
    from jiuwenswarm.lh.side_calls import SideCallIntegrityError

    args, calls = trial(tmp_path, git_repo_factory), []
    value = plugin(calls)
    original = value.investigate

    async def mixed(context, prompt, *, stage):
        if stage == "proposal-1:investigation":
            raise ClarificationBudgetExhausted("scripted timeout first in gather order")
        if stage == "proposal-2:investigation":
            if failure == "mutation":
                raise SideCallIntegrityError("scripted later frozen-view integrity error")
            raise PlanningCheckpointError("scripted later checkpoint integrity error")
        return await original(context, prompt, stage=stage)

    value.investigate = mixed
    result = await LongHorizonBenchmark(args, plugin=value).run()
    report = json.loads(result.selection.read_text())
    assert result.internal_failure
    expected = "SideCallIntegrityError" if failure == "mutation" else "PlanningCheckpointError"
    assert report["internal_failure"]["type"] == expected
    assert "[SUITE_P1_REPAIR]" not in [r[0] for r in calls]


def test_checkpoint_swap_to_symlink_between_check_and_open_is_rejected(tmp_path, monkeypatch):
    import os

    cp = PlanningCheckpoint(tmp_path / "work", {"task": "one"})
    cp.save("proposal", {"accepted": True})
    target = cp._path("proposal")
    outside = tmp_path / "outside.json"
    outside.write_bytes(target.read_bytes())
    real_open = os.open

    def swapped_open(path, flags, *args, **kwargs):
        if path == target:
            target.unlink()
            target.symlink_to(outside)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swapped_open)
    with pytest.raises(PlanningCheckpointError, match="evidence"):
        cp.read("proposal")
    assert not cp.reused
