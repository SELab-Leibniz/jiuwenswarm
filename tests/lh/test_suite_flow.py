# Copyright (c) 2026 Chrys. All rights reserved.

"""Exercise suite phase order, evidence isolation, recovery and real Core gates."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from pact_core.review_decision import parse_review_decision
from pact_core.runtime import WorkRuntime
from pact_core.schemas import AdapterProbe, TurnResult

from jiuwenswarm.lh import BenchmarkArguments, Capabilities, ExecutionBinding, LongHorizonBenchmark
from jiuwenswarm.lh.cli import arguments_from_namespace, build_parser
from jiuwenswarm.lh.plugins.generic import GenericPlugin
from jiuwenswarm.lh.suite import _suite_shell_command, register_suite_checks

TASK = "Create greeting.txt containing hello. Preserve existing behavior.\n"
CONTRACT = {
    "schema": "pact-runtime/goal-contract/v1",
    "goal": TASK,
    "acceptance_criteria": [{"id": "greeting", "text": "The greeting is hello."}],
    "non_goals": [],
}
PLAN = {
    "schema": "pact-runtime/initial-plan/v1",
    "constraints": ["Preserve existing behavior"],
    "missions": [
        {
            "id": "m1",
            "objective": "Write greeting.txt",
            "target_ac_ids": ["greeting"],
            "dependencies": [],
            "verification_intent": "Assert the greeting file equals hello",
        }
    ],
}


def verification():
    return shlex.join(
        [sys.executable, "-c", "from pathlib import Path; assert Path('greeting.txt').read_text().strip() == 'hello'"]
    )


class SuiteAgent:
    budget_failure = None
    configuration_failure = None

    def __init__(
        self,
        role,
        calls,
        *,
        fail_repair=False,
        block_localization=None,
        localization_started=None,
        mutate=False,
        mutate_target=None,
        p1_ready=True,
        fail_optional=False,
        expect_memory=True,
        later_ready=False,
    ):
        self.id = "scripted-suite-" + role
        self.role, self.calls = role, calls
        self.fail_repair, self.block_localization, self.mutate = fail_repair, block_localization, mutate
        self.mutate_target = mutate_target
        self.p1_ready = p1_ready
        self.localization_started = localization_started
        self.fail_optional = fail_optional
        self.expect_memory = expect_memory
        self.later_ready = later_ready

    def probe(self):
        return AdapterProbe(adapter_id=self.id, available=True, version="scripted-suite-v1")

    async def cancel_current_turn(self):
        if self.block_localization:
            self.block_localization.set()

    def run_turn(self, request):
        prompt = request.prompt
        tag = re.search(r"\[SUITE_[^]]+\]", prompt)
        label = tag.group() if tag else self.role
        self.calls.append((label, request.workdir, prompt))
        if self.fail_optional and label in ("[SUITE_LOCALIZATION]", "[SUITE_MANAGER]"):
            return TurnResult(status="output_missing", final_text="", output_source="scripted", exit_code=1)
        capture = None
        if prompt.startswith("[SUITE_LOCALIZATION]"):
            assert not (request.workdir / "greeting.txt").exists()
            if self.localization_started:
                self.localization_started.set()
            if self.block_localization:
                self.block_localization.wait(10)
            if self.mutate:
                target = self.mutate_target() if self.mutate_target is not None else request.workdir
                if target is not None:
                    (target / "src/README.md").write_text("mutated")
            history = json.JSONDecoder().raw_decode(
                prompt.split("Previous graph queries and results (untrusted):\n", 1)[1]
            )[0]
            if (request.workdir / "app/storage.py").exists():
                expected = [
                    ("commit_record", "app/storage.py"),
                    ("flush_batch", "app/queue.py"),
                    ("test_empty_batch", "tests/test_queue.py"),
                ]
                if len(history) < 3:
                    symbol, path = expected[len(history)]
                    if history:
                        assert path in json.dumps(history[-1]), "next hop was not present in actual graph results"
                    text = json.dumps(
                        {
                            "queries": [
                                {"name": "find_child_unit", "arguments": {"definition_name": symbol, "file_path": path}}
                            ]
                        }
                    )
                else:
                    text = json.dumps(
                        {
                            "locations": [
                                {"file_path": path, "symbol": symbol, "reason": "Inspected graph-linked definition"}
                                for symbol, path in expected
                            ]
                        }
                    )
            elif not history:
                text = json.dumps({"queries": [{"name": "find_file", "arguments": {"file_name": "src/README.md"}}]})
            else:
                assert "src/README.md" in json.dumps(history)
                text = json.dumps(
                    {"locations": [{"file_path": "src/README.md", "symbol": "", "reason": "Repository documentation"}]}
                )
        elif prompt.startswith("[SUITE_INVESTIGATION"):
            assert not (request.workdir / "greeting.txt").exists()
            text = "src/README.md:1 inspected. The original requirement names greeting.txt."
        elif prompt.startswith("[SUITE_PROPOSAL"):
            text = json.dumps(
                {
                    "guidance_points": [
                        {
                            "statement": "Write the greeting at the repository root.",
                            "confidence": 0.9,
                            "evidence": ["src/README.md:1", "original task"],
                            "decision_impact": "The path determines observable output.",
                        }
                    ]
                }
            )
        elif prompt.startswith("[SUITE_SELECTOR]"):
            text = json.dumps(
                {
                    "reviews": [
                        {
                            "id": f"p{i}-g1",
                            "decision": "select" if i == 1 else "reject",
                            "rationale": "Select one necessary mapping; others repeat it.",
                        }
                        for i in (1, 2, 3)
                    ]
                }
            )
        elif prompt.startswith("[SUITE_GOAL_CONTRACT]"):
            assert "UNTRUSTED-MEMORY" not in prompt and "Candidate code locations" not in prompt
            assert "Semantic graph localization" not in prompt
            text = json.dumps(CONTRACT)
        elif prompt.startswith("[SUITE_INITIAL_PLAN]"):
            assert ("UNTRUSTED-MEMORY" in prompt) == self.expect_memory
            if (request.workdir / "app/storage.py").exists():
                assert all(path in prompt for path in ("app/storage.py", "app/queue.py", "tests/test_queue.py"))
            text = json.dumps(PLAN)
        elif prompt.startswith("[SUITE_PUBLIC_VERIFICATION]"):
            text = json.dumps(
                {
                    "status": "ready",
                    "checks": [
                        {
                            "id": "greeting",
                            "command": verification(),
                            "target_ac_ids": ["greeting"],
                            "rationale": "Assert the requested behavior",
                        }
                    ],
                }
            )
        elif prompt.startswith("[SUITE_MANAGER]"):
            text = "Check the full original task, greeting behavior and preserved baseline before accepting."
        elif self.role == "worker":
            if prompt.startswith("[SUITE_P1_REPAIR]"):
                assert (request.workdir / "greeting.txt").read_text() == "draft\n"
                assert ("UNTRUSTED-MEMORY" in prompt) == self.expect_memory and "Initial Plan" in prompt
                if (request.workdir / "app/storage.py").exists():
                    assert all(path in prompt for path in ("app/storage.py", "app/queue.py", "tests/test_queue.py"))
                if self.fail_repair:
                    (request.workdir / "greeting.txt").write_text("broken\n")
                    return TurnResult(status="error", final_text="Failed repair", output_source="scripted")
                value = "hello\n"
            else:
                value = "draft\n" if "first implementation" in prompt else "hello\n"
            (request.workdir / "greeting.txt").write_text(value)
            ready = self.p1_ready or (self.later_ready and not prompt.startswith("[SUITE_P1_REPAIR]"))
            text = "Implemented\nPACT_REVIEW_READY\n" if value == "hello\n" and ready else "Draft saved."
        elif prompt.startswith("[SUITE_PLANNER]"):
            assert (request.workdir / "greeting.txt").read_text() == "hello\n"
            text = json.dumps(PLAN)
        elif request.review_mode == "acceptance":
            text = json.dumps(
                {"verdict": "complete", "reason": "Actual public check and original requirement verified"}
            )
            capture = parse_review_decision(text, existed=True)
        else:
            text = (
                "Review the complete original requirement.\n<pact-check-decisions>"
                '[{"name":"suite-greeting","version":1,"action":"admit",'
                '"reason":"A concrete assertion of the requested greeting"}]</pact-check-decisions>'
            )
        kwargs = {"review_decision": capture} if capture else {}
        return TurnResult(status="completed", final_text=text, output_source="scripted", exit_code=0, **kwargs)


def trial(tmp_path, git_repo_factory, **changes):
    workspace = git_repo_factory(tmp_path / "repo")
    task = tmp_path / "task.md"
    task.write_text(TASK)
    return BenchmarkArguments(
        task,
        workspace,
        tmp_path / "output",
        "suite-task",
        "suite-trial",
        budget_seconds=120,
        max_role_calls=40,
        max_turns=4,
        turn_timeout_seconds=20,
        verify_command=verification(),
        worktree_root=tmp_path / "worktrees",
        model=None,
        **changes,
    )


def plugin(calls, **agent_options):
    value = GenericPlugin(
        name="third-agent",
        capabilities=Capabilities(True, True, True, True),
        resolve=lambda workspace, args: ExecutionBinding("fixture-suite-v1", {"runtime": "scripted-suite"}),
        adapter_factory=lambda role, **kwargs: SuiteAgent(role, calls, **agent_options),
    )

    async def memory(context, task):
        assert not (Path(context.snapshot.roots[0].view_root) / "greeting.txt").exists()
        return "UNTRUSTED-MEMORY: root output is required; verify against the current task."

    value.set_memory_provider(memory, identity="frozen-test-corpus-v1")
    return value


@pytest.mark.asyncio
async def test_shared_suite_restores_p1_planning_recall_and_localization_before_review(tmp_path, git_repo_factory):
    args, calls = trial(tmp_path, git_repo_factory), []
    outcome = await LongHorizonBenchmark(args, plugin=plugin(calls)).run()
    report = json.loads(outcome.selection.read_text())
    assert not outcome.internal_failure, report["internal_failure"]
    assert report["internal_accepted"] and report["delivered"]
    assert report["external_grade"]["status"] == "not_run"
    assert "review_isolation" not in report
    assert report["long_horizon"]["workflow"] == "suite"
    labels = [row[0] for row in calls]
    assert labels[0] == "worker"
    assert labels.index("[SUITE_P1_REPAIR]") < labels.index("[SUITE_MANAGER]") < labels.index("reviewer")
    assert len([label for label in labels if label.startswith("[SUITE_INVESTIGATION")]) == 3
    assert len([label for label in labels if label.startswith("[SUITE_PROPOSAL")]) == 3
    assert (
        report["long_horizon"]["stages"]["p1"]["candidate_tree"]
        != report["long_horizon"]["stages"]["p0"]["candidate_tree"]
    )
    assert (args.workdir / "greeting.txt").read_text() == "hello\n"
    assert report["budget"]["role_calls"] == len(calls)
    assert (
        report["long_horizon"]["stages"]["handoff"]["candidate_tree"]
        == report["long_horizon"]["stages"]["p1"]["candidate_tree"]
    )


@pytest.mark.asyncio
async def test_generated_public_checks_are_reviewed_and_run_on_real_candidate(tmp_path, git_repo_factory):
    args, calls = replace(trial(tmp_path, git_repo_factory), verify_command=None), []
    outcome = await LongHorizonBenchmark(args, plugin=plugin(calls)).run()
    report = json.loads(outcome.selection.read_text())
    assert not outcome.internal_failure, report["internal_failure"]
    assert report["internal_accepted"] and report["delivered"], report
    assert "[SUITE_PUBLIC_VERIFICATION]" in [row[0] for row in calls]
    assert report["public_verification"]["status"] == "passed"


@pytest.mark.skipif(os.name != "posix", reason="POSIX shell commands")
@pytest.mark.parametrize("command", [
    "printf '%s' 'set -euo pipefail'",
    "# set -euo pipefail\nprintf ok",
    "cat <<'TEXT'\nset -euo pipefail\nTEXT",
    "bash -c 'set -euo pipefail; true'",
    "set -eu\nprintf ok",
    "#!/bin/sh\nset -euo pipefail",
    "set -- -o pipefail\nprintf '%s' \"$#\"",
])
def test_suite_keeps_shell_commands_without_a_pipefail_prologue(command):
    assert _suite_shell_command(command) == command


@pytest.mark.skipif(os.name != "posix", reason="POSIX shell commands")
@pytest.mark.parametrize(("header", "argv"), [
    ("#!/bin/bash", ["/bin/bash"]),
    ("#!/usr/bin/env bash", ["/bin/bash"]),
    ("#!/usr/bin/bash -eu", ["/usr/bin/bash", "-eu"]),
])
def test_suite_selects_explicit_bash_shebang(header, argv):
    command = header + "\nset -euo pipefail\nfalse | true"
    assert shlex.split(_suite_shell_command(command)) == ["exec", *argv, "-c", command]


@pytest.mark.skipif(os.name != "posix", reason="POSIX shell commands")
@pytest.mark.parametrize("edit", [
    {"disposition": "suspended"}, {"command": "printf 'reviewer revision'"},
    {"reason": "registered separately"}, {"files": {"check.sh": "bound digest"}},
    {"source": "supplied"}, {"requirement": "revised requirement"},
])
def test_resume_and_p1_retry_preserve_existing_method_edits(edit):
    script = "set -euo pipefail\nfalse | true"
    bound = {"status": "ready", "checks": [{"id": "test", "command": script}]}
    state = SimpleNamespace(
        advisory={"long_horizon_suite": {"public_verification": bound}}, contract={"goal": TASK},
        checks=[{
            "name": "suite-test", "command": script, "requirement": TASK, "files": {}, "source": "derived",
            "reason": "S0-generated public methods; pending independent Reviewer admission; original checks retained",
            "disposition": "pending", **edit,
        }],
    )
    for resume in (False, True):
        assert register_suite_checks(object(), state, upgrade_registered=resume) is state


@pytest.mark.skipif(os.name != "posix", reason="POSIX shell commands")
@pytest.mark.asyncio
async def test_generated_pipefail_check_preserves_the_reviewed_script(tmp_path, git_repo_factory, monkeypatch):
    script = (
        "# Exercise the registered script verbatim.\n\nset -eu -o pipefail\n"
        "read -r value <<'BODY'\n$literal 'quoted' `literal`\nBODY\n"
        "expected=$(cat <<'BODY'\n$literal 'quoted' `literal`\nBODY\n)\n"
        'test "$value" = "$expected"\ntest "$(cat greeting.txt)" = hello\n'
    )
    args, calls = replace(trial(tmp_path, git_repo_factory), verify_command=None), []
    monkeypatch.setattr(sys.modules[__name__], "verification", lambda: script)
    outcome = await LongHorizonBenchmark(args, plugin=plugin(calls)).run()
    report = json.loads(outcome.selection.read_text())
    assert not outcome.internal_failure and report["delivered"], report
    state = WorkRuntime(args.workdir, worktree_root=args.worktree_root).read_work(report["work_id"])
    assert state.advisory["long_horizon_suite"]["public_verification"]["checks"][0]["command"] == script
    assert shlex.split(state.checks[0]["command"]) == ["exec", "/bin/bash", "-c", script]
    assert state.verification.check_results[0]["command"] == state.checks[0]["command"]
    assert state.verification.check_results[0]["status"] == "passed"


@pytest.mark.skipif(os.name != "posix", reason="POSIX shell commands")
@pytest.mark.asyncio
async def test_resume_versions_registered_pipefail_script_and_clears_old_verification(tmp_path, git_repo_factory, monkeypatch):
    script = "set -euo pipefail\nprintf 'before pipeline\\n'\nfalse | true\nprintf 'unexpected success\\n'\n"
    args, calls = replace(trial(tmp_path, git_repo_factory), max_turns=2, verify_command=None), []
    monkeypatch.setattr(sys.modules[__name__], "verification", lambda: script)
    with monkeypatch.context() as legacy:
        legacy.setattr("jiuwenswarm.lh.suite._suite_shell_command", lambda command: command)
        first = await LongHorizonBenchmark(args, plugin=plugin(calls, p1_ready=False)).run()
    report = json.loads(first.selection.read_text())
    assert not first.internal_failure and not report["delivered"], report
    runtime = WorkRuntime(args.workdir, worktree_root=args.worktree_root)
    old = runtime.read_work(report["work_id"])
    previous = old.checks[0]
    assert previous["command"] == script and previous["version"] == 1
    assert old.verification and old.verification.check_results[0]["command"] == script
    before = old.verification.artifact.read_bytes()
    paused = runtime.pause(old.work_id, expected_revision=old.revision)
    resumed = await LongHorizonBenchmark(
        replace(args, work_id=old.work_id, expected_revision=paused.revision), plugin=plugin(calls)
    ).run()
    assert not resumed.internal_failure, json.loads(resumed.selection.read_text())
    state = runtime.read_work(old.work_id)
    upgraded = state.checks[0]
    assert upgraded["version"] == 2 and upgraded["fingerprint"] != previous["fingerprint"]
    assert shlex.split(upgraded["command"]) == ["exec", "/bin/bash", "-c", script]
    assert old.verification.artifact.read_bytes() == before
    assert state.advisory["long_horizon_suite"]["public_verification"]["checks"][0]["command"] == script
    assert state.acceptance is None
    if state.verification is None:
        state = runtime.verify_candidate(state.work_id, expected_revision=state.revision)
    result = state.verification.check_results[0]
    assert result["command"] == upgraded["command"] and result["fingerprint"] == upgraded["fingerprint"]
    assert (result["status"], result["exit_code"], result["stdout_tail"]) == ("failed", 1, "before pipeline\n")
    assert state.verification.artifact != old.verification.artifact
    assert register_suite_checks(runtime, state, upgrade_registered=True).revision == state.revision


@pytest.mark.asyncio
async def test_suite_budget_resume_never_replays_p0_and_preserves_deadline(tmp_path, git_repo_factory):
    # Graph search now has a query round and a selection round, both admitted.
    args, calls = replace(trial(tmp_path, git_repo_factory), max_role_calls=14), []
    first = await LongHorizonBenchmark(args, plugin=plugin(calls, p1_ready=False)).run()
    report = json.loads(first.selection.read_text())
    assert report["long_horizon"]["phase"] == "first_review_done", report
    assert not report["internal_accepted"]
    before = len(calls)
    resumed = await LongHorizonBenchmark(
        replace(args, work_id=report["work_id"], expected_revision=report["revision"]), plugin=plugin(calls)
    ).run()
    second = json.loads(resumed.selection.read_text())
    assert len(calls) == before
    assert second["budget"]["deadline"] == report["budget"]["deadline"]
    assert not second["long_horizon"]["p0_ran_this_invocation"]


@pytest.mark.asyncio
async def test_suite_binds_upstream_origin_and_rejects_changed_resume(tmp_path, git_repo_factory):
    origin = time.time() - 3.1390960216522217
    args, calls = replace(trial(tmp_path, git_repo_factory), budget_started_at_unix=origin, max_role_calls=14), []
    first = await LongHorizonBenchmark(args, plugin=plugin(calls, p1_ready=False)).run()
    report = json.loads(first.selection.read_text())
    assert not first.internal_failure, report
    assert report["budget"]["started_at"] == origin
    assert report["budget"]["deadline"] == origin + args.budget_seconds
    before = len(calls)
    resumed_args = replace(args, work_id=report["work_id"], expected_revision=report["revision"])
    resumed = await LongHorizonBenchmark(resumed_args, plugin=plugin(calls)).run()
    after = json.loads(resumed.selection.read_text())
    assert after["budget"]["deadline"] == report["budget"]["deadline"]
    assert len(calls) == before
    rejected = await LongHorizonBenchmark(
        replace(resumed_args, budget_started_at_unix=origin + 1), plugin=plugin(calls)
    ).run()
    assert rejected.internal_failure
    assert len(calls) == before


@pytest.mark.asyncio
async def test_failed_p1_exports_saved_p0_and_refuses_automatic_replay(tmp_path, git_repo_factory):
    args, calls = trial(tmp_path, git_repo_factory), []
    outcome = await LongHorizonBenchmark(args, plugin=plugin(calls, fail_repair=True)).run()
    report = json.loads(outcome.selection.read_text())
    assert not report["internal_accepted"] and not report["delivered"]
    assert report["candidate"]["tree"] == report["long_horizon"]["stages"]["p0"]["candidate_tree"]
    assert "hello" not in (args.output / report["export_patch"]).read_text()
    resumed = await LongHorizonBenchmark(
        replace(args, work_id=report["work_id"], expected_revision=report["revision"]), plugin=plugin(calls)
    ).run()
    second = json.loads(resumed.selection.read_text())
    assert resumed.internal_failure and "not_auto_resumable" in second["internal_failure"]["detail"]
    assert len([row for row in calls if row[0] == "worker"]) == 1


@pytest.mark.asyncio
async def test_cancel_during_parallel_preparation_settles_all_admitted_calls(tmp_path, git_repo_factory):
    args, calls, event, started = trial(tmp_path, git_repo_factory), [], threading.Event(), threading.Event()
    controller = LongHorizonBenchmark(
        args, plugin=plugin(calls, block_localization=event, localization_started=started)
    )
    task = asyncio.create_task(controller.run())
    assert await asyncio.to_thread(started.wait, 30), "localization never started"
    await controller.cancel()
    report = json.loads((await task).selection.read_text())
    assert not report["internal_accepted"]
    assert report["long_horizon"]["phase"] == "p0_saved"
    assert all(row["status"] != "started" for row in report["budget"]["calls"])


@pytest.mark.asyncio
async def test_suite_rejects_mutated_s0_without_starting_p1(tmp_path, git_repo_factory):
    args, calls = trial(tmp_path, git_repo_factory), []
    real_view: dict[str, Path] = {}
    value = plugin(calls, mutate=True, mutate_target=lambda: real_view.get("view"))
    original_investigate = value.investigate

    async def capture_view(context, prompt, *, stage):
        real_view["view"] = Path(context.snapshot.roots[0].view_root)
        return await original_investigate(context, prompt, stage=stage)

    value.investigate = capture_view
    outcome = await LongHorizonBenchmark(args, plugin=value).run()
    report = json.loads(outcome.selection.read_text())
    assert outcome.internal_failure
    assert "[SUITE_P1_REPAIR]" not in [row[0] for row in calls]
    assert not report["internal_accepted"]


def test_cli_suite_defaults_and_explicit_old_protocol():
    common = ["--task", "t.md", "--workdir", ".", "--output", "/tmp/output", "--task-id", "a", "--trial-id", "b"]
    suite = arguments_from_namespace(build_parser().parse_args(common))
    old = arguments_from_namespace(build_parser().parse_args(["--workflow", "benchmark", *common]))
    assert (suite.workflow, suite.budget_seconds, suite.max_turns, suite.max_role_calls) == ("suite", 172800, 64, None)
    assert (old.workflow, old.budget_seconds, old.max_turns, old.max_role_calls) == ("benchmark", 3600, 3, 32)


@pytest.mark.asyncio
async def test_suite_turn_limit_is_cumulative_on_resume(tmp_path, git_repo_factory):
    args, calls = replace(trial(tmp_path, git_repo_factory), max_turns=2), []
    first = await LongHorizonBenchmark(args, plugin=plugin(calls, p1_ready=False)).run()
    report = json.loads(first.selection.read_text())
    assert report["stop_reason"] == "max_turns"
    assert report["long_horizon"]["phase"] == "first_review_done"
    assert len([call for call in report["budget"]["calls"] if call["purpose"] == "implementation"]) == 2
    before = len(calls)
    second = await LongHorizonBenchmark(
        replace(args, work_id=report["work_id"], expected_revision=report["revision"]), plugin=plugin(calls)
    ).run()
    assert not second.internal_failure
    assert len(calls) == before


@pytest.mark.asyncio
async def test_delivered_replay_is_zero_call_and_rejects_changed_plan(tmp_path, git_repo_factory):
    args, calls = trial(tmp_path, git_repo_factory), []
    first = await LongHorizonBenchmark(args, plugin=plugin(calls)).run()
    report = json.loads(first.selection.read_text())
    resume = replace(args, work_id=report["work_id"], expected_revision=report["revision"])
    before = len(calls)
    replay = await LongHorizonBenchmark(resume, plugin=plugin(calls)).run()
    assert not replay.internal_failure and len(calls) == before
    (args.output / report["long_horizon"]["suite"]["initial_plan_path"]).write_text("{}\n")
    tampered = await LongHorizonBenchmark(resume, plugin=plugin(calls)).run()
    assert tampered.internal_failure and len(calls) == before
    assert "bound suite evidence changed" in json.loads(tampered.selection.read_text())["internal_failure"]["detail"]


@pytest.mark.asyncio
async def test_optional_memory_localization_and_manager_failures_preserve_execution(tmp_path, git_repo_factory):
    args, calls = trial(tmp_path, git_repo_factory), []
    configured = plugin(calls, fail_optional=True, expect_memory=False)

    async def broken_memory(context, task):
        raise ConnectionError("memory unavailable")

    configured.set_memory_provider(broken_memory, identity="frozen-test-corpus-v1")
    outcome = await LongHorizonBenchmark(args, plugin=configured).run()
    report = json.loads(outcome.selection.read_text())
    assert not outcome.internal_failure, report["internal_failure"]
    assert report["internal_accepted"] and report["delivered"]
    assert report["long_horizon"]["suite"]["memory"]["status"] == "degraded"
    assert report["long_horizon"]["suite"]["localization"]["status"] == "degraded"


@pytest.mark.asyncio
async def test_later_worker_gets_fresh_readonly_plan_after_review(tmp_path, git_repo_factory):
    args, calls = trial(tmp_path, git_repo_factory), []
    outcome = await LongHorizonBenchmark(args, plugin=plugin(calls, p1_ready=False, later_ready=True)).run()
    report = json.loads(outcome.selection.read_text())
    assert not outcome.internal_failure, report["internal_failure"]
    assert report["internal_accepted"] and report["delivered"]
    labels = [row[0] for row in calls]
    assert labels.index("reviewer") < labels.index("[SUITE_PLANNER]")
    later = [row for row in calls if row[0] == "worker"][-1]
    assert "Current validated implementation plan" in later[2]
    assert any(call["purpose"] == "clarification:planner" for call in report["budget"]["calls"])
    records = [json.loads(path.read_text()) for path in (args.output / "planning").glob("*.json")]
    assert any(record["status"] == "replanned" for record in records)


@pytest.mark.asyncio
async def test_planning_integrity_error_is_not_degraded(tmp_path, monkeypatch):
    import jiuwenswarm.lh.suite as suite_module
    from jiuwenswarm.lh.side_calls import SideCallIntegrityError

    view = tmp_path / "view"
    view.mkdir()
    context = SimpleNamespace(
        snapshot=SimpleNamespace(roots=(SimpleNamespace(view_root=str(view)),)),
        session_dir=tmp_path / "session",
    )
    error = SideCallIntegrityError("side call mutated its assigned repository view")
    original_task_pair_calls = []
    generated = []

    class Plugin:
        artifacts = SimpleNamespace(
            exhausted=lambda message: RuntimeError(message),
            stopped=lambda message: RuntimeError(message),
        )

        async def clarify(self, _context, _original_task):
            return SimpleNamespace(text="")

        def degraded_clarification(self, _reason):
            return SimpleNamespace(text="")

        async def recall(self, _context, _original_task):
            return ""

        async def generate_pact_input(self, _context, result, original_task, *, hints):
            generated.append((result, original_task, hints))
            raise error

    async def localization(_plugin, _context, _task, _identity, **_kwargs):
        return {"status": "available", "text": "candidate location", "artifacts": []}

    monkeypatch.setattr(suite_module, "localize_suite", localization)
    monkeypatch.setattr(
        suite_module,
        "original_task_pair",
        lambda task: original_task_pair_calls.append(task) or {},
    )

    with pytest.raises(SideCallIntegrityError) as caught:
        await suite_module._prepare_suite_bounded(Plugin(), context, TASK)

    assert caught.value is error
    assert original_task_pair_calls == []
    assert len(generated) == 1


@pytest.mark.asyncio
async def test_recoverable_public_review_saves_p0_and_resume_reviews_only_unreviewed(tmp_path, git_repo_factory):
    from jiuwenswarm.lh.public_review import PublicReviewProgress, recoverable_public_review_error

    contract = {
        "schema": "pact-runtime/goal-contract/v1",
        "goal": TASK,
        "acceptance_criteria": [
            {"id": "c1", "text": "The greeting file is created."},
            {"id": "c2", "text": "The greeting contains hello."},
        ],
        "non_goals": [],
    }
    plan = {
        "schema": "pact-runtime/initial-plan/v1",
        "constraints": ["Preserve existing behavior"],
        "missions": [
            {
                "id": "m1",
                "objective": "Write greeting.txt",
                "target_ac_ids": ["c1", "c2"],
                "dependencies": [],
                "verification_intent": "Check the greeting file",
            }
        ],
    }

    class TwoCriterionAgent(SuiteAgent):
        def run_turn(self, request):
            if request.prompt.startswith("[SUITE_GOAL_CONTRACT]"):
                self.calls.append(("[SUITE_GOAL_CONTRACT]", request.workdir, request.prompt))
                return TurnResult(
                    status="completed", final_text=json.dumps(contract), output_source="scripted", exit_code=0
                )
            if request.prompt.startswith("[SUITE_INITIAL_PLAN]"):
                self.calls.append(("[SUITE_INITIAL_PLAN]", request.workdir, request.prompt))
                return TurnResult(status="completed", final_text=json.dumps(plan), output_source="scripted", exit_code=0)
            return super().run_turn(request)

    args, calls = replace(trial(tmp_path, git_repo_factory), verify_command=None), []
    value = plugin(calls)
    value._factory = lambda role, **kwargs: TwoCriterionAgent(role, calls)
    reviewed_file = tmp_path / "reviewed-criteria.json"
    review_calls = []

    async def public_verification(context, original_task, pair):
        reviewed = set(json.loads(reviewed_file.read_text())) if reviewed_file.exists() else set()
        required = [row["id"] for row in pair["goal_contract"]["acceptance_criteria"]]
        pending = [criterion for criterion in required if criterion not in reviewed]
        review_calls.append(tuple(pending))
        if len(review_calls) == 1:
            reviewed.add(pending[0])
            reviewed_file.write_text(json.dumps(sorted(reviewed)))
            progress = PublicReviewProgress(
                started=True,
                reason="trial_budget_refused",
                completed_criteria=tuple(sorted(reviewed)),
                required_criteria=tuple(required),
                deadline_unix=context.deadline,
                total_seconds=args.budget_seconds,
                refused=True,
            )
            raise recoverable_public_review_error(value.artifacts.exhausted, progress)
        reviewed.update(pending)
        reviewed_file.write_text(json.dumps(sorted(reviewed)))
        return {
            "status": "ready",
            "reason": "Both criteria reviewed independently",
            "checks": [
                {
                    "id": "check-" + criterion,
                    "command": verification(),
                    "target_ac_ids": [criterion],
                    "rationale": "Independent public criterion review",
                }
                for criterion in required
            ],
        }

    value._public_verification = public_verification
    first = await LongHorizonBenchmark(args, plugin=value).run()
    report = json.loads(first.selection.read_text())
    assert not first.internal_failure, report.get("internal_failure")
    assert report["long_horizon"]["phase"] == "p0_saved"
    assert report["stop_reason"] == "trial_budget_refused"
    assert review_calls == [("c1", "c2")]
    recovery = report["long_horizon"]["clarification_attempts"][-1]["recoverable"]
    assert recovery["reason"] == "trial_budget_refused"
    assert recovery["completed_criteria"] == ["c1"]
    assert recovery["deadline_unix"] == report["budget"]["deadline"]
    assert "[SUITE_P1_REPAIR]" not in [row[0] for row in calls]

    second = await LongHorizonBenchmark(
        replace(args, work_id=report["work_id"], expected_revision=report["revision"]), plugin=value
    ).run()
    resumed = json.loads(second.selection.read_text())
    assert not second.internal_failure, resumed
    assert resumed["internal_accepted"] and resumed["delivered"]
    # The durable reviewed set is reused: the resume reviews only c2.
    assert review_calls == [("c1", "c2"), ("c2",)]
    assert resumed["budget"]["deadline"] == report["budget"]["deadline"]
    assert [row[0] for row in calls].count("[SUITE_P1_REPAIR]") == 1
