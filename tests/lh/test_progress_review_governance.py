# Copyright (c) 2026 Chrys. All rights reserved.

"""Time-and-progress review governance controls (replaces low-count stops)."""

from __future__ import annotations

import inspect
import json
import threading
from pathlib import Path
from types import SimpleNamespace

from jiuwenswarm.pact_core.runtime.types import CandidateRef, ReviewEvidence

from jiuwenswarm.lh.controller import LongHorizonBenchmark
from jiuwenswarm.lh.progress import ReviewProgressGuard
from jiuwenswarm.lh.review_admissions import (
    FUSE_REASON,
    ReviewAdmissions,
    default_review_limits,
)
from jiuwenswarm.lh.review_governance import (
    acceptance_reserve,
    normalize_gap_id,
    recent_acceptance_durations,
    time_admission,
    worker_check_estimate,
)

BACKENDS = ["jiuwen", "chrys"]


def _state(
    root: Path,
    worker: int,
    *,
    gaps: list[str] | None = None,
    failed: int = 1,
    tree: str = "tree",
    text: str | None = None,
) -> SimpleNamespace:
    candidate = CandidateRef("work", Path(f"candidate-{worker}.json"), f"checkpoint-{worker}", tree)
    artifact = root / f"review-{worker}.md"
    review_text = text if text is not None else "Review.\n"
    if gaps is not None:
        review_text += "<pact-gaps>" + json.dumps(gaps) + "</pact-gaps>\n"
    artifact.write_text(review_text)
    checks = tuple(
        {"name": f"check-{index}", "version": 1, "status": "failed" if index < failed else "passed", "exit_code": 1}
        for index in range(3)
    )
    return SimpleNamespace(
        candidate=candidate,
        review=ReviewEvidence(artifact, candidate, False, "feedback_recorded", ("feedback_recorded",)),
        verification=SimpleNamespace(candidate=candidate, status="failed", check_results=checks),
        policy=SimpleNamespace(verify_command="public-check"),
        checks=(),
        acceptance=None,
        delivery=None,
        contract_revision=1,
        contract={"goal": "Original task"},
        constraints=("Preserve baseline",),
        unresolved_assumptions=(),
        user_decisions=(),
    )


def _admission_state(tree: str) -> SimpleNamespace:
    return SimpleNamespace(
        candidate=SimpleNamespace(tree=tree),
        contract_revision=0,
        checks=(),
        user_decisions=(),
    )


def test_many_admissions_and_positive_progress_do_not_stop_on_count(tmp_path):
    ledger = ReviewAdmissions(tmp_path / "review.json", acceptance_limit=64, same_input_limit=2)
    for index in range(12):
        assert ledger.admit(_admission_state(f"tree-{index}"), purpose="acceptance")["admitted"]
    assert ledger.payload()["purposes"]["acceptance"]["count"] == 12

    guard = ReviewProgressGuard()
    for worker, gap in enumerate(("alpha", "alpha and beta", "alpha, beta and gamma"), start=1):
        assert not guard.observe(_state(tmp_path, worker, gaps=[gap]), worker)


def test_three_rejections_without_new_or_closed_gap_or_check_improvement_stop(tmp_path):
    guard = ReviewProgressGuard()
    for worker in (1, 2):
        assert not guard.observe(_state(tmp_path, worker, gaps=[]), worker)
    assert guard.observe(_state(tmp_path, 3, gaps=[]), 3)
    assert guard.stop_reason == "review_no_progress"




def test_missing_gap_block_with_changing_tree_and_gap_text_never_stops(tmp_path):
    guard = ReviewProgressGuard()
    for worker in range(1, 6):
        state = _state(
            tmp_path,
            worker,
            tree=f"tree-{worker}",
            text=f"Review.\n\n## Remaining gap\n\n- boundary case {worker}\n",
        )
        assert not guard.observe(state, worker)
    assert guard.stop_reason is None
    assert guard.evidence()["no_progress_rounds"] == 0


def test_missing_gap_block_with_changed_list_or_heading_fingerprints_never_stops(tmp_path):
    guard = ReviewProgressGuard()
    reviews = (
        "Review.\n\n## Missing boundary\n\n- error handling\n",
        "Review.\n\n## Missing integration\n\n- retry behavior\n",
        "Review.\n\n## Missing integration\n\n- retry behavior\n- logging\n",
    )
    for worker, text in enumerate(reviews, start=1):
        assert not guard.observe(_state(tmp_path, worker, text=text), worker)
    assert guard.stop_reason is None


def test_missing_gap_block_stops_only_after_three_unchanged_rounds(tmp_path):
    guard = ReviewProgressGuard()
    text = "Review.\n\nThe same boundary gap is still present.\n"
    assert not guard.observe(_state(tmp_path, 1, text=text), 1)
    assert not guard.observe(_state(tmp_path, 2, text=text), 2)
    assert guard.stop_reason is None
    assert guard.observe(_state(tmp_path, 3, text=text), 3)
    assert guard.stop_reason == "review_no_progress"
    assert guard.evidence()["no_progress_rounds"] == 3


def test_persisted_consecutive_counts_survive_reload(tmp_path):
    path = tmp_path / "review-progress.json"
    guard = ReviewProgressGuard(path=path)
    guard._no_progress_rounds = 2
    guard._same_gap_rounds = 1
    guard._diagnosis_streak = 1
    guard._protocol_streak = 1
    guard._diagnosis_paused = True
    guard._persist_if_changed(False)

    restored = ReviewProgressGuard(path=path)
    assert restored._no_progress_rounds == 2
    assert restored._same_gap_rounds == 1
    assert restored._diagnosis_streak == 1
    assert restored._protocol_streak == 1
    assert restored.diagnosis_paused is True

def test_empty_structured_gap_block_is_no_gaps_not_a_parse_failure(tmp_path):
    for index, payload in enumerate(("[]", "", "   ", "null", "[null]", "{}"), start=1):
        text = (
            "Review.\n\n"
            "## Remaining gap\n\n"
            "- fallback text that must not be used\n\n"
            f"<pact-gaps>{payload}</pact-gaps>\n"
        )
        guard = ReviewProgressGuard()
        assert not guard.observe(_state(tmp_path, index, text=text), index)
        evidence = guard.evidence()
        assert evidence["last_gaps"] == [], payload
        assert evidence["same_gap_rounds"] == 0, payload
        assert evidence["events"][-1]["gaps"] == [], payload
        assert evidence["seen_gaps"] == [], payload


def test_same_blocking_gap_three_rounds_without_improvement_stops(tmp_path):
    guard = ReviewProgressGuard()
    for worker in (1, 2):
        assert not guard.observe(_state(tmp_path, worker, gaps=["blocking boundary"]), worker)
    assert guard.observe(_state(tmp_path, 3, gaps=["blocking boundary"]), 3)
    assert guard.stop_reason in {"review_no_progress", "review_no_progress_same_gap"}
    assert guard.evidence()["same_gap_rounds"] == 3


def test_diagnosis_pauses_after_two_rounds_without_new_evidence(tmp_path):
    guard = ReviewProgressGuard()
    # The first diagnosis contributes a new gap, so it is not an evidence-free
    # repeat. The next two identical diagnostics are.
    assert not guard.observe(_state(tmp_path, 1, gaps=["new gap"]), 1, purpose="diagnostic")
    assert not guard.observe(_state(tmp_path, 2, gaps=["new gap"]), 2, purpose="diagnostic")
    assert not guard.observe(_state(tmp_path, 3, gaps=["new gap"]), 3, purpose="diagnostic")
    assert guard.diagnosis_paused
    assert guard.stop_reason is None
    assert guard.evidence()["diagnosis_streak"] == 2


def test_protocol_failures_are_stalled_separately_from_business_rejection(tmp_path):
    guard = ReviewProgressGuard()
    for worker in (1, 2):
        state = _state(tmp_path, worker)
        state.review = ReviewEvidence(
            state.review.artifact,
            state.candidate,
            False,
            "protocol",
            ("reviewer_protocol_error",),
        )
        result = guard.observe(state, worker)
        assert result == (worker == 2)
    assert guard.stop_reason == "review_protocol_stalled"
    assert guard.evidence()["no_progress_rounds"] == 0


def test_final_acceptance_after_resume_derives_window_from_persisted_deadline(tmp_path, monkeypatch):
    controller = object.__new__(LongHorizonBenchmark)
    controller._abort = threading.Event()
    controller._review_governance = {}
    controller._args = SimpleNamespace(budget_seconds=7200)
    controller._public_check_durations = []
    controller._soft_deadline = 1300.0
    controller._time_admission_active = lambda: False
    controller._invocation = SimpleNamespace(state=None)

    class Budget:
        def clock(self):
            return 1000.0

        def snapshot(self):
            return {"deadline": 5000.0, "calls": []}

    state = SimpleNamespace(acceptance=None, delivery=None, revision=1)
    captured = {}

    class Driver:
        def _review(self, state, budget, window, *, purpose, full, force):
            captured["window"] = window
            return state, None

    monkeypatch.setattr("jiuwenswarm.lh.controller._bench_progress", lambda _state: {"ready": True})
    monkeypatch.setattr("jiuwenswarm.lh.controller._public_passed", lambda _state: True)
    result = controller._try_final_acceptance(None, tmp_path, Budget(), None, state, Driver())
    assert result == (state, False)
    window = captured["window"]
    assert window is not None
    # The persisted absolute soft deadline is reused; no fresh window is opened.
    assert window.deadline == 1300.0
    assert window.execution_deadline == 1300.0


def test_time_admission_math_includes_repair_and_uses_mean_worker_history():
    snapshot = {
        "calls": [
            {"role": "reviewer", "purpose": "acceptance", "status": "completed", "observed_duration_seconds": 100},
            {"role": "reviewer", "purpose": "acceptance", "status": "completed", "observed_duration_seconds": 30},
            {"role": "worker", "purpose": "implementation", "status": "completed", "observed_duration_seconds": 100},
            {"role": "reviewer", "purpose": "acceptance", "status": "completed", "observed_duration_seconds": 200},
            {"role": "worker", "purpose": "implementation", "status": "completed", "observed_duration_seconds": 200},
            {"role": "reviewer", "purpose": "acceptance", "status": "completed", "observed_duration_seconds": 300},
            {"role": "worker", "purpose": "implementation", "status": "completed", "observed_duration_seconds": 300},
        ]
    }
    assert recent_acceptance_durations(snapshot) == [130.0, 200.0, 300.0]
    # A = max(10min, 5% of four hours = 720s, 300 * 1.25 = 375s) = 720s.
    assert acceptance_reserve(total_seconds=4 * 3600, recent_acceptance=[130, 200, 300]) == 720
    # W = mean(100, 200, 300) + mean(50, 100) = 275s.
    assert worker_check_estimate(snapshot, check_durations=[50, 100]) == 275
    admission = time_admission(
        now=1000.0,
        soft_deadline=2000.0,
        original_deadline=2500.0,
        total_seconds=4 * 3600,
        snapshot=snapshot,
        check_durations=[50, 100],
    )
    assert admission.soft_remaining == 1000
    assert admission.original_remaining == 1500
    assert admission.acceptance_reserve == 720
    assert admission.worker_estimate == 275
    assert admission.required_before_worker == 995
    assert admission.worker_allowed
    tight = time_admission(
        now=1100.0,
        soft_deadline=2000.0,
        original_deadline=2500.0,
        total_seconds=4 * 3600,
        snapshot=snapshot,
        check_durations=[50, 100],
    )
    assert tight.soft_remaining == 900
    assert not tight.worker_allowed
    assert tight.acceptance_allowed


def test_acceptance_fuse_is_reported_separately(tmp_path):
    ledger = ReviewAdmissions(tmp_path / "review.json", acceptance_limit=2, same_input_limit=2)
    assert ledger.admit(_admission_state("one"), purpose="acceptance")["admitted"]
    assert ledger.admit(_admission_state("two"), purpose="acceptance")["admitted"]
    refused = ledger.admit(_admission_state("three"), purpose="acceptance")
    assert refused["admitted"] is False
    assert refused["reason"] == "acceptance_fuse"
    assert refused["stop"] == FUSE_REASON


def test_default_limits_are_extreme_fuses_only():
    assert default_review_limits(120) == (64, 32)
    assert default_review_limits(None) == (64, 32)


def test_first_review_passes_the_locked_budget_recheck_to_both_admissions():
    source = inspect.getsource(LongHorizonBenchmark._first_review)
    assert source.count("check=lambda: budget.stop_reason(deadline, purpose=purpose)") >= 2


def test_gap_ids_are_stable_across_wording_punctuation_and_bullets():
    assert normalize_gap_id("- Missing boundary case.") == normalize_gap_id("missing   boundary case")
    assert normalize_gap_id("Missing boundary case") != normalize_gap_id("Different gap")
