# Copyright (c) 2026 Chrys. All rights reserved.

"""P1 failure routing with real Core work/check/review bookkeeping, no models."""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from pact_core import verification
from test_control_regressions import BAD, GOOD, VERIFY, FakeAdapter, FixturePlugin, repository

from jiuwenswarm.lh.controller import BenchmarkArguments, LongHorizonBenchmark, _first_check_outcome


class TrackingAdapter(FakeAdapter):
    def run_turn(self, request):
        if self.role != "worker":
            self.state["review_calls"].append(request)
        return super().run_turn(request)


class TrackingPlugin(FixturePlugin):
    def make_adapter(self, role, resolved, args, **kwargs):
        return TrackingAdapter(self.state, role)

    def __init__(self, state):
        super().__init__(state)
        self.state = state

    async def investigate(self, context, prompt, *, stage):
        return ""  # No side-model calls in this synthetic real-Core fixture.


async def run_case(tmp_path, *, check=None, steps=None, max_turns=3):
    repo = repository(tmp_path, BAD)
    task = tmp_path / "instruction.txt"
    task.write_text("Implement calculate, preserving the original public requirements.")
    calls = {
        "steps": steps or [(BAD, "completed"), (BAD, "completed"), (GOOD, "completed")],
        "calls": [],
        "review_calls": [],
    }
    args = BenchmarkArguments(
        task,
        repo,
        tmp_path / "out",
        "synthetic",
        "trial",
        budget_seconds=600,
        budget_started_at_unix=time.time(),
        turn_timeout_seconds=120,
        max_turns=max_turns,
        control_profile="bounded",
        verify_command=VERIFY,
        worktree_root=tmp_path / "worktrees",
    )
    controller = LongHorizonBenchmark(args, plugin=TrackingPlugin(calls))
    localization = {"status": "unavailable", "text": "", "artifacts": []}
    with patch("jiuwenswarm.lh.suite.localize_suite", AsyncMock(return_value=localization)):
        if check is None:
            outcome = await controller.run()
        else:
            with patch.object(verification, "_run_task_command", side_effect=check):
                outcome = await controller.run()
    return controller, calls, outcome, json.loads((tmp_path / "out/selection.json").read_text())


@pytest.mark.asyncio
async def test_real_compile_failure_reaches_diagnostic_and_actionable_worker(tmp_path):
    controller, calls, outcome, selection = await run_case(tmp_path)
    assert len(calls["calls"]) == 3
    assert calls["review_calls"]
    first = calls["review_calls"][0]
    assert "Diagnose the recorded public result" in first.prompt
    assert "concrete implementation change" in first.prompt
    progress = controller._progress(controller._invocation.state)
    assert progress["first_review"]["purpose"] == "diagnostic"
    assert progress["first_check_outcome"]["outcome"] == "command_failure_diagnostic"
    assert progress["first_check_outcome"]["automatic_recheck"] is False
    assert outcome.candidate_available
    assert Path(selection["export"]["snapshot"], "module.py").read_text() == GOOD
    assert not selection["internal_accepted"]
    assert selection["external_grade"]["reward"] is None


@pytest.mark.asyncio
async def test_rpc_infrastructure_signal_stops_without_reviewer_or_retry(tmp_path):
    count = []

    def unavailable(*args, **kwargs):
        count.append(kwargs["deadline"])
        return "failed", 125, "", "public_check_runtime_error", 0.0

    controller, calls, outcome, selection = await run_case(tmp_path, check=unavailable)
    assert len(count) == 3  # S0, P0, P1 exactly once each.
    assert len(calls["calls"]) == 2
    assert calls["review_calls"] == []
    assert not outcome.candidate_available
    assert not selection["internal_accepted"]
    progress = controller._progress(controller._invocation.state)
    record = progress["first_check_outcome"]
    assert record["outcome"] == "infrastructure_signal"
    assert record["infrastructure_cause_established"] is False
    assert record["stop_reason"] == "public_check_infrastructure_unresolved"
    assert "public_check_runtime_error" not in json.dumps(record)


@pytest.mark.asyncio
async def test_recorded_timeout_goes_to_diagnostic_without_automatic_recheck(tmp_path):
    count = []

    def timeout(*args, **kwargs):
        count.append(kwargs["deadline"])
        return "timeout", None, "", "", 0.0

    controller, calls, _, selection = await run_case(
        tmp_path,
        check=timeout,
        steps=[(BAD, "completed"), (BAD, "completed")],
        max_turns=2,
    )
    # Explicit max2 prevents a new Worker; only the diagnostic is admitted.
    assert calls["review_calls"]
    assert controller._progress(controller._invocation.state)["first_review"]["purpose"] == "diagnostic"
    assert len(count) == 3
    assert count == [count[0]] * 3
    assert not selection["internal_accepted"]


def evidence(status="failed", code=1, *, checks=(), stderr="", candidate=None):
    current = SimpleNamespace(tree="current")
    return SimpleNamespace(
        candidate=current,
        verification=SimpleNamespace(
            candidate=current if candidate is None else candidate,
            artifact=Path("/owned/verification.json"),
            status=status,
            exit_code=code,
            check_results=checks,
            stderr_tail=stderr,
        ),
    )


@pytest.mark.parametrize(
    "case,outcome",
    [
        (evidence("failed", 2), "command_failure_diagnostic"),
        (evidence("timeout", None), "timeout_diagnostic"),
        (
            evidence("failed", None, checks=({"name": "namespace_clean", "passed": False},)),
            "unclassified_error",  # Derived-check-shaped data is not structural proof.
        ),
        (evidence("failed", None), "unclassified_error"),
        (evidence("failed", 125, stderr="not trusted arbitrary output"), "unclassified_error"),
        (evidence("failed", -9), "unclassified_error"),
        (evidence("unavailable", None), "unavailable"),
        (evidence("passed", 0, candidate=SimpleNamespace(tree="stale")), "candidate_mismatch"),
        (evidence("passed", 0), "passed"),
    ],
)
def test_typed_evidence_classification(case, outcome):
    record = _first_check_outcome(case, 1234.0)
    assert record["outcome"] == outcome
    assert record["global_deadline_unix"] == 1234.0
    assert record["automatic_recheck"] is False
    if outcome in {"unclassified_error", "unavailable", "candidate_mismatch"}:
        assert record["stop_reason"] is not None


def test_missing_gate_evidence_is_unresolved_and_safe():
    record = _first_check_outcome(SimpleNamespace(candidate=SimpleNamespace(tree="current"), verification=None), 7)
    assert record["outcome"] == "missing"
    assert record["stop_reason"] == "public_check_missing"


def test_unavailable_without_configured_check_is_not_configured():
    record = _first_check_outcome(evidence("unavailable", None), 1234.0, check_configured=False)
    assert (record["outcome"], record["stop_reason"]) == ("not_configured", None)


def test_unavailable_with_configured_check_remains_unavailable():
    record = _first_check_outcome(evidence("unavailable", None), 1234.0, check_configured=True)
    assert (record["outcome"], record["stop_reason"]) == ("unavailable", "public_check_unavailable")


def test_candidate_mismatch_without_configured_check_still_mismatches():
    case = evidence("passed", 0, candidate=SimpleNamespace(tree="stale"))
    record = _first_check_outcome(case, 1234.0, check_configured=False)
    assert (record["outcome"], record["stop_reason"]) == ("candidate_mismatch", "public_check_candidate_mismatch")


@pytest.mark.parametrize("expired", [False, True])
def test_missing_required_gate_receipt_or_total_expiry_starts_nothing(expired):
    from threading import Event

    controller = object.__new__(LongHorizonBenchmark)
    controller._rpc_public_check = False
    controller._args = SimpleNamespace(workflow="suite", verify_command=None)
    controller._gate = object()
    controller._abort = Event()
    controller._invocation = SimpleNamespace(stop_reason="not_started")
    controller._phase = lambda _: "p1_saved"
    controller._stages = lambda _: {"p1": {"candidate_tree": "current"}}
    controller._advance = Mock(side_effect=lambda runtime, state, phase, **kwargs: state)
    current = SimpleNamespace(candidate=SimpleNamespace(tree="current"), verification=None)
    budget = SimpleNamespace(
        purpose_deadline=lambda *a: 90, stop_reason=lambda *a: "budget_exhausted" if expired else None
    )
    runtime = SimpleNamespace(verify_candidate=Mock(), review_candidate=Mock(), accept_candidate=Mock())
    assert controller._first_review(runtime, budget, SimpleNamespace(deadline=100), current) is current
    assert controller._invocation.stop_reason == ("budget_exhausted" if expired else "public_check_missing")
    runtime.verify_candidate.assert_not_called()
    runtime.review_candidate.assert_not_called()
    runtime.accept_candidate.assert_not_called()


def test_real_core_structural_failure_uses_its_recorded_artifact(tmp_path):
    repo = repository(tmp_path, GOOD)
    from test_control_regressions import git

    head = git(repo, "rev-parse", "HEAD")
    result = verification.run_verification(
        workspace=repo,
        workdir=repo,
        checkpoint_commit=head,
        baseline_commit=head,
        namespace_violations=["synthetic-outside-namespace"],
        required_artifacts=[],
        command=VERIFY,
        timeout_seconds=120,
        loop_dir=tmp_path,
        round_number=1,
        patch_path=tmp_path / "patch.diff",
        deadline=time.time() + 120,
    )
    assert result.status == "failed" and result.exit_code is None
    artifact = tmp_path / "verification.json"
    artifact.write_text(json.dumps(asdict(result)))
    state = evidence("failed", None)
    state.verification.artifact = artifact
    assert _first_check_outcome(state, 1234)["outcome"] == "structural_failure_diagnostic"
    artifact.write_text('{"status":"failed","exit_code":null,"checks":[]}')
    assert _first_check_outcome(state, 1234)["outcome"] == "unclassified_error"


@pytest.mark.parametrize(
    "record",
    [
        [],
        None,
        "not-an-object",
        {"status": "failed", "checks": [{"name": "namespace_clean", "passed": False}]},
        {"status": "failed", "exit_code": None, "checks": None},
        {"status": "failed", "exit_code": None, "checks": [{"name": [], "passed": False}]},
        {"status": "failed", "exit_code": None, "checks": [{"name": {}, "passed": False}]},
    ],
)
def test_malformed_structural_artifact_stays_unresolved(tmp_path, record):
    artifact = tmp_path / "verification.json"
    artifact.write_text(json.dumps(record))
    state = evidence("failed", None)
    state.verification.artifact = artifact
    assert _first_check_outcome(state, 1234)["outcome"] == "unclassified_error"


def test_missing_or_invalid_structural_artifact_stays_unresolved(tmp_path):
    state = evidence("failed", None)
    for artifact in (tmp_path / "missing.json", None):
        state.verification.artifact = artifact
        assert _first_check_outcome(state, 1234)["outcome"] == "unclassified_error"
