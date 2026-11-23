# Copyright (c) 2026 Chrys. All rights reserved.

"""Recover a P1 Worker transport failure before the public fallback is published."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from pact_core.runtime import RuntimeContractError
from pact_core.schemas import TurnResult
from test_suite_flow import SuiteAgent, trial

from jiuwenswarm.lh import Capabilities, ExecutionBinding, LongHorizonBenchmark
from jiuwenswarm.lh.cli import arguments_from_namespace, build_parser
from jiuwenswarm.lh.plugins.generic import GenericPlugin
from jiuwenswarm.lh.transport_failure import transport_failure_evidence

READ_ERROR = "[181001] model call failed, reason: openAI API async stream error: ReadError"
NON_TRANSPORT = "[181001] model call failed, reason: authentication failed"


def write_native_receipt(directory: Path, message: str = READ_ERROR) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    terminal = {
        "schema_version": "0.1",
        "type": "result",
        "request_id": "request-1",
        "session_id": "failed-native-session",
        "status": "failed",
        "exit_code": 1,
        "error": {"code": "RUNTIME_ERROR", "message": message},
    }
    metadata = {
        "stage": "benchmark-worker",
        "status": "failed",
        "failure_code": "NATIVE_FAILED",
        "actual_exit_code": 1,
        "timed_out": False,
        "signal": None,
    }
    (directory / "terminal-result.json").write_text(json.dumps(terminal))
    (directory / "metadata.json").write_text(json.dumps(metadata))


class TransportFailingSuiteAgent(SuiteAgent):
    """Scripted suite roles whose P1 Worker reports a machine or provider failure."""

    def __init__(
        self,
        role,
        calls,
        *,
        backend: str,
        failures: int,
        failure_kind: str,
        output_dir: Path,
        **kwargs,
    ):
        super().__init__(role, calls, **kwargs)
        self.backend = backend
        self.failures = failures
        self.failure_kind = failure_kind
        self.output_dir = Path(output_dir)
        self.sequence = 0
        self.p1_attempts = 0

    def run_turn(self, request):
        if self.role != "worker":
            return super().run_turn(request)
        self.sequence += 1
        if request.prompt.startswith("[SUITE_P1_REPAIR]"):
            self.p1_attempts += 1
            if self.p1_attempts <= self.failures:
                self.calls.append(("[SUITE_P1_REPAIR]", request.workdir, request.prompt))
                # The failed turn leaves a real partial candidate for recovery.
                (request.workdir / "recovery-marker.txt").write_text("partial candidate retained\n")
                if self.backend == "jiuwen":
                    write_native_receipt(
                        self.output_dir / "worker" / f"{self.sequence:04d}",
                        READ_ERROR if self.failure_kind == "transport" else NON_TRANSPORT,
                    )
                    return TurnResult(
                        status="spawn_failed",
                        final_text="",
                        output_source="jiuwenswarm_native",
                        exit_code=None,
                        stderr_tail="WorkerExecutionError:NATIVE_FAILED",
                    )
                return TurnResult(
                    status="spawn_failed",
                    final_text="",
                    output_source="chrys-acp",
                    exit_code=None,
                    stderr_tail=(
                        "ReadError: [Errno 104] Connection reset by peer"
                        if self.failure_kind == "transport"
                        else "WorkerExecutionError:MODEL_ERROR"
                    ),
                )
            assert (request.workdir / "recovery-marker.txt").read_text() == "partial candidate retained\n"
        return super().run_turn(request)


def transport_plugin(calls, *, backend: str, failures: int, failure_kind: str = "transport"):
    def factory(*, role, args, **kwargs):
        return TransportFailingSuiteAgent(
            role,
            calls,
            backend=backend,
            failures=failures,
            failure_kind=failure_kind,
            output_dir=Path(args.output) / "native-roles" / "fixture",
            expect_memory=True,
        )

    value = GenericPlugin(
        name="transport-recovery-fixture",
        capabilities=Capabilities(True, True, True, True),
        resolve=lambda *_: ExecutionBinding("transport-recovery-fixture-v1", {"runtime": "scripted"}),
        adapter_factory=factory,
    )

    async def memory(context, task):
        return "UNTRUSTED-MEMORY: inspect the current candidate before continuing."

    value.set_memory_provider(memory, identity="transport-recovery-corpus-v1")
    return value


def bounded_trial(tmp_path, git_repo_factory, **changes):
    values = {
        "control_profile": "bounded",
        "budget_seconds": 600,
        "turn_timeout_seconds": 120,
        "max_role_calls": 60,
        "max_turns": 6,
        **changes,
    }
    return replace(trial(tmp_path, git_repo_factory), **values)


def labels(calls):
    return [row[0] for row in calls]


@pytest.mark.parametrize("backend", ["jiuwen"])
@pytest.mark.asyncio
async def test_recoverable_transport_failure_spawns_recovery_before_first_review(
    tmp_path, git_repo_factory, backend
):
    args, calls = bounded_trial(tmp_path, git_repo_factory), []
    outcome = await LongHorizonBenchmark(args, plugin=transport_plugin(calls, backend=backend, failures=1)).run()
    report = json.loads(outcome.selection.read_text())
    assert not outcome.internal_failure, report["internal_failure"]
    assert report["internal_accepted"] and report["delivered"], report
    observed = labels(calls)
    repair_calls = [index for index, label in enumerate(observed) if label == "[SUITE_P1_REPAIR]"]
    assert len(repair_calls) == 2, observed
    assert max(repair_calls) < observed.index("reviewer"), observed
    recovery = report["long_horizon"]["suite"]["p1_transport_recovery"]
    assert recovery["incomplete"] is False
    assert recovery["recovery_attempts"] == 1
    assert recovery["outcome"] == "recovered"
    assert report["stop_reason"] != "repair_spawn_failed_publicly_checked_fallback"
    assert report["long_horizon"]["phase"] == "first_review_done"
    assert report["long_horizon"]["first_review"] is not None


@pytest.mark.parametrize("backend", ["jiuwen"])
@pytest.mark.asyncio
async def test_exhausted_transport_recovery_marks_public_fallback_incomplete(
    tmp_path, git_repo_factory, backend
):
    args = bounded_trial(tmp_path, git_repo_factory, transport_recovery_attempts=1)
    calls = []
    outcome = await LongHorizonBenchmark(
        args, plugin=transport_plugin(calls, backend=backend, failures=2)
    ).run()
    report = json.loads(outcome.selection.read_text())
    assert not outcome.internal_failure, report["internal_failure"]
    observed = labels(calls)
    assert observed.count("[SUITE_P1_REPAIR]") == 2, observed
    assert report["stop_reason"] == "repair_spawn_failed_publicly_checked_fallback"
    assert report["long_horizon"]["phase"] == "p1_saved"
    assert report["long_horizon"]["first_review"] is None
    assert not report["internal_accepted"] and not report["delivered"]
    recovery = report["long_horizon"]["suite"]["p1_transport_recovery"]
    assert recovery["incomplete"] is True
    assert recovery["recovery_attempts"] == 1
    assert recovery["outcome"] == "recovery_exhausted"
    receipt = json.loads((args.output / recovery["path"]).read_text())
    assert receipt["incomplete"] is True and receipt["recovery_attempts"] == 1


@pytest.mark.parametrize("backend", ["jiuwen"])
@pytest.mark.asyncio
async def test_transport_recovery_requires_a_reasonable_remaining_turn(
    tmp_path, git_repo_factory, backend, monkeypatch
):
    # Force the configured minimum above the remaining trial time without
    # changing the official budget or deadline.
    monkeypatch.setattr("jiuwenswarm.lh.suite.MIN_RECOVERY_TURN_SECONDS", 10_000.0)
    args = bounded_trial(
        tmp_path,
        git_repo_factory,
        transport_recovery_attempts=3,
        turn_timeout_seconds=5_000,
    )
    calls = []
    outcome = await LongHorizonBenchmark(
        args, plugin=transport_plugin(calls, backend=backend, failures=1)
    ).run()
    report = json.loads(outcome.selection.read_text())
    assert labels(calls).count("[SUITE_P1_REPAIR]") == 1
    assert report["stop_reason"] == "repair_spawn_failed_publicly_checked_fallback"
    recovery = report["long_horizon"]["suite"]["p1_transport_recovery"]
    assert recovery["incomplete"] is True
    assert recovery["recovery_attempts"] == 0
    assert recovery["outcome"] == "budget_insufficient"


@pytest.mark.asyncio
async def test_non_transport_worker_failure_keeps_the_existing_fallback(tmp_path, git_repo_factory):
    args = bounded_trial(tmp_path, git_repo_factory, transport_recovery_attempts=3)
    calls = []
    outcome = await LongHorizonBenchmark(
        args, plugin=transport_plugin(calls, backend="chrys", failures=1, failure_kind="model")
    ).run()
    report = json.loads(outcome.selection.read_text())
    assert labels(calls).count("[SUITE_P1_REPAIR]") == 1
    assert report["stop_reason"] == "repair_spawn_failed_publicly_checked_fallback"
    assert "p1_transport_recovery" not in report["long_horizon"]["suite"]


@pytest.mark.parametrize(
    "diagnostic",
    [
        "ReadError: connection closed",
        "APIConnectionError: Connection reset by peer",
        "APITimeoutError: request timed out",
        "HTTP/1.1 503 Service Unavailable",
        "provider returned 502 Bad Gateway",
        "httpx.ConnectError: [Errno 111] Connection refused",
    ],
)
def test_common_transport_diagnostics_are_recoverable(diagnostic):
    result = SimpleNamespace(status="spawn_failed", stderr_tail=diagnostic)
    evidence = transport_failure_evidence(result)
    assert evidence is not None
    assert evidence["kind"] == "adapter_diagnostic"


def test_model_and_task_failures_are_not_transport():
    result = SimpleNamespace(status="spawn_failed", stderr_tail="WorkerExecutionError:MODEL_ERROR")
    assert transport_failure_evidence(result) is None
    result = SimpleNamespace(status="completed", stderr_tail="ReadError: stale diagnostic")
    assert transport_failure_evidence(result) is None


def test_transport_recovery_attempts_are_configurable_and_bounded():
    common = ["--task", "t.md", "--workdir", ".", "--output", "/tmp/output", "--task-id", "a", "--trial-id", "b"]
    parsed = build_parser().parse_args([*common, "--transport-recovery-attempts", "2"])
    assert arguments_from_namespace(parsed).transport_recovery_attempts == 2
    with pytest.raises(RuntimeContractError, match="transport_recovery_attempts"):
        arguments_from_namespace(build_parser().parse_args([*common, "--transport-recovery-attempts", "11"]))
