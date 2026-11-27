"""Regression controls for suite scheduling, CLI budgets and frozen views."""

from __future__ import annotations

import json
import stat
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from jiuwenswarm.pact_core.runtime import RuntimeContractError
from jiuwenswarm.pact_core.runtime.types import CandidateRef, ReviewEvidence
from jiuwenswarm.pact_core.schemas import TurnResult
from test_suite_flow import PLAN, SuiteAgent, plugin, trial

from jiuwenswarm.lh import Capabilities, ExecutionBinding, LongHorizonBenchmark
from jiuwenswarm.lh.cli import arguments_from_namespace, build_parser
from jiuwenswarm.lh.file_artifacts import FileArtifacts, Root, Snapshot
from jiuwenswarm.lh.plugins.generic import GenericPlugin
from jiuwenswarm.lh.progress import ReviewProgressGuard

# Five to eight git-backed Core turns take roughly 100s on the slowest
# validation host, so the default 120s fixture budget expires under parallel
# load. These controls assert stop reasons, not budget exhaustion.
SLOW_CORE_BUDGET_SECONDS = 600


def reviewed_state(root: Path, worker: int, *, reason: str = "feedback_recorded"):
    candidate = CandidateRef("work", Path(f"candidate-{worker}.json"), f"checkpoint-{worker}", "same-tree")
    artifact = root / f"review-{worker}.md"
    artifact.write_text("Fix the same public behavior.\n")
    return SimpleNamespace(
        candidate=candidate,
        review=ReviewEvidence(artifact, candidate, False, "feedback_recorded", (reason,)),
        verification=SimpleNamespace(candidate=candidate, status="failed", check_results=()),
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


def test_three_fresh_current_reviews_can_still_stop_identical_feedback(tmp_path):
    guard = ReviewProgressGuard()
    assert [guard.observe(reviewed_state(tmp_path, n), n) for n in (1, 2, 3)] == [False, False, True]
    rows = guard.evidence()["reviewed_inputs"]
    assert len({row["review"] for row in rows}) == 3
    assert len({row["candidate_checkpoint"] for row in rows}) == 3
    assert len({row["input_sha256"] for row in rows}) == 1


def test_expected_two_worker_review_cadence_counts_reviews_but_missing_round_breaks_streak(tmp_path):
    guard = ReviewProgressGuard(review_interval=2)
    assert [guard.observe(reviewed_state(tmp_path, n), n) for n in (2, 4, 6)] == [False, False, True]
    guard = ReviewProgressGuard(review_interval=2)
    assert not guard.observe(reviewed_state(tmp_path, 2), 2)
    assert not guard.observe(reviewed_state(tmp_path, 6), 6)  # The expected review at Worker 4 is missing.
    assert len(guard.evidence()["reviewed_inputs"]) == 1


def test_polls_and_stale_same_tree_review_do_not_count_new_workers(tmp_path):
    guard = ReviewProgressGuard()
    state = reviewed_state(tmp_path, 1)
    assert not guard.observe(state, 1)
    assert not guard.observe(state, 1)
    for n in (2, 3, 4):
        current = reviewed_state(tmp_path, n)
        current.review = state.review
        assert not guard.observe(current, n)
    assert len(guard.evidence()["reviewed_inputs"]) == 1
    # A later fresh review after unreviewed Workers starts a new streak.
    assert not guard.observe(reviewed_state(tmp_path, 5), 5)
    assert len(guard.evidence()["reviewed_inputs"]) == 1


def test_reused_review_artifact_cannot_count_even_with_rebound_candidate(tmp_path):
    guard = ReviewProgressGuard()
    artifact = tmp_path / "one-review.md"
    artifact.write_text("Fix the same public behavior.\n")
    for n in (1, 2, 3):
        state = reviewed_state(tmp_path, n)
        state.review = replace(state.review, artifact=artifact)
        assert not guard.observe(state, n)
    assert len(guard.evidence()["reviewed_inputs"]) == 1


@pytest.mark.parametrize(
    "reason", ["reviewer_output_missing", "reviewer_protocol_error", "reviewer_mutation", "budget_exhausted"]
)
def test_failed_reviewer_breaks_streak_without_becoming_agent_no_progress(tmp_path, reason):
    guard = ReviewProgressGuard()
    assert not guard.observe(reviewed_state(tmp_path, 1), 1)
    assert not guard.observe(reviewed_state(tmp_path, 2), 2)
    assert not guard.observe(reviewed_state(tmp_path, 3, reason=reason), 3)
    assert not guard.evidence()["reviewed_inputs"]
    assert [guard.observe(reviewed_state(tmp_path, n), n) for n in (4, 5, 6)] == [False, False, True]


def test_changed_inputs_missing_verification_and_worker_gaps_break_or_delay_streak(tmp_path):
    guard = ReviewProgressGuard()
    assert not guard.observe(reviewed_state(tmp_path, 1), 1)
    missing = reviewed_state(tmp_path, 2)
    missing.verification = None
    assert not guard.observe(missing, 2)
    # The same completed Worker becomes countable once its evidence settles.
    missing.verification = reviewed_state(tmp_path, 2).verification
    assert not guard.observe(missing, 2)
    changed = reviewed_state(tmp_path, 3)
    changed.constraints = ("New constraint",)
    assert not guard.observe(changed, 3)
    assert not guard.observe(reviewed_state(tmp_path, 5), 5)
    assert len(guard.evidence()["reviewed_inputs"]) == 1


def test_review_wording_changes_do_not_reset_gap_progress_by_themselves(tmp_path):
    guard = ReviewProgressGuard()
    assert not guard.observe(reviewed_state(tmp_path, 1), 1)
    assert not guard.observe(reviewed_state(tmp_path, 2), 2)
    changed = reviewed_state(tmp_path, 3)
    changed.review.artifact.write_text("Now fix a different public behavior.\n")
    # Gap identity, closed gaps and check improvement decide progress; wording
    # alone must not interrupt a genuine no-progress streak.
    assert guard.observe(changed, 3)
    assert guard.stop_reason == "review_no_progress"


@pytest.mark.parametrize("damage", ["missing", "empty"])
def test_unavailable_review_text_does_not_establish_reviewed_no_progress(tmp_path, damage):
    guard = ReviewProgressGuard()
    for n in (1, 2, 3):
        state = reviewed_state(tmp_path, n)
        if damage == "missing":
            state.review.artifact.unlink()
        else:
            state.review.artifact.write_text("")
        assert not guard.observe(state, n)
    assert not guard.evidence()["reviewed_inputs"]


@pytest.mark.parametrize("missing", [False, True])
@pytest.mark.asyncio
async def test_real_core_one_review_is_not_recounted_at_long_review_interval(tmp_path, git_repo_factory, missing):
    args = replace(
        trial(tmp_path, git_repo_factory),
        review_interval=10,
        max_role_calls=100,
        max_turns=5,
        budget_seconds=SLOW_CORE_BUDGET_SECONDS,
    )
    calls = []

    class Reviewer(SuiteAgent):
        def run_turn(self, request):
            if missing and self.role == "reviewer" and not request.prompt.startswith("[SUITE_"):
                self.calls.append(("failed-core-review", request.workdir, request.prompt))
                return TurnResult(status="output_missing", final_text="", output_source="scripted", exit_code=1)
            return super().run_turn(request)

    configured = GenericPlugin(
        name="third-agent",
        capabilities=Capabilities(True, True, True, True),
        resolve=lambda *_: ExecutionBinding("review-interval-control-v1", {}),
        adapter_factory=lambda role, **_: Reviewer(role, calls, p1_ready=False, expect_memory=False),
    )
    outcome = await LongHorizonBenchmark(args, plugin=configured).run()
    report = json.loads(outcome.selection.read_text())
    assert not outcome.internal_failure, report["internal_failure"]
    assert report["stop_reason"] == "implementation_stalled"
    assert not (args.output / "no-progress-stop.json").exists()
    implementations = [row for row in report["budget"]["calls"] if row["purpose"] == "implementation"]
    reviews = [row for row in report["budget"]["calls"] if row["purpose"] == "feedback"]
    assert len(implementations) >= args.max_turns
    assert len(reviews) == 1
    assert reviews[0]["status"] == ("output_missing" if missing else "completed")
    assert not report["internal_accepted"]
    assert report["external_grade"]["status"] == "not_run"


@pytest.mark.asyncio
async def test_real_core_pauses_repeat_diagnosis_after_two_evidence_free_rounds(tmp_path, git_repo_factory):
    args = replace(
        trial(tmp_path, git_repo_factory),
        max_role_calls=100,
        max_turns=8,
        budget_seconds=SLOW_CORE_BUDGET_SECONDS,
    )

    class UnchangedFailingWorker(SuiteAgent):
        def run_turn(self, request):
            if request.prompt.startswith("[SUITE_PLANNER]"):
                return TurnResult(
                    status="completed", final_text=json.dumps(PLAN), output_source="scripted", exit_code=0
                )
            result = super().run_turn(request)
            if self.role == "worker" and "first implementation" not in request.prompt:
                (request.workdir / "greeting.txt").write_text("wrong\n")
            return result

    # Passing unchanged checks let Core reuse feedback. Failed public checks
    # actually invoke distinct diagnostic Reviews on the configured cadence.
    configured = GenericPlugin(
        name="third-agent",
        capabilities=Capabilities(True, True, True, True),
        resolve=lambda *_: ExecutionBinding("fresh-failed-review-control-v1", {}),
        adapter_factory=lambda role, **_: UnchangedFailingWorker(role, [], p1_ready=False, expect_memory=False),
    )
    outcome = await LongHorizonBenchmark(args, plugin=configured).run()
    report = json.loads(outcome.selection.read_text())
    assert not outcome.internal_failure, report["internal_failure"]
    # Two evidence-free diagnostics pause diagnosis; the retained repair path
    # then stops after consecutive ineffective Worker rounds.
    assert report["stop_reason"] == "implementation_stalled"
    admissions = json.loads((args.workdir / ".pact/bench/trials/suite-trial/review-admissions.json").read_text())
    assert admissions["trial"] == 2
    [counted] = admissions["keys"].values()
    assert counted["count"] == 2 and counted["tree"] == report["core_candidate"]["tree"]
    assert admissions["refusals"] == []
    progress = json.loads((args.output / "review-progress.json").read_text())
    assert progress["diagnosis_paused"] is True
    assert not (args.output / "no-progress-stop.json").exists()
    assert not report["internal_accepted"]
    assert report["external_grade"]["status"] == "not_run"


@pytest.mark.parametrize("workflow", ["suite", "benchmark"])
@pytest.mark.parametrize("value", ["0", "-1"])
def test_cli_rejects_nonpositive_role_limit_instead_of_removing_it(workflow, value):
    options = build_parser().parse_args(
        [
            "--workflow",
            workflow,
            "--task",
            "task.md",
            "--workdir",
            ".",
            "--output",
            "output",
            "--task-id",
            "task",
            "--trial-id",
            "trial",
            "--max-role-calls",
            value,
        ]
    )
    arguments = arguments_from_namespace(options)
    assert arguments.max_role_calls == int(value)
    with pytest.raises(RuntimeContractError, match="max_role_calls"):
        arguments.config()


@pytest.mark.parametrize("workflow", ["suite", "benchmark"])
def test_cli_preserves_positive_explicit_role_limit(workflow):
    options = build_parser().parse_args(
        [
            "--workflow",
            workflow,
            "--task",
            "task.md",
            "--workdir",
            ".",
            "--output",
            "output",
            "--task-id",
            "task",
            "--trial-id",
            "trial",
            "--max-role-calls",
            "7",
        ]
    )
    assert arguments_from_namespace(options).config().max_role_calls == 7


@pytest.mark.asyncio
async def test_actual_generic_suite_keeps_executable_s0_and_detects_mode_only_tampering(tmp_path, git_repo_factory):
    args, calls = trial(tmp_path, git_repo_factory), []
    script = args.workdir / "run-public.sh"
    script.write_text("#!/bin/sh\nprintf 'public-control\\n'\n")
    script.chmod(0o755)
    subprocess.run(["git", "add", "."], cwd=args.workdir, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Public executable fixture"], cwd=args.workdir, check=True, capture_output=True
    )
    outcome = await LongHorizonBenchmark(args, plugin=plugin(calls)).run()
    assert not outcome.internal_failure
    report = json.loads(outcome.selection.read_text())
    artifacts = FileArtifacts()
    snapshot = artifacts.load_stage_snapshot(args.output, report["long_horizon"]["stages"]["s0"])
    frozen = Path(snapshot.roots[0].view_root) / script.name
    assert stat.S_IMODE(frozen.stat().st_mode) == 0o755
    run = subprocess.run([str(frozen)], check=True, capture_output=True, text=True)
    assert run.stdout == "public-control\n"
    proposal_views = {directory for label, directory, _ in calls if label.startswith("[SUITE_PROPOSAL")}
    assert len([label for label, _, _ in calls if label.startswith("[SUITE_PROPOSAL")]) == 3
    assert proposal_views
    # Side calls now execute in one-time copies, so their assigned workdirs are
    # not the frozen view and are removed after the call.  The frozen S0 file
    # keeps its executable mode for the real gates.
    assert all(Path(directory) != Path(snapshot.roots[0].view_root) for directory in proposal_views)
    assert all(not Path(directory).exists() for directory in proposal_views)
    frozen.chmod(0o644)
    with pytest.raises(RuntimeContractError, match="file modes changed"):
        artifacts.validate(snapshot)


def test_legacy_byte_only_snapshot_remains_readable_without_rewriting(tmp_path):
    from jiuwenswarm.lh.evidence import sha256_bytes

    view = tmp_path / "view"
    view.mkdir()
    (view / "public.txt").write_text("public")
    manifest = tmp_path / "manifest.json"
    content = json.dumps({"schema": "long-horizon/snapshot/v1", "files": {"public.txt": sha256_bytes(b"public")}})
    manifest.write_text(content)
    snapshot = Snapshot(str(tmp_path), sha256_bytes(manifest.read_bytes()), (Root(str(view)),))
    FileArtifacts().validate(snapshot)
    assert manifest.read_text() == content
