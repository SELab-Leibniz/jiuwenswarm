# Copyright (c) 2026 Chrys. All rights reserved.

"""Remaining-budget public checking with no models, task answers or containers."""

from __future__ import annotations

import json
import shlex
import sys
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from jiuwenswarm.pact_core import verification
from jiuwenswarm.pact_core.runtime import AcceptancePolicy
from jiuwenswarm.pact_core.schemas import FinalValidationResult, VerificationCheck
from test_control_regressions import GOOD, FixturePlugin, repository

from jiuwenswarm.lh.control_policy import PhasePolicy
from jiuwenswarm.lh.controller import BenchmarkArguments, LongHorizonBenchmark


def phase(tmp_path, now):
    return PhasePolicy(100, 1000, 910, 900, tmp_path / "phase.json", clock=lambda: now[0])


def test_check_uses_original_global_deadline_after_preprocessing(tmp_path):
    now = [350.0]
    policy = phase(tmp_path, now)
    assert policy.deadline_for("check") == 1000
    assert policy.admissions[-1]["admitted_seconds"] == 650
    now[0] = 720
    assert policy.deadline_for("check") == 1000
    assert policy.admissions[-1]["admitted_seconds"] == 280
    payload = json.loads(policy.path.read_text())
    assert payload["check_cap_seconds"] is None
    assert payload["check_reserve_seconds"] == 28.5
    assert payload["finish_reserve_seconds"] == 28.5
    assert payload["finish_reserve_source"] == "default"
    assert payload["check_budget_policy"] == "task_remaining_global_v1"


def test_positive_sub30_budget_is_admitted_and_expired_is_not_extended(tmp_path):
    now = [999.75]
    policy = phase(tmp_path, now)
    assert policy.deadline_for("check") == 1000
    assert policy.admissions[-1]["admitted_seconds"] == 0.25
    now[0] = 1001
    assert policy.deadline_for("check") == 1000
    assert policy.admissions[-1]["admitted_seconds"] == 0


def test_other_phase_reservations_use_the_dynamic_finish_reserve(tmp_path):
    policy = phase(tmp_path, [350])
    assert policy.deadline_for("p0") == 376
    assert policy.deadline_for("planning") == 485
    # The tight branch keeps the Worker launchable with only the finish reserve;
    # A=600 for this sub-hour window would otherwise drive the cutoff behind now.
    assert policy.deadline_for("p1") == 826
    assert policy.admissions[-1]["acceptance_reserve_skipped"] is True


@pytest.mark.parametrize("now,expected_calls", [(350.0, 1), (999.75, 1), (1000.0, 0), (1001.0, 0)])
def test_controller_forwards_original_deadline_or_refuses_expired(tmp_path, now, expected_calls):
    controller = object.__new__(LongHorizonBenchmark)
    controller._rpc_public_check = False
    controller._phase_policy = phase(tmp_path, [now])
    controller._budget = SimpleNamespace(clock=lambda: now)
    controller._abort = threading.Event()
    controller._gate = SimpleNamespace(observed=lambda _: False, record=Mock(), selected=None)
    controller._invocation = SimpleNamespace(state=None)
    state = SimpleNamespace(candidate=object(), verification=None, work_id="synthetic", revision=4, status="active")
    runtime = SimpleNamespace(verify_candidate=Mock(return_value=state))
    assert controller._check_candidate(runtime, state, "p1") is state
    assert runtime.verify_candidate.call_count == expected_calls
    if expected_calls:
        runtime.verify_candidate.assert_called_once_with("synthetic", expected_revision=4, deadline=1000)


@pytest.mark.parametrize("profile,expected", [("bounded", 4100.0), ("legacy", 1800.0)])
def test_core_policy_uses_full_task_total_only_for_bounded_profile(tmp_path, profile, expected):
    repo = repository(tmp_path)
    task = tmp_path / "instruction.txt"
    task.write_text("Synthetic requirement.")
    args = BenchmarkArguments(
        task,
        repo,
        tmp_path / "out",
        "task",
        "trial",
        budget_seconds=4100,
        control_profile=profile,
        verify_command="fixture-command",
    )
    controller = LongHorizonBenchmark(args, plugin=FixturePlugin({"steps": [], "calls": []}))
    controller._require_same = Mock()
    controller._phase = lambda _: "created"
    state = SimpleNamespace(delivery=None, revision=1)
    runtime = SimpleNamespace(workspace=repo, create_work=Mock(return_value=state))
    controller._open_work(runtime, {}, {"fingerprint": "fixture"}, "Synthetic requirement.")
    policy = runtime.create_work.call_args.kwargs["policy"]
    assert isinstance(policy, AcceptancePolicy)
    assert policy.verify_timeout_seconds == expected
    assert policy.final_verify_timeout_seconds == expected


@pytest.mark.asyncio
async def test_real_core_receives_more_than_old30_and1800_caps(tmp_path):
    repo = repository(tmp_path)
    task = tmp_path / "instruction.txt"
    task.write_text("Synthetic requirement; preserve this exact original task.")
    state = {"steps": [(GOOD, "completed"), (GOOD, "completed")], "calls": []}
    args = BenchmarkArguments(
        task,
        repo,
        tmp_path / "out",
        "task",
        "trial",
        budget_seconds=4100,
        budget_started_at_unix=time.time() - 200,
        max_turns=2,
        control_profile="bounded",
        verify_command="fixture-command",
        worktree_root=tmp_path / "worktrees",
    )
    controller = LongHorizonBenchmark(args, plugin=FixturePlugin(state))
    timeouts = []

    def command(*_args, timeout_seconds, deadline=None, **_kwargs):
        timeouts.append((timeout_seconds, deadline))
        return "passed", 0, "", "", 0.0

    with patch.object(verification, "_run_task_command", side_effect=command):
        await controller.run()
    assert timeouts
    # Direct host checks retain the original per-command ceiling and the
    # original hard clock; a review's own internal re-check still uses the
    # review admission window and keeps its soft model boundary.
    assert all(1800 < seconds < 3900 for seconds, _ in timeouts)
    assert any(deadline == args.original_deadline_unix for _, deadline in timeouts)
    assert all(
        deadline in (args.original_deadline_unix, args.soft_deadline_unix) for _, deadline in timeouts
    )
    assert controller._invocation.state.policy.verify_timeout_seconds == 4100
    frozen = controller._cli_frozen
    assert frozen["control_profile"] == "bounded-phases-public-candidate-gate-v3-check-outcome-routing"


def public_verification(tmp_path, *, deadline, command="fixture-command", timeout_seconds=4100):
    return verification.run_verification(
        workspace=tmp_path,
        workdir=tmp_path,
        checkpoint_commit="synthetic-checkpoint",
        baseline_commit="synthetic-baseline",
        namespace_violations=[],
        required_artifacts=[],
        command=command,
        timeout_seconds=timeout_seconds,
        loop_dir=tmp_path,
        round_number=1,
        patch_path=tmp_path / "patch",
        deadline=deadline,
    )


def test_structural_checks_expiring_budget_do_not_spawn_task_command(tmp_path):
    def builtins(**_kwargs):
        time.sleep(0.06)
        return [VerificationCheck("fixture", True, "")]

    with (
        patch.object(verification, "run_builtin_checks", side_effect=builtins),
        patch.object(verification.subprocess, "Popen") as spawn,
    ):
        result = public_verification(tmp_path, deadline=time.time() + 0.03)
    assert result.status == "timeout"
    assert "deadline reached before verification command" in result.stderr_tail
    spawn.assert_not_called()


def test_already_expired_budget_starts_no_structural_or_task_process(tmp_path):
    with (
        patch.object(verification, "run_builtin_checks") as builtins,
        patch.object(verification.subprocess, "Popen") as spawn,
    ):
        result = public_verification(tmp_path, deadline=time.time() - 1)
    assert result.status == "timeout"
    builtins.assert_not_called()
    spawn.assert_not_called()


def test_after_structural_delay_real_command_is_killed_at_original_deadline(tmp_path):
    marker = tmp_path / "late"
    code = "import time,pathlib;time.sleep(2);pathlib.Path(" + repr(str(marker)) + ").write_text('late')"

    def builtins(**_kwargs):
        time.sleep(0.15)
        return [VerificationCheck("fixture", True, "")]

    started = time.time()
    with patch.object(verification, "run_builtin_checks", side_effect=builtins):
        result = public_verification(
            tmp_path, deadline=started + 0.45, command=shlex.join([sys.executable, "-c", code])
        )
    assert result.status == "timeout"
    assert time.time() - started < 1.2
    assert not marker.exists()


def test_popen_delay_is_deducted_again_before_communicate(tmp_path):
    now = [100.0]
    process = Mock()
    process.returncode = 0
    process.communicate.return_value = ("", "")

    def spawn(*_args, **_kwargs):
        now[0] = 109.75
        return process

    with (
        patch.object(verification.time, "time", side_effect=lambda: now[0]),
        patch.object(verification.subprocess, "Popen", side_effect=spawn),
    ):
        result = verification._run_task_command(
            "fixture-command", workdir=tmp_path, timeout_seconds=4100, env_extra={}, deadline=110
        )
    assert result[0] == "passed"
    process.communicate.assert_called_once_with(timeout=0.25)


def test_final_validation_expired_does_not_spawn(tmp_path):
    with patch.object(verification.subprocess, "Popen") as spawn:
        result = verification.run_final_validation(
            command="fixture-command",
            workdir=tmp_path,
            timeout_seconds=4100,
            loop_dir=tmp_path,
            baseline_commit="baseline",
            candidate_commit="candidate",
            patch_path=tmp_path / "patch",
            deadline=time.time() - 1,
        )
    assert result.status == "timeout"
    spawn.assert_not_called()


def test_final_stage_passes_original_deadline_through_worktree_preparation(tmp_path):
    from jiuwenswarm.pact_core import _execution_finalization as finalization

    deadline = time.time() + 10
    patch_path = tmp_path / "patch"
    paths = SimpleNamespace(
        loop_dir=tmp_path,
        final_patch=patch_path,
        final_validation=tmp_path / "validation.json",
        final_validation_log=tmp_path / "validation.log",
        events=tmp_path / "events",
    )

    @contextmanager
    def worktree(**_kwargs):
        yield tmp_path

    store = SimpleNamespace(
        export_patch=lambda *_: patch_path.write_text("fixture"), final_validation_worktree=worktree
    )
    io = SimpleNamespace(write_json=Mock(), write_text=Mock(), append_event=Mock())
    initial = SimpleNamespace(worker_worktree=tmp_path, baseline_commit="baseline")
    with patch.object(finalization, "run_final_validation", return_value=FinalValidationResult("passed")) as run:
        result = finalization.run_final_stage(
            io,
            store=store,
            paths=paths,
            initialization=initial,
            checkpoint="candidate",
            command="fixture-command",
            timeout_seconds=4100,
            deadline=deadline,
        )
    assert result == "passed"
    assert run.call_args.kwargs["deadline"] == deadline


def test_explicit_cancellation_kills_process_group_and_preserves_primary(tmp_path):
    import signal
    import subprocess

    original = subprocess.Popen.communicate
    calls = []

    def interrupt_once(process, *args, **kwargs):
        calls.append(process)
        if len(calls) == 1:
            process.stdout.readline()
            raise KeyboardInterrupt("synthetic cancellation")
        return original(process, *args, **kwargs)

    command = shlex.join([sys.executable, "-c", "import time;print('ready',flush=True);time.sleep(120)"])
    with (
        patch.object(subprocess.Popen, "communicate", new=interrupt_once),
        patch.object(verification.os, "killpg", wraps=verification.os.killpg) as kill,
    ):
        with pytest.raises(KeyboardInterrupt, match="synthetic cancellation"):
            verification._run_task_command(
                command, workdir=tmp_path, timeout_seconds=4100, env_extra={}, deadline=time.time() + 30
            )
    kill_calls = [call.args for call in kill.call_args_list if call.args[1] == signal.SIGKILL]
    assert kill_calls == [(calls[0].pid, signal.SIGKILL)]
    assert calls[0].returncode is not None
