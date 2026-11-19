# Copyright (c) 2026 Chrys. All rights reserved.

"""Exercise suite phase order, evidence isolation, recovery and real Core gates."""

from __future__ import annotations

import asyncio
import json
import re
import shlex
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

from pact_core.review_decision import parse_review_decision
from pact_core.schemas import AdapterProbe, TurnResult

from jiuwenswarm.lh import BenchmarkArguments, Capabilities, ExecutionBinding, LongHorizonBenchmark
from jiuwenswarm.lh.cli import arguments_from_namespace, build_parser
from jiuwenswarm.lh.plugins.generic import GenericPlugin

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
        p1_ready=True,
        fail_optional=False,
        expect_memory=True,
        later_ready=False,
    ):
        self.id = "scripted-suite-" + role
        self.role, self.calls = role, calls
        self.fail_repair, self.block_localization, self.mutate = fail_repair, block_localization, mutate
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
                (request.workdir / "src/README.md").write_text("mutated")
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


async def test_shared_suite_restores_p1_planning_recall_and_localization_before_review(tmp_path, git_repo_factory):
    args, calls = trial(tmp_path, git_repo_factory), []
    outcome = await LongHorizonBenchmark(args, plugin=plugin(calls)).run()
    report = json.loads(outcome.selection.read_text())
    assert not outcome.internal_failure, report["internal_failure"]
    assert report["internal_accepted"] and report["delivered"]
    assert report["external_grade"]["status"] == "not_run"
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


async def test_generated_public_checks_are_reviewed_and_run_on_real_candidate(tmp_path, git_repo_factory):
    args, calls = replace(trial(tmp_path, git_repo_factory), verify_command=None), []
    outcome = await LongHorizonBenchmark(args, plugin=plugin(calls)).run()
    report = json.loads(outcome.selection.read_text())
    assert not outcome.internal_failure, report["internal_failure"]
    assert report["internal_accepted"] and report["delivered"], report
    assert "[SUITE_PUBLIC_VERIFICATION]" in [row[0] for row in calls]
    assert report["public_verification"]["status"] == "passed"


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


async def test_suite_rejects_mutated_s0_without_starting_p1(tmp_path, git_repo_factory):
    args, calls = trial(tmp_path, git_repo_factory), []
    outcome = await LongHorizonBenchmark(args, plugin=plugin(calls, mutate=True)).run()
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
