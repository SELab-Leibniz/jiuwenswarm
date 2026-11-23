# Copyright (c) 2026 Chrys. All rights reserved.

"""Soft-deadline lifecycle: model admission closes, admitted checks may finish."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml

from jiuwenswarm.lh import suite
from jiuwenswarm.lh.control_policy import (
    DEFAULT_FINISH_RESERVE_SECONDS,
    MAX_FINISH_RESERVE_SECONDS,
    PhasePolicy,
)
from jiuwenswarm.lh.controller import LongHorizonBenchmark, _p0_prompt
from jiuwenswarm.lh.review_governance import time_admission
from jiuwenswarm.lh.worker_evidence import (
    ANYTIME_DELIVERABLE_GUIDANCE_VERSION,
    worker_anytime_deliverable_guidance,
    worker_evidence_guidance,
)


def _policy(tmp_path: Path, now: list[float], *, total: float = 28_800.0):
    return PhasePolicy(0.0, total, total, total, tmp_path / "phase-policy.json", clock=lambda: now[0])


def _controller(tmp_path: Path, now: list[float], *, soft: float = 150.0, hard: float = 200.0):
    controller = object.__new__(LongHorizonBenchmark)
    controller._rpc_public_check = False
    controller._check_deadline = hard
    controller._soft_deadline = soft
    controller._original_deadline = hard
    controller._abort = threading.Event()
    controller._phase_policy = PhasePolicy(100.0, hard, soft, hard - 100.0, tmp_path / "phase.json", clock=lambda: now[0])
    controller._budget = SimpleNamespace(clock=lambda: now[0])
    controller._gate = SimpleNamespace(observed=lambda _candidate: False, record=Mock(), selected=None)
    controller._invocation = SimpleNamespace(state=None)
    controller._public_check_durations = []
    controller._public_check_evidence = []
    controller._finish_observations = []
    controller._rejected_check_checkpoints = set()
    controller._pending_seal_seconds = 0.0
    return controller


def _state(checkpoint: str = "candidate-1"):
    return SimpleNamespace(
        candidate=SimpleNamespace(checkpoint=checkpoint, tree="tree-" + checkpoint),
        verification=None,
        work_id="synthetic-work",
        revision=1,
        status="active",
        delivery=None,
    )


def test_finish_reserve_uses_observation_safety_factor_and_caps(tmp_path):
    now = [0.0]
    policy = _policy(tmp_path, now)
    assert policy.finish_reserve() == (DEFAULT_FINISH_RESERVE_SECONDS, "default")

    policy.observe_finish(300.0)
    assert policy.finish_reserve() == (450.0, "observed")
    policy.observe_finish(5_000.0)
    assert policy.finish_reserve() == (MAX_FINISH_RESERVE_SECONDS, "observed")

    # The cap follows the remaining execution window, never the total budget.
    now[0] = 28_800.0 - 1_000.0
    assert policy.finish_reserve() == (150.0, "observed")


def test_observed_finish_moves_the_next_worker_deadline_earlier(tmp_path):
    now = [0.0]
    policy = _policy(tmp_path, now)
    acceptance = policy.acceptance_reserve_seconds
    assert acceptance == 1_440.0
    before = policy.deadline_for("p1")
    assert before == 28_800.0 - DEFAULT_FINISH_RESERVE_SECONDS - acceptance

    policy.observe_finish(300.0)
    after = policy.deadline_for("p1")
    assert after == 28_800.0 - 450.0 - acceptance
    assert after < before
    assert policy.deadline_for("check") == 28_800.0  # checks keep the original clock


def test_eight_hour_worker_cutoff_leaves_an_acceptance_and_seal_window(tmp_path):
    now = [0.0]
    total = 8 * 3600.0
    soft = total - 1_440.0
    policy = PhasePolicy(0.0, total, soft, total, tmp_path / "phase.json", clock=lambda: now[0])

    worker_deadline = policy.deadline_for("worker")
    finish = policy.finish_reserve_seconds
    acceptance = policy.acceptance_reserve_seconds
    assert acceptance == 1_440.0
    assert worker_deadline == soft - finish - acceptance
    assert policy.admissions[-1]["acceptance_reserve_skipped"] is False

    now[0] = worker_deadline
    admission = time_admission(
        now=now[0],
        soft_deadline=soft,
        original_deadline=total,
        total_seconds=total,
        snapshot={"calls": []},
        check_durations=[],
    )
    assert admission.soft_remaining == finish + acceptance
    assert admission.acceptance_allowed
    assert policy.finish_reserve_seconds <= finish + 1e-9


def test_tight_budget_skips_acceptance_reserve_but_still_starts_worker(tmp_path):
    total = 8 * 3600.0
    soft = total - 1_440.0
    now = [soft - 1_000.0]
    policy = PhasePolicy(0.0, total, soft, total, tmp_path / "phase.json", clock=lambda: now[0])

    finish = policy.finish_reserve_seconds
    worker_deadline = policy.deadline_for("worker")
    assert worker_deadline == soft - finish
    assert worker_deadline > now[0]
    assert policy.admissions[-1]["acceptance_reserve_skipped"] is True
    payload = json.loads(policy.path.read_text())
    assert payload["acceptance_reserve_skipped"] is True
    assert payload["acceptance_reserve_seconds"] == 1_440.0


def test_check_crossing_soft_but_finishing_before_hard_is_selected(tmp_path):
    now = [140.0]
    controller = _controller(tmp_path, now)
    state = _state()

    def verify(_work_id, *, expected_revision, deadline):
        assert deadline == 200.0
        now[0] = 160.0
        state.verification = SimpleNamespace(candidate=state.candidate, status="passed")
        return state

    checked = controller._check_candidate(
        SimpleNamespace(verify_candidate=verify), state, "p1"
    )
    assert checked is state
    controller._gate.record.assert_called_once_with(state, "p1")
    evidence = controller._public_check_evidence[-1]
    assert evidence["started_at"] == 140.0
    assert evidence["finished_at"] == 160.0
    assert evidence["crossed_soft_deadline"] is True
    assert evidence["stop_reason"] == "soft_deadline_allowed"
    assert controller._finish_observations


def test_check_crossing_hard_is_a_timeout_and_never_selected(tmp_path):
    now = [190.0]
    controller = _controller(tmp_path, now)
    state = _state("late-candidate")

    def verify(_work_id, *, expected_revision, deadline):
        now[0] = 210.0
        state.verification = SimpleNamespace(candidate=state.candidate, status="passed")
        return state

    checked = controller._check_candidate(
        SimpleNamespace(verify_candidate=verify), state, "p1"
    )
    assert checked is state
    controller._gate.record.assert_not_called()
    assert "late-candidate" in controller._rejected_check_checkpoints
    assert controller._public_check_evidence[-1]["stop_reason"] == "hard_deadline"
    assert controller._invocation.stop_reason == "window_budget_exhausted"


def test_explicit_cancel_during_check_is_cancelled_and_not_selected(tmp_path):
    now = [140.0]
    controller = _controller(tmp_path, now)
    state = _state("cancelled-candidate")

    def verify(_work_id, *, expected_revision, deadline):
        controller._abort.set()
        now[0] = 160.0
        state.verification = SimpleNamespace(candidate=state.candidate, status="passed")
        return state

    controller._check_candidate(SimpleNamespace(verify_candidate=verify), state, "p1")
    controller._gate.record.assert_not_called()
    assert controller._public_check_evidence[-1]["stop_reason"] == "cancelled"
    assert controller._invocation.stop_reason == "invocation_stopped"


def test_priority_check_does_not_replace_an_existing_passed_candidate(tmp_path):
    now = [190.0]
    controller = _controller(tmp_path, now)
    prior = (SimpleNamespace(checkpoint="prior"), SimpleNamespace(status="passed"), "p0", "digest")
    controller._gate.selected = prior
    state = _state("later")
    runtime = SimpleNamespace(verify_candidate=Mock())
    assert controller._check_priority_candidate(runtime, state) is state
    runtime.verify_candidate.assert_not_called()
    assert controller._gate.selected is prior


def test_priority_check_completes_one_sealed_unchecked_candidate(tmp_path):
    now = [190.0]
    controller = _controller(tmp_path, now, soft=150.0, hard=200.0)
    state = _state("pending")

    def verify(_work_id, *, expected_revision, deadline):
        now[0] = 195.0
        state.verification = SimpleNamespace(candidate=state.candidate, status="passed")
        return state

    checked = controller._check_priority_candidate(SimpleNamespace(verify_candidate=verify), state)
    assert checked is state
    controller._gate.record.assert_called_once()
    assert controller._public_check_evidence[-1]["stop_reason"] == "soft_deadline_allowed"


def test_soft_deadline_blocks_new_worker_and_reviewer_turns():
    native = SimpleNamespace(id="synthetic-role", run_turn=Mock())
    controller = SimpleNamespace(_model_window_closed=lambda: True)
    guided = suite.GuidedAdapter(native, controller, SimpleNamespace(), None, None)
    managed = suite.ManagedReviewer(native, controller, SimpleNamespace(), None, None)
    request = SimpleNamespace(prompt="synthetic", workdir=Path("/tmp"), deadline=None)

    assert guided.run_turn(request).status == "timeout"
    assert managed.run_turn(request).status == "timeout"
    native.run_turn.assert_not_called()


def test_anytime_deliverable_clause_is_in_worker_prompts():
    assert ANYTIME_DELIVERABLE_GUIDANCE_VERSION == "anytime-deliverable/v1"
    clause = worker_anytime_deliverable_guidance()
    assert "anytime-deliverable/v1" in clause
    assert "temporary file" in clause and "rename" in clause
    assert clause in worker_evidence_guidance()

    state = SimpleNamespace(contract={"goal": "synthetic"}, constraints=[], acceptance_brief={})
    assert ANYTIME_DELIVERABLE_GUIDANCE_VERSION in _p0_prompt(state)


def test_review_governance_payload_keeps_first_time_admission_evidence(tmp_path):
    evidence = {
        "schema": "long-horizon/time-admission/v1",
        "now_unix": 27_631.0,
        "original_remaining_seconds": 1_169.0,
        "soft_remaining_seconds": 729.0,
        "total_seconds": 28_800.0,
        "acceptance_reserve_seconds": 1_440.0,
        "worker_estimate_seconds": 16_827.0,
        "required_before_worker_seconds": 18_267.0,
        "worker_allowed": False,
        "diagnosis_allowed": False,
        "acceptance_allowed": False,
        "purpose": "acceptance",
        "allowed": False,
        "rejection_condition": "soft_remaining < acceptance_reserve",
    }
    controller = object.__new__(LongHorizonBenchmark)
    controller._review_admissions = SimpleNamespace(payload=lambda: {"limits": {}, "admissions": [], "refusals": []})
    controller._budget = None
    controller._review_governance = {"first_review_time_admission": evidence}
    controller._code_sha256 = lambda: "synthetic"
    payload = controller._review_governance_payload(None)
    assert payload["first_review_time_admission"] == evidence
    assert payload["first_review_time_admission"]["purpose"] == "acceptance"
    assert payload["first_review_time_admission"]["rejection_condition"] == "soft_remaining < acceptance_reserve"
    assert payload["first_review_time_admission"]["soft_remaining_seconds"] == 729.0
    assert payload["first_review_time_admission"]["worker_estimate_seconds"] == 16_827.0
    assert payload["first_review_time_admission"]["acceptance_reserve_seconds"] == 1_440.0


def _recovery_controller(now, *, output, hard=28_800.0, soft=27_360.0, calls=None):
    controller = object.__new__(LongHorizonBenchmark)
    controller._args = SimpleNamespace(
        control_profile="bounded",
        max_turns=None,
        budget_seconds=hard,
    )
    controller._soft_deadline = soft
    controller._original_deadline = hard
    controller._review_governance = {}
    controller._invocation = SimpleNamespace(state=None, stop_reason="not_started")
    controller._finish_observations = []
    snapshot = {
        "started_at": 0.0,
        "deadline": hard,
        "calls": list(calls or []),
    }
    budget = SimpleNamespace(
        clock=lambda: now[0],
        snapshot=lambda: snapshot,
    )
    return controller, budget, snapshot


def _recovery_window(controller, budget, phase):
    if phase == "first_review_done":
        return controller._restored_budget_window(budget)
    return SimpleNamespace(started_at=0.0, deadline=controller._soft_deadline, execution_deadline=controller._soft_deadline)


@pytest.mark.parametrize("phase", [
    "created",
    "s0_frozen",
    "p0_started",
    "p0_saved",
    "clarification_started",
    "brief_binding",
    "brief_bound",
    "p1_started",
    "p1_saved",
    "first_review_started",
    "first_review_done",
])
@pytest.mark.parametrize("recovered_at", [7_200.0, 14_400.0, 21_600.0])
def test_recovery_uses_absolute_soft_window_for_every_phase(tmp_path, phase, recovered_at):
    now = [recovered_at]
    controller, budget, _snapshot = _recovery_controller(now, output=tmp_path)
    window = _recovery_window(controller, budget, phase)

    controller._initialize_phase_policy(tmp_path, budget, window)

    policy = controller._phase_policy
    assert policy is not None
    assert policy.soft_deadline == 27_360.0
    assert policy.hard_deadline == 28_800.0
    expected = policy.soft_deadline - policy.finish_reserve_seconds - policy.acceptance_reserve_seconds
    assert policy.worker_deadline() == expected
    assert policy.deadline_for("worker") == expected
    if phase == "s0_frozen":
        p0_deadline = policy.deadline_for("p0")
        assert p0_deadline > recovered_at
        assert p0_deadline != recovered_at - 144.0


def test_recovery_restores_observed_finish_history(tmp_path):
    now = [0.0]
    controller, budget, _snapshot = _recovery_controller(now, output=tmp_path)
    controller._initialize_phase_policy(tmp_path, budget, _recovery_window(controller, budget, "s0_frozen"))
    controller._phase_policy.observe_finish(300.0)
    assert controller._phase_policy.finish_reserve() == (450.0, "observed")

    resumed, resumed_budget, _resumed_snapshot = _recovery_controller(now, output=tmp_path)
    resumed._initialize_phase_policy(
        tmp_path,
        resumed_budget,
        _recovery_window(resumed, resumed_budget, "s0_frozen"),
    )

    assert resumed._phase_policy.finish_observations == [300.0]
    assert resumed._phase_policy.finish_reserve() == (450.0, "observed")
    assert resumed._phase_policy.finish_reserve_seconds == 450.0


def test_worker_deadline_reads_latest_acceptance_history_at_runtime(tmp_path):
    now = [0.0]
    controller, budget, snapshot = _recovery_controller(now, output=tmp_path)
    controller._initialize_phase_policy(tmp_path, budget, _recovery_window(controller, budget, "s0_frozen"))
    policy = controller._phase_policy

    first = policy.deadline_for("worker")
    assert policy.acceptance_reserve_seconds == 1_440.0
    assert first == policy.soft_deadline - policy.finish_reserve_seconds - 1_440.0

    snapshot["calls"].append(
        {
            "role": "reviewer",
            "purpose": "acceptance",
            "status": "completed",
            "observed_duration_seconds": 2_000.0,
        }
    )

    second = policy.deadline_for("worker")
    assert policy.acceptance_reserve_seconds == 2_500.0
    assert second == policy.soft_deadline - policy.finish_reserve_seconds - 2_500.0
    payload = json.loads(policy.path.read_text())
    assert payload["acceptance_reserve_seconds"] == 2_500.0
    assert payload["recent_acceptance_seconds"] == [2_000.0]
