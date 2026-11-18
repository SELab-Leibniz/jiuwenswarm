# Copyright (c) 2026 Chrys. All rights reserved.

"""No models, hidden tests, task answers, or network. Real Core draft/check/export paths."""

import asyncio
import json
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from pact_core.benchmark.types import BenchConfig
from pact_core.runtime import AcceptancePolicy, WorkRuntime
from pact_core.schemas import AdapterProbe, TurnResult

from jiuwenswarm.lh.budget_origin import create_budget
from jiuwenswarm.lh.candidate_gate import PublicCandidateGate
from jiuwenswarm.lh.control_policy import PhasePolicy
from jiuwenswarm.lh.controller import BenchmarkArguments, LongHorizonBenchmark
from jiuwenswarm.lh.file_artifacts import Clarification, Root, Snapshot
from jiuwenswarm.lh.plugins.generic import GenericPlugin
from jiuwenswarm.lh.ports import Capabilities, ExecutionBinding, RoleBoundary
from jiuwenswarm.lh.suite import original_task_pair, prepare_suite

GOOD = "def calculate(x):\n    return x + 1\n"
BAD = "def calculate(x):\n    return (\n"
VERIFY = shlex.join([sys.executable, "-c", "import ast; ast.parse(open('module.py').read())"])


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def repository(root, code=GOOD):
    repo = root / "repo"
    repo.mkdir()
    (repo / "module.py").write_text(code)
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "test")
    git(repo, "config", "user.email", "test@local.invalid")
    git(repo, "add", "-A")
    git(repo, "-c", "commit.gpgsign=false", "commit", "-qm", "fixture")
    return repo


class FakeAdapter:
    budget_failure = configuration_failure = None
    id = "fixture-worker"

    def __init__(self, state, role):
        self.state, self.role = state, role

    def probe(self):
        return AdapterProbe(self.id, True)

    async def cancel_current_turn(self):
        pass

    def run_turn(self, request):
        if self.role != "worker":
            return TurnResult("completed", "Review found incomplete original task.", "fixture", 0)
        index = len(self.state["calls"])
        self.state["calls"].append(request)
        code, status = self.state["steps"][index]
        (request.workdir / "module.py").write_text(code)
        if isinstance(status, Exception):
            raise status
        return TurnResult(
            status, "Partial implementation; no task-success claim.", "fixture", 0 if status == "completed" else 130
        )


class FixturePlugin(GenericPlugin):
    def __init__(self, state):
        super().__init__(
            name="fixture",
            capabilities=Capabilities(True, True, True, True),
            resolve=lambda w, a: ExecutionBinding("fixture-v1", {}),
            adapter_factory=lambda role, **kwargs: FakeAdapter(state, role),
        )

    async def clarify(self, context, original_task):
        return Clarification("")

    async def recall(self, context, original_task):
        return "Frozen unrelated fixture memory."

    async def generate_pact_input(self, context, result, original_task, *, hints):
        return original_task_pair(original_task)


class CoreRegression(unittest.IsolatedAsyncioTestCase):
    async def run_fixture(self, baseline, steps, *, expired=False):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        repo = repository(root, baseline)
        task = root / "instruction.txt"
        task.write_text("Implement calculate correctly; preserve original requirements.")
        state = {"steps": steps, "calls": []}
        args = BenchmarkArguments(
            task=task,
            workdir=repo,
            output=root / "output",
            task_id="fixture",
            trial_id="case",
            budget_seconds=120,
            turn_timeout_seconds=120,
            budget_started_at_unix=time.time() - 121 if expired else time.time(),
            max_turns=2,
            control_profile="bounded",
            verify_command=VERIFY,
            worktree_root=root / "worktrees",
        )
        controller = LongHorizonBenchmark(args, plugin=FixturePlugin(state))
        localization = {"status": "unavailable", "text": "", "artifacts": []}
        with patch("jiuwenswarm.lh.suite.localize_suite", AsyncMock(return_value=localization)):
            outcome = await controller.run()
        selection = json.loads((root / "output/selection.json").read_text())
        return root, repo, state, outcome, selection

    async def test_compile_failed_p0_then_p1_timeout_exports_only_verified_baseline(self):
        root, repo, calls, outcome, s = await self.run_fixture(GOOD, [(BAD, "timeout"), (BAD, "timeout")])
        self.assertEqual(len(calls["calls"]), 2)
        self.assertTrue(outcome.candidate_available)
        self.assertEqual(s["public_candidate_gate"]["selected_stage"], "s0")
        self.assertEqual(s["generation_status"], "candidate_validated")
        self.assertEqual(s["external_grade"], {"status": "not_run", "reward": None})
        self.assertFalse(s["internal_accepted"])
        self.assertEqual((Path(s["export"]["snapshot"]) / "module.py").read_text(), GOOD)
        self.assertNotEqual(s["core_candidate"]["tree"], s["candidate"]["tree"])
        self.assertEqual((repo / "module.py").read_text(), GOOD)

    async def test_good_p0_bad_p1_keeps_good_p0(self):
        _, _, _, outcome, s = await self.run_fixture(BAD, [(GOOD, "completed"), (BAD, "completed")])
        self.assertTrue(outcome.candidate_available)
        self.assertEqual(s["public_candidate_gate"]["selected_stage"], "p0")
        self.assertEqual((Path(s["export"]["snapshot"]) / "module.py").read_text(), GOOD)
        self.assertFalse(s["internal_accepted"])

    async def test_no_valid_candidate_never_exports_or_accepts(self):
        _, _, _, outcome, s = await self.run_fixture(BAD, [(BAD, "timeout"), (BAD, "timeout")])
        self.assertFalse(outcome.candidate_available)
        self.assertIsNone(s["export"])
        self.assertIsNone(s["candidate"])
        self.assertEqual(s["generation_status"], "no_valid_candidate")
        self.assertFalse(s["internal_accepted"])
        self.assertFalse(s["delivered"])

    async def test_partial_p1_must_pass_public_check_before_export(self):
        _, _, _, outcome, s = await self.run_fixture(BAD, [(BAD, "timeout"), (GOOD, "timeout")])
        self.assertTrue(outcome.candidate_available)
        self.assertEqual(s["public_candidate_gate"]["selected_stage"], "p1")
        self.assertFalse(s["internal_accepted"])
        self.assertEqual((Path(s["export"]["snapshot"]) / "module.py").read_text(), GOOD)

    async def test_expired_global_budget_admits_no_model_call(self):
        _, _, calls, outcome, s = await self.run_fixture(GOOD, [], expired=True)
        self.assertEqual(calls["calls"], [])
        self.assertFalse(outcome.candidate_available)
        self.assertIsNone(s["export"])

    async def test_sse_partial_error_preserves_tools_without_replay(self):
        exc = RuntimeError("[181001] model call failed, reason: openAI API async stream error: ReadError")
        root, _, calls, outcome, s = await self.run_fixture(GOOD, [(BAD, exc), (GOOD, "timeout")])
        self.assertEqual(len(calls["calls"]), 2)  # P0 once, separately scheduled P1 once
        self.assertTrue(outcome.candidate_available)
        reports = list(root.rglob("transport-failure.json"))
        self.assertEqual(len(reports), 1)
        diag = json.loads(reports[0].read_text())
        self.assertFalse(diag["replayed"])
        self.assertEqual(diag["retries"], 0)
        self.assertFalse(s["internal_accepted"])

    async def test_bounded_planning_preserves_memory_and_original_task(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "fixture.txt").write_text("read only")
        snapshot = Snapshot(str(root), "fixture", (Root(str(root)),))
        cancelled = []

        class Slow(FixturePlugin):
            async def clarify(self, *args):
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.append("clarification")

            async def generate_pact_input(self, *args, **kwargs):
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.append("planning")

        plugin = Slow({"steps": [], "calls": []})
        context = SimpleNamespace(snapshot=snapshot)
        loc = {"status": "unavailable", "text": "", "artifacts": []}
        with patch("jiuwenswarm.lh.suite.localize_suite", AsyncMock(return_value=loc)):
            result, prep = await asyncio.wait_for(
                prepare_suite(
                    plugin,
                    context,
                    "Original authoritative task.",
                    optional_seconds=0.03,
                    planning_deadline=time.time() + 0.08,
                ),
                timeout=3,
            )
        self.assertCountEqual(cancelled, ["clarification", "planning"])
        self.assertEqual(prep["goal_contract"]["goal"], "Original authoritative task.")
        self.assertEqual(prep["memory"]["text"], "Frozen unrelated fixture memory.")
        self.assertEqual(prep["planning_status"], "degraded_original_task")
        self.assertEqual(result.status, "degraded")


class BudgetAndTransport(unittest.TestCase):
    def test_deadlines_reserve_repair_checks_and_never_extend_global(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = [350.0]
            p = PhasePolicy(100, 1000, 910, 900, Path(tmp) / "policy.json", clock=lambda: now[0])
            planning = p.deadline_for("planning")
            self.assertLessEqual(planning, 910 - 315 - 30 - 20)
            self.assertLessEqual(p.deadline_for("p1"), 860)
            now[0] = 999
            self.assertLessEqual(p.deadline_for("check"), 1000)
            self.assertLessEqual(p.deadline_for("manager"), 1000)

    def test_existing_origin_helper_does_not_restart_trial_clock(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BenchConfig(task_id="fixture", trial_id="origin", budget_seconds=900, turn_timeout_seconds=900)
            b = create_budget(Path(tmp), config, "work", 100.0, clock=lambda: 250.0)
            self.assertEqual(b.snapshot()["started_at"], 100)
            self.assertEqual(b.snapshot()["deadline"], 1000)
            again = create_budget(Path(tmp), config, "work", 100.0, clock=lambda: 500.0)
            self.assertEqual(again.snapshot()["deadline"], 1000)

    def test_permission_denial_is_not_retried_or_normalized(self):
        adapter = SimpleNamespace(run_turn=lambda r: (_ for _ in ()).throw(PermissionError("denied")))
        with self.assertRaises(PermissionError):
            RoleBoundary(adapter).run_turn(SimpleNamespace())

    def test_sse_receipt_does_not_log_headers_or_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            count = [0]

            def call(request):
                count[0] += 1
                raise RuntimeError(
                    "[181001] model call failed, reason: openAI API async stream error: ReadError Authorization: Bearer sk-private"
                )

            request = SimpleNamespace(artifact_dir=Path(tmp), deadline=time.time() + 1)
            result = RoleBoundary(SimpleNamespace(run_turn=call)).run_turn(request)
            self.assertEqual(result.status, "error")
            self.assertEqual(count[0], 1)
            data = (Path(tmp) / "transport-failure.json").read_text()
            self.assertNotIn("sk-private", data)
            self.assertNotIn("Authorization", data)

    def test_normalized_native_terminal_failure_is_recorded_without_secrets(self):
        from jiuwenswarm.lh.transport_failure import record_native_stream_failure

        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            terminal = {
                "status": "failed",
                "request_id": "local-request",
                "session_id": "local-session",
                "error": {
                    "code": "RUNTIME_ERROR",
                    "message": "[181001] model call failed, reason: openAI API async stream error: ReadError Authorization: Bearer sk-private",
                },
            }
            (p / "terminal-result.json").write_text(json.dumps(terminal))
            self.assertTrue(record_native_stream_failure(p, stage="worker", deadline=time.time() + 1))
            text = (p / "transport-failure.json").read_text()
            self.assertNotIn("Authorization", text)
            self.assertNotIn("sk-private", text)
            diag = json.loads(text)
            self.assertEqual(diag["origin"], "native_terminal_result")
            self.assertFalse(diag["replayed"])
            self.assertIsNone(diag["http_status"])

    def test_real_public_verifier_respects_deadline_before_or_after_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = repository(root)
            runtime = WorkRuntime(repo, worktree_root=root / "worktrees")
            late = root / "late.txt"
            started = root / "started.txt"
            code = (
                "import time,pathlib;pathlib.Path("
                + repr(str(started))
                + ").write_text('started');time.sleep(2);pathlib.Path("
                + repr(str(late))
                + ").write_text('late')"
            )
            state = runtime.create_work(
                request_id="deadline",
                work_id="deadline",
                contract=original_task_pair("Fixture task")["goal_contract"],
                policy=AcceptancePolicy(
                    verify_command=shlex.join([sys.executable, "-c", code]), allow_unverified=False
                ),
            )
            state = runtime.verify_candidate(
                state.work_id, expected_revision=state.revision, deadline=time.time() + 1.0
            )
            self.assertEqual(state.verification.status, "timeout")
            if not started.exists():
                self.assertIn("deadline reached before public verification", state.verification.stderr_tail)
            self.assertFalse(late.exists())

    def test_verifier_kills_an_actually_started_command(self):
        from pact_core.verification import run_verification

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = repository(root)
            head = git(repo, "rev-parse", "HEAD")
            started, late = root / "pid.txt", root / "late.txt"
            code = (
                "import os,time,pathlib;pathlib.Path("
                + repr(str(started))
                + ").write_text(str(os.getpid()));time.sleep(2);pathlib.Path("
                + repr(str(late))
                + ").write_text('late')"
            )
            result = run_verification(
                workspace=repo,
                workdir=repo,
                checkpoint_commit=head,
                baseline_commit=head,
                namespace_violations=[],
                required_artifacts=[],
                command=shlex.join([sys.executable, "-c", code]),
                timeout_seconds=0.5,
                loop_dir=root,
                round_number=1,
                patch_path=root / "patch.diff",
            )
            self.assertEqual(result.status, "timeout")
            self.assertTrue(started.exists())
            self.assertFalse(late.exists())
            pid = int(started.read_text())
            state = subprocess.run(["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True)
            self.assertTrue(state.returncode != 0 or state.stdout.strip().startswith("Z"))


class SideCallCancellationRegression(unittest.IsolatedAsyncioTestCase):
    async def test_optional_cancel_is_local_but_user_cancel_still_propagates(self):
        import threading

        from jiuwenswarm.lh.file_artifacts import FileArtifacts
        from jiuwenswarm.lh.ports import ClarificationContext

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            view = root / "view"
            view.mkdir()
            (view / "source.txt").write_text("immutable")
            parent = threading.Event()
            captured = []

            class NativeStyleAdapter:
                id = "native-style"
                budget_failure = configuration_failure = None

                def __init__(self, abort):
                    self.abort = abort
                    captured.append(self)

                def probe(self):
                    return AdapterProbe(self.id, True)

                def run_turn(self, request):
                    while not self.abort.is_set():
                        time.sleep(0.002)
                    return TurnResult("error", "", "fixture", 1)

                async def cancel_current_turn(self):
                    self.abort.set()

            plugin = GenericPlugin(
                name="cancel-fixture",
                capabilities=Capabilities(True, True, True, True),
                resolve=lambda w, a: ExecutionBinding("cancel-v1", {}),
                adapter_factory=lambda abort, **kwargs: NativeStyleAdapter(abort),
            )
            args = BenchmarkArguments(
                task=root / "task.txt", workdir=view, output=root / "out", task_id="cancel", trial_id="cancel"
            )
            binding = plugin.resolve_execution(view, args)
            plugin._remember_execution(
                binding, args, {"loop": asyncio.get_running_loop(), "invocation_id": "fixture", "abort": parent}
            )
            config = BenchConfig(task_id="cancel", trial_id="cancel", budget_seconds=5, turn_timeout_seconds=5)
            budget = create_budget(root / "budget", config, "work", time.time())
            admission = FileArtifacts().make_admission(
                budget, deadline=time.time() + 5, workdir=view, adapter_id="fixture", stop_requested=parent.is_set
            )
            context = ClarificationContext(
                None, Snapshot(str(view), "fixture", (Root(str(view)),)), "fixture", root / "session", admission
            )
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(plugin.investigate(context, "Read only.", stage="optional"), timeout=0.03)
            self.assertEqual(len(captured), 1)
            self.assertTrue(captured[0].abort.is_set())
            self.assertFalse(parent.is_set(), "optional cancellation must not stop the invocation")
            parent.set()
            self.assertTrue(captured[0].abort.is_set())
            self.assertEqual(budget.snapshot()["role_calls"], 1)
            self.assertEqual(budget.snapshot()["calls"][0]["detail"], "cancelled")

    async def test_scoped_abort_observes_parent_without_setting_or_clearing_it(self):
        import threading

        from jiuwenswarm.lh.ports import ScopedAbort

        parent = threading.Event()
        a = ScopedAbort(parent)
        b = ScopedAbort(parent)
        a.set()
        self.assertTrue(a.is_set())
        self.assertFalse(b.is_set())
        self.assertFalse(parent.is_set())
        parent.set()
        self.assertTrue(b.is_set())
        a.clear()
        self.assertTrue(a.is_set())
        self.assertTrue(parent.is_set())


class ResumeCandidateHistory(unittest.IsolatedAsyncioTestCase):
    run_fixture = CoreRegression.run_fixture

    async def test_good_p0_receipt_survives_explicit_resume_without_worker_replay(self):
        root, repo, calls, _, selection = await self.run_fixture(BAD, [(GOOD, "completed"), (BAD, "timeout")])
        runtime = WorkRuntime(repo, worktree_root=root / "worktrees")
        state = runtime.read_work(selection["work_id"])
        frozen = state.advisory[FixturePlugin(calls).spec.input_key]
        args = BenchmarkArguments(
            task=root / "instruction.txt",
            workdir=repo,
            output=root / "output",
            task_id="fixture",
            trial_id="case",
            budget_seconds=120,
            turn_timeout_seconds=120,
            budget_started_at_unix=frozen["budget_started_at_unix"],
            max_turns=2,
            control_profile="bounded",
            verify_command=VERIFY,
            worktree_root=root / "worktrees",
            work_id=state.work_id,
            expected_revision=state.revision,
        )
        controller = LongHorizonBenchmark(args, plugin=FixturePlugin(calls))
        outcome = await controller.run()
        self.assertFalse(outcome.internal_failure)
        self.assertTrue(outcome.candidate_available)
        self.assertEqual(len(calls["calls"]), 2)
        resumed = json.loads((root / "output/selection.json").read_text())
        self.assertEqual(resumed["public_candidate_gate"]["selected_stage"], "p0")
        self.assertEqual((Path(resumed["export"]["snapshot"]) / "module.py").read_text(), GOOD)

    async def test_changed_public_receipt_fails_closed(self):
        root, repo, _, _, selection = await self.run_fixture(BAD, [(GOOD, "completed"), (BAD, "timeout")])
        runtime = WorkRuntime(repo, worktree_root=root / "worktrees")
        state = runtime.read_work(selection["work_id"])
        gate = PublicCandidateGate(root / "restore-before.json")
        gate.restore(state, repo)
        self.assertIsNotNone(gate.selected)
        gate.selected[1].artifact.write_text("{}")
        with self.assertRaisesRegex(RuntimeError, "changed|evidence"):
            gate.export(runtime, root / "bad-export")
        restored = PublicCandidateGate(root / "restore-after.json")
        with self.assertRaisesRegex(RuntimeError, "changed"):
            restored.restore(state, repo)


class ControlProfileCompatibility(unittest.TestCase):
    def test_bounded_requires_supplied_check_and_suite(self):
        values = dict(task=Path("task"), workdir=Path("repo"), output=Path("out"), task_id="fixture", trial_id="one")
        self.assertEqual(BenchmarkArguments(**values).control_profile, "legacy")
        from pact_core.runtime import RuntimeContractError

        for extra in ({}, {"verify_command": VERIFY, "workflow": "benchmark"}):
            with self.assertRaises(RuntimeContractError):
                BenchmarkArguments(**values, control_profile="bounded", **extra)
