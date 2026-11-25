from __future__ import annotations

import ast
import asyncio
import json
import shlex
import sys
import threading
from dataclasses import replace
from importlib.util import resolve_name
from pathlib import Path

import pytest
from jiuwenswarm.pact_core.review_decision import parse_review_decision
from jiuwenswarm.pact_core.schemas import AdapterProbe, TurnResult

from jiuwenswarm.lh import BenchmarkArguments, Capabilities, ExecutionBinding, LongHorizonBenchmark
from jiuwenswarm.lh.plugins.generic import GenericPlugin


class PortableAgent:
    budget_failure = None
    configuration_failure = None

    def __init__(self, role, calls, **context):
        self.id = "portable-" + role
        self.role, self.calls, self.context = role, calls, context

    def probe(self):
        return AdapterProbe(adapter_id=self.id, available=True, version="scripted-v1")

    def run_turn(self, request):
        self.calls.append((self.role, request.prompt, request.workdir))
        capture = None
        if request.prompt.startswith("Read the frozen"):
            assert not (request.workdir / "greeting.txt").exists()
            text = "The greeting file belongs at the repository root. Keep unknown requirements open."
        elif self.role == "worker":
            value = "draft\n" if "first implementation" in request.prompt else "hello\n"
            (request.workdir / "greeting.txt").write_text(value)
            text = "Implemented\nPACT_REVIEW_READY\n"
        elif request.review_mode == "acceptance":
            text = json.dumps({"verdict": "complete", "reason": "Supplied checks and original task verified"})
            capture = parse_review_decision(text, existed=True)
        else:
            text = "Change draft to hello."
        options = {"review_decision": capture} if capture is not None else {}
        return TurnResult(
            status="completed", final_text=text, output_source="portable-scripted", exit_code=0, **options
        )

    async def cancel_current_turn(self):
        self.context["abort"].set()


def trial(tmp_path, git_repo_factory, *, max_role_calls=20):
    workspace = git_repo_factory(tmp_path / "repo")
    task = tmp_path / "task.md"
    task.write_text("Create greeting.txt containing hello. Preserve existing behavior.\n")
    verify = shlex.join(
        [sys.executable, "-c", "from pathlib import Path; assert Path('greeting.txt').read_text().strip() == 'hello'"]
    )
    return BenchmarkArguments(
        workflow="benchmark",
        task=task,
        workdir=workspace,
        output=tmp_path / "output",
        task_id="portable-task",
        trial_id="portable-trial",
        budget_seconds=120,
        turn_timeout_seconds=20,
        max_turns=2,
        max_role_calls=max_role_calls,
        verify_command=verify,
        worktree_root=tmp_path / "worktrees",
        model=None,
    )


def plugin(calls, *, fingerprint="fixture-v1", capabilities=None):
    return GenericPlugin(
        name="third-agent",
        capabilities=capabilities or Capabilities(True, True, True, True),
        resolve=lambda workspace, args: ExecutionBinding(fingerprint, {"runtime": "portable-test"}),
        adapter_factory=lambda role, **context: PortableAgent(role, calls, **context),
    )


@pytest.mark.asyncio
async def test_third_agent_runs_same_controller_without_native_packages(tmp_path, git_repo_factory):
    args, calls = trial(tmp_path, git_repo_factory), []
    outcome = await LongHorizonBenchmark(args, plugin=plugin(calls)).run()
    payload = json.loads(outcome.selection.read_text())
    assert not outcome.internal_failure, payload["internal_failure"]
    assert payload["internal_accepted"] and payload["delivered"]
    assert payload["external_grade"] == {"status": "not_run", "reward": None}
    assert payload["schema"] == "third-agent-pact/benchmark-result/v1"
    assert [row["purpose"] for row in payload["budget"]["calls"]] == [
        "implementation",
        "clarification:single-readonly",
        "diagnostic",
        "implementation",
        "acceptance",
    ]
    assert (args.workdir / "greeting.txt").read_text() == "hello\n"


@pytest.mark.parametrize("damage", ["snapshot", "configuration", "revision"])
@pytest.mark.asyncio
async def test_resume_rejects_changed_source_or_binding_without_replaying_p0(tmp_path, git_repo_factory, damage):
    args, calls = trial(tmp_path, git_repo_factory, max_role_calls=2), []
    outcome = await LongHorizonBenchmark(args, plugin=plugin(calls)).run()
    first = json.loads(outcome.selection.read_text())
    assert first["long_horizon"]["phase"] == "p0_saved"
    assert len(calls) == 1
    fingerprint = "fixture-v1"
    revision = first["revision"]
    if damage == "snapshot":
        root = args.output / first["long_horizon"]["stages"]["s0"]["snapshot"] / "view"
        (root / "src/README.md").write_text("changed frozen source")
    elif damage == "configuration":
        fingerprint = "fixture-v2"
    else:
        revision -= 1
    resumed = await LongHorizonBenchmark(
        replace(args, work_id=outcome.work_id, expected_revision=revision),
        plugin=plugin(calls, fingerprint=fingerprint),
    ).run()
    second = json.loads(resumed.selection.read_text())
    assert resumed.internal_failure
    assert len(calls) == 1
    assert not second["internal_accepted"]
    assert not (args.workdir / "greeting.txt").exists()


@pytest.mark.asyncio
async def test_missing_agent_capabilities_fail_before_execution(tmp_path, git_repo_factory):
    args, calls = trial(tmp_path, git_repo_factory), []
    with pytest.raises(Exception, match="plugin must enforce"):
        await LongHorizonBenchmark(args, plugin=plugin(calls, capabilities=Capabilities())).run()
    assert not calls
    assert not (args.workdir / ".pact").exists()


def test_shared_core_has_no_native_agent_imports():
    root = Path(__file__).parents[2] / "jiuwenswarm/lh"
    shared = [
        root / name
        for name in (
            "controller.py",
            "ports.py",
            "evidence.py",
            "file_artifacts.py",
            "progress.py",
            "plugins/generic.py",
        )
    ]
    core = root.parent / "pact_core"
    for path in [*shared, *core.rglob("*.py")]:
        allowed = {"pact_core"} if core in path.parents else {"pact_core", "lh"}
        package = ".".join(path.parent.relative_to(root.parents[1]).parts)
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = resolve_name("." * node.level + (node.module or ""), package)
                modules = [f"{base}.{alias.name}" for alias in node.names]
            else:
                continue
            for module in modules:
                family, _, submodule = module.partition(".")
                assert family not in {"chrys", "pact_core"}, (path, module)
                if family == "jiuwenswarm":
                    assert submodule.split(".", 1)[0] in allowed, (path, module)


@pytest.mark.asyncio
async def test_cancel_stops_active_clarifier_and_preserves_admitted_p0(tmp_path, git_repo_factory):
    args, calls = trial(tmp_path, git_repo_factory), []
    started = threading.Event()

    class BlockingAgent(PortableAgent):
        def run_turn(self, request):
            if request.prompt.startswith("Read the frozen"):
                self.calls.append((self.role, request.prompt, request.workdir))
                started.set()
                assert self.context["abort"].wait(15), "controller did not cancel the active clarification"
                return TurnResult(status="cancelled", final_text="", output_source="portable-scripted", exit_code=None)
            return super().run_turn(request)

    runtime = GenericPlugin(
        name="third-agent",
        capabilities=Capabilities(True, True, True, True),
        resolve=lambda workspace, args: ExecutionBinding("fixture-v1", {"runtime": "portable-test"}),
        adapter_factory=lambda role, **context: BlockingAgent(role, calls, **context),
    )
    controller = LongHorizonBenchmark(args, plugin=runtime)
    pending = asyncio.create_task(controller.run())
    assert await asyncio.to_thread(started.wait, 15)
    await controller.cancel()
    outcome = await pending
    payload = json.loads(outcome.selection.read_text())
    assert not outcome.internal_failure, payload["internal_failure"]
    assert payload["stop_reason"] == "invocation_stopped"
    assert payload["long_horizon"]["phase"] == "p0_saved"
    assert [row["status"] for row in payload["budget"]["calls"]] == ["completed", "error"]
    assert payload["budget"]["calls"][1]["detail"] == "invocation_stopped"
    assert not (args.workdir / "greeting.txt").exists()


@pytest.mark.asyncio
async def test_native_worker_cancellation_does_not_corrupt_core_ledger(tmp_path, git_repo_factory):
    args, calls = trial(tmp_path, git_repo_factory), []
    started = threading.Event()

    class BlockingWorker(PortableAgent):
        def run_turn(self, request):
            if self.role == "worker":
                started.set()
                assert self.context["abort"].wait(15)
                (request.workdir / "partial.txt").write_text("partial work")
                return TurnResult(status="cancelled", final_text="", output_source="portable-scripted", exit_code=None)
            return super().run_turn(request)

    runtime = GenericPlugin(
        name="third-agent",
        capabilities=Capabilities(True, True, True, True),
        resolve=lambda workspace, args: ExecutionBinding("fixture-v1", {"runtime": "portable-test"}),
        adapter_factory=lambda role, **context: BlockingWorker(role, calls, **context),
    )
    controller = LongHorizonBenchmark(args, plugin=runtime)
    pending = asyncio.create_task(controller.run())
    assert await asyncio.to_thread(started.wait, 15)
    await controller.cancel()
    outcome = await pending
    payload = json.loads(outcome.selection.read_text())
    assert payload["export_status"] == "exported", payload["export_error"]
    assert payload["budget"]["calls"][0]["status"] == "output_missing"
    assert (Path(payload["export"]["snapshot"]) / "partial.txt").read_text() == "partial work"
    assert payload["long_horizon"]["phase"] == "p0_saved"
    assert not payload["internal_accepted"]
    assert not (args.workdir / "partial.txt").exists()
