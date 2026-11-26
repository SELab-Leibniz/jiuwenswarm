"""Exercise the scheduling counters with sealed candidates and repeatable review evidence."""

import json
from dataclasses import replace
from types import SimpleNamespace

from jiuwenswarm.lh.controller import LongHorizonBenchmark
from jiuwenswarm.lh.progress import ReviewProgressGuard
from jiuwenswarm.lh.review_admissions import ReviewAdmissions, acceptance_fuse_reached
from jiuwenswarm.pact_core.runtime.types import CandidateRef, ReviewEvidence, VerificationEvidence


def candidate(root, number, passed, *, total=72, tree=None, gaps=None, status=None):
    ref = CandidateRef("work", root / f"c{number}", f"checkpoint-{number}", tree or f"tree-{number}")
    results = tuple(
        {"name": f"criterion-{index}", "version": 1, "status": "passed" if index < passed else "failed"}
        for index in range(total)
    )
    verified = VerificationEvidence(
        root / f"check-{number}", ref, status or ("passed" if passed == total else "failed"), "public-check",
        0 if passed == total else 1, "", "", results,
    )
    review = None
    if gaps is not None:
        artifact = root / f"review-{number}"
        artifact.write_text(f"<pact-gaps>{json.dumps(gaps)}</pact-gaps>")
        review = ReviewEvidence(artifact, ref, False, "needs repair", ("feedback_recorded",))
    return SimpleNamespace(
        work_id="work", revision=number, contract_revision=1, checks=(), user_decisions=(),
        candidate=ref, review=review, verification=verified, acceptance=None, delivery=None,
    )


def calls(count):
    return [{"purpose": "implementation", "status": "completed"}] * count


def test_more_than_64_productive_workers_and_32_acceptance_reviews(tmp_path):
    path = tmp_path / "ledger.json"
    ledger = ReviewAdmissions(path, work_id="work", diagnosis_limit=2, acceptance_limit=2)
    baseline = candidate(tmp_path, 0, 0)
    assert ledger.worker_progress(baseline, []) == (0, 0)
    for worker in range(1, 73):
        if worker == 37:
            ledger = ReviewAdmissions(path, work_id="work", diagnosis_limit=2, acceptance_limit=2)
        state = candidate(tmp_path, worker, worker)
        for purpose in ("diagnostic", "acceptance"):
            assert ledger.admit(state, purpose=purpose)["admitted"]
        assert ledger.worker_progress(state, calls(worker)) == (0, worker)
    report = ledger.payload()
    [archive] = tmp_path.glob("ledger.inputs-*.json")
    saved = json.loads(archive.read_bytes())
    assert saved["through_admission"] == 128 and len(saved["keys"]) == 64
    assert len(report["keys"]) == 8
    assert report["trial"] == 144
    assert report["purposes"]["acceptance"]["count"] == 72
    assert report["consecutive"] == {"diagnosis": 1, "acceptance": 1}
    assert not acceptance_fuse_reached(report)


def test_streak_survives_resume_and_resets_once_on_real_progress(tmp_path):
    path = tmp_path / "ledger.json"
    ledger = ReviewAdmissions(path, work_id="work", acceptance_limit=2)
    first = candidate(tmp_path, 1, 0, gaps=["parser", "cache"])
    assert ledger.worker_progress(first, calls(1)) == (1, 0)
    assert ledger.admit(first, purpose="acceptance")["admitted"]
    second = candidate(tmp_path, 2, 0, gaps=["parser", "cache"])
    assert ledger.worker_progress(second, calls(2)) == (2, 0)
    assert ledger.admit(second, purpose="acceptance")["admitted"]
    ledger = ReviewAdmissions(path, work_id="work", acceptance_limit=2)
    assert acceptance_fuse_reached(ledger.payload())
    assert ledger.worker_progress(second, calls(2)) == (2, 0)
    assert not ledger.admit(second, purpose="acceptance")["admitted"]
    recovered = candidate(tmp_path, 3, 0, gaps=["cache"], tree=second.candidate.tree)
    assert ledger.worker_progress(recovered, calls(3)) == (0, 1)
    assert ledger.worker_progress(first, calls(1)) == (0, 1)
    assert ledger.admit(recovered, purpose="acceptance")["admitted"]
    assert ledger.payload()["purposes"]["acceptance"]["count"] == 3
    third = candidate(tmp_path, 4, 0, gaps=["cache"])
    assert ledger.worker_progress(third, calls(4)) == (1, 1)
    changed = candidate(tmp_path, 5, 0, gaps=["a rewritten issue"])
    assert ledger.worker_progress(changed, calls(5)) == (2, 1)
    known_good = candidate(tmp_path, 6, 1)
    assert ledger.worker_progress(known_good, calls(6)) == (0, 2)
    assert ledger.worker_progress(known_good, calls(6)) == (0, 2)


def test_stale_verification_and_disappearing_or_malformed_checks_do_not_reset(tmp_path):
    ledger = ReviewAdmissions(tmp_path / "ledger.json", work_id="work")
    first = candidate(tmp_path, 1, 1, gaps=["original issue"])
    assert ledger.worker_progress(first, calls(1)) == (1, 0)
    next_state = candidate(tmp_path, 2, 72, gaps=[])
    next_state.review.artifact.write_text("<pact-gaps>{broken}</pact-gaps>")
    next_state.verification = replace(next_state.verification, candidate=first.candidate)
    assert ledger.worker_progress(next_state, calls(2)) == (2, 0)
    next_state.verification = replace(next_state.verification, candidate=next_state.candidate, check_results=())
    assert ledger.worker_progress(next_state, calls(2)) == (2, 0)


def test_recovered_review_lane_reopens_only_once_after_reload(tmp_path):
    path = tmp_path / "progress.json"
    guard = ReviewProgressGuard(path=path)
    guard._diagnosis_paused = True
    guard._stop_reason = "review_no_progress_same_gap"
    guard._no_progress_rounds = guard._same_gap_rounds = 3
    guard.recover(1)
    restored = ReviewProgressGuard(path=path)
    assert not restored.diagnosis_paused and restored.stop_reason is None
    assert restored.evidence()["same_gap_rounds"] == restored.evidence()["no_progress_rounds"] == 0
    restored._no_progress_rounds = 1
    restored.recover(1)
    assert restored.evidence()["no_progress_rounds"] == 1


def test_audited_gap_closure_reopens_acceptance_across_resume(tmp_path):
    path = tmp_path / "ledger.json"
    ledger = ReviewAdmissions(path, work_id="work", acceptance_limit=1)
    first = candidate(tmp_path, 1, 0, gaps=["baseline"])
    assert ledger.worker_progress(first, calls(1), coverage_gaps=["a", "b"]) == (1, 0)
    assert ledger.admit(first, purpose="acceptance")["admitted"]
    unchanged = candidate(tmp_path, 2, 0, gaps=["baseline"])
    assert ledger.worker_progress(unchanged, calls(2), coverage_gaps=["a", "b"]) == (2, 0)
    assert not ledger.admit(unchanged, purpose="acceptance")["admitted"]
    reopened = ReviewAdmissions(path, work_id="work", acceptance_limit=1)
    improved = candidate(tmp_path, 3, 0, gaps=["baseline"])
    assert reopened.worker_progress(improved, calls(3), coverage_gaps=["b"]) == (0, 1)
    assert reopened.admit(improved, purpose="acceptance")["admitted"]
    assert reopened.worker_progress(improved, calls(3), coverage_gaps=["b"]) == (0, 1)
    assert not reopened.admit(improved, purpose="acceptance")["admitted"]


def test_unattended_streak_uses_outstanding_questions_and_verified_progress(tmp_path):
    history = [{"kind": "initial_clarification"}]
    ledger = ReviewAdmissions(tmp_path / "ledger.json", work_id="work")
    controller = SimpleNamespace(
        _progress=lambda state: {"clarification_attempts": history}, _review_admissions=ledger,
        _args=SimpleNamespace(budget_seconds=600, clarification_budget_ratio=0.2, clarification_max_rounds=1),
    )
    budget = SimpleNamespace(clock=lambda: 1, snapshot=lambda: {"calls": []}, stop_reason=lambda deadline: None)
    window = SimpleNamespace(deadline=601)
    def allowance(state):
        return LongHorizonBenchmark._clarification_budget_status(controller, state, budget, window)
    first = candidate(tmp_path, 1, 0)
    first.unresolved_assumptions = ({"question": "A"}, {"question": "B"})
    baseline = allowance(first)
    assert baseline["rounds_used"] == 0 and baseline["available"]
    history.append({"kind": "unattended_resolution", "reset_before": True,
                    "open_questions": baseline["open_questions"], "progress_epoch": 0})
    assert allowance(first)["reason"] == "clarification_stalled"
    first.unresolved_assumptions = ({"question": "B"},)
    assert allowance(first)["rounds_used"] == 0
    history.append({"kind": "unattended_resolution", "reset_before": True, "open_questions": ["b"], "progress_epoch": 0})
    assert allowance(first)["reason"] == "clarification_stalled"
    verified = candidate(tmp_path, 2, 1)
    verified.unresolved_assumptions = first.unresolved_assumptions
    assert allowance(verified)["available"] and allowance(verified)["rounds_used"] == 0
