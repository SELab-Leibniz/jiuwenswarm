from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
from jiuwenswarm.pact_core.benchmark.budget import BenchBudget
from jiuwenswarm.pact_core.benchmark.types import BenchConfig
from jiuwenswarm.pact_core.schemas import TurnRequest

from jiuwenswarm.lh.plugins.transport_recovery import RECOVERY_ADAPTER, RecoveringJiuwenWorker, stream_failure

READ_ERROR = "[181001] model call failed, reason: openAI API async stream error: ReadError"
IDLE_ERROR = (
    "[181001] model call failed, reason: LLM stream timeout: stream frame timeout: "
    "stage=idle_chunk, timeout=120.0s, chunk_count=856, idle_elapsed=120.01s, total_elapsed=125.64s"
)
FIRST_ERROR = IDLE_ERROR.replace("idle_chunk", "first_chunk")


def failed_receipts(directory, *, message=READ_ERROR, **metadata_changes):
    directory.mkdir(parents=True, exist_ok=True)
    terminal = {
        "status": "failed",
        "exit_code": 1,
        "session_id": "failed-native-session",
        "error": {"code": "RUNTIME_ERROR", "message": message, "retryable": False},
    }
    metadata = {
        "stage": "benchmark-worker",
        "failure_code": "NATIVE_FAILED",
        "timed_out": False,
        "signal": None,
        "actual_exit_code": 1,
        **metadata_changes,
    }
    (directory / "terminal-result.json").write_text(json.dumps(terminal))
    (directory / "metadata.json").write_text(json.dumps(metadata))


@pytest.mark.parametrize("message", [READ_ERROR, IDLE_ERROR, FIRST_ERROR])
def test_observed_stream_interruptions_are_classified_from_native_receipts(tmp_path, message):
    failed_receipts(tmp_path, message=message)
    evidence = stream_failure(tmp_path, stage="benchmark-worker")
    assert evidence["failure"] == message
    assert len(evidence["terminal_sha256"]) == len(evidence["metadata_sha256"]) == 64


@pytest.mark.parametrize(
    "changes",
    [{"timed_out": True}, {"signal": 9}, {"actual_exit_code": 137}, {"budget_stop_observed": True}],
)
def test_hard_deadline_signal_oom_and_budget_stops_are_not_stream_recovery(tmp_path, changes):
    failed_receipts(tmp_path, **changes)
    assert stream_failure(tmp_path, stage="benchmark-worker") is None


def test_model_prose_and_non_transport_runtime_errors_are_not_recovery_evidence(tmp_path):
    failed_receipts(tmp_path, message="The implementation mentions " + READ_ERROR)
    assert stream_failure(tmp_path, stage="benchmark-worker") is None
    failed_receipts(tmp_path, message="[181001] model call failed, reason: authentication failed")
    assert stream_failure(tmp_path, stage="benchmark-worker") is None
    (tmp_path / "metadata.json").unlink()
    assert stream_failure(tmp_path, stage="benchmark-worker") is None


@pytest.fixture
def native_types():
    return pytest.importorskip("jiuwenswarm.long_horizon.worker")


class InterruptedWorker:
    source_commit = "scripted-native-source"

    def __init__(self, root, types, *, fail_again=False, config_drift=False, cancel_recovery=False):
        self.binary, self.config_template = root / "binary", root / "config"
        self.binary.write_text("scripted binary")
        self.config_template.write_text("scripted config")
        self.types, self.calls = types, []
        self.fail_again, self.config_drift, self.cancel_recovery = fail_again, config_drift, cancel_recovery

    async def run(self, **kwargs):
        self.calls.append(kwargs)
        directory, workspace = Path(kwargs["output_dir"]), Path(kwargs["workspace"])
        if len(self.calls) == 1 or self.fail_again:
            (workspace / "retained.py").write_text("partial candidate survives\n")
            failed_receipts(directory)
            if self.config_drift:
                self.config_template.write_text("changed config")
            raise self.types.WorkerExecutionError("native failed", code="NATIVE_FAILED", output_dir=directory)
        if self.cancel_recovery:
            raise asyncio.CancelledError
        assert (workspace / "retained.py").read_text() == "partial candidate survives\n"
        assert "session_id" not in kwargs
        assert "[TRANSPORT_RECOVERY]" in kwargs["prompt"]
        directory.mkdir()
        (directory / "metadata.json").write_text('{"status":"completed"}')
        return self.types.WorkerResult("Fixed\nPACT_REVIEW_READY", [], "fresh-native-session", {}, "completed", 0)


def setup_recovery(tmp_path, types, **worker_options):
    worker = InterruptedWorker(tmp_path, types, **worker_options)
    proxy = RecoveringJiuwenWorker(worker)
    budget = BenchBudget(
        tmp_path / "ledger", BenchConfig(task_id="task", trial_id="trial", budget_seconds=600), "same-work"
    )
    proxy.budget = budget
    request = TurnRequest(role="worker", prompt="original task verbatim", workdir=tmp_path, timeout_seconds=120)
    return worker, proxy, budget, request


async def invoke(proxy, budget, request, output):
    ticket, timeout, _ = budget.start_call(
        request, adapter_id="native-worker", purpose="implementation", deadline=budget.snapshot()["deadline"]
    )
    try:
        result = await proxy.run(
            stage="benchmark-worker",
            prompt=request.prompt,
            workspace=request.workdir,
            output_dir=output,
            read_only=False,
            timeout_seconds=timeout,
        )
        budget.finish_call(ticket, status="completed")
        return result
    except BaseException:
        budget.finish_call(ticket, status="error")
        raise


@pytest.mark.asyncio
async def test_recovery_retains_checkout_charges_call_and_keeps_original_deadline(tmp_path, native_types):
    worker, proxy, budget, request = setup_recovery(tmp_path, native_types)
    before = budget.snapshot()
    result = await invoke(proxy, budget, request, tmp_path / "turn")
    after = budget.snapshot()
    assert result.status == "completed" and result.session_id == "fresh-native-session"
    assert after["deadline"] == before["deadline"] and after["role_calls"] == 2
    assert after["calls"][1]["adapter_id"] == RECOVERY_ADAPTER
    assert 0 < worker.calls[1]["timeout_seconds"] < worker.calls[0]["timeout_seconds"]
    assert (tmp_path / "turn/terminal-result.json").exists()
    receipt = json.loads((tmp_path / "turn/transport-recovery.json").read_text())
    assert receipt["native_session_continuation"] is False and receipt["status"] == "completed"
    assert receipt["parent"]["failed_session_id"] == "failed-native-session"


@pytest.mark.asyncio
async def test_second_stream_failure_is_not_retried_even_after_proxy_recreation(tmp_path, native_types):
    worker, proxy, budget, request = setup_recovery(tmp_path, native_types, fail_again=True)
    with pytest.raises(native_types.WorkerExecutionError):
        await invoke(proxy, budget, request, tmp_path / "turn")
    assert len(worker.calls) == 2
    replacement = RecoveringJiuwenWorker(worker)
    replacement.budget = budget
    with pytest.raises(native_types.WorkerExecutionError):
        await invoke(replacement, budget, request, tmp_path / "next-turn")
    assert len(worker.calls) == 3
    assert sum(c["adapter_id"] == RECOVERY_ADAPTER for c in budget.snapshot()["calls"]) == 1


@pytest.mark.asyncio
async def test_call_cap_reserves_review_and_refuses_recovery(tmp_path, native_types):
    worker, proxy, _, request = setup_recovery(tmp_path, native_types)
    budget = BenchBudget(
        tmp_path / "limited",
        BenchConfig(task_id="task", trial_id="trial", budget_seconds=600, max_role_calls=2),
        "same-work",
    )
    proxy.budget = budget
    with pytest.raises(native_types.WorkerExecutionError):
        await invoke(proxy, budget, request, tmp_path / "turn")
    assert len(worker.calls) == 1 and budget.snapshot()["role_calls"] == 1
    assert json.loads((tmp_path / "turn/transport-recovery.json").read_text())["refused"] == "review_reserved"


@pytest.mark.asyncio
async def test_changed_configuration_never_launches_recovery(tmp_path, native_types):
    worker, proxy, budget, request = setup_recovery(tmp_path, native_types, config_drift=True)
    with pytest.raises(native_types.WorkerExecutionError):
        await invoke(proxy, budget, request, tmp_path / "turn")
    assert len(worker.calls) == 1 and budget.snapshot()["role_calls"] == 1


@pytest.mark.asyncio
async def test_cancellation_preserves_attempted_recovery_without_another_call(tmp_path, native_types):
    worker, proxy, budget, request = setup_recovery(tmp_path, native_types, cancel_recovery=True)
    with pytest.raises(asyncio.CancelledError):
        await invoke(proxy, budget, request, tmp_path / "turn")
    assert len(worker.calls) == 2 and budget.snapshot()["role_calls"] == 2
    assert all(c["status"] == "error" for c in budget.snapshot()["calls"])


@pytest.mark.parametrize("message", [READ_ERROR, IDLE_ERROR, FIRST_ERROR])
@pytest.mark.asyncio
async def test_real_native_process_receipts_recover_without_resuming_failed_session(tmp_path, native_types, message):
    executable = tmp_path / "native-process"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, pathlib, sys\n"
        "request = json.load(sys.stdin)\n"
        "assert sys.argv[1:] == ['--run-json', '-']\n"
        "assert 'session_id' not in request\n"
        "workspace = pathlib.Path(request['workspace']['cwd'])\n"
        "recovering = '[TRANSPORT_RECOVERY]' in request['input']\n"
        "if recovering:\n"
        "    assert (workspace/'retained.py').read_text() == '# retained candidate\\n'\n"
        "else:\n"
        "    (workspace/'retained.py').write_text('# retained candidate\\n')\n"
        "code = 0 if recovering else 1\n"
        "print(json.dumps(dict(schema_version='0.1', type='result', sequence=0,\n"
        "    request_id=request['request_id'], session_id='recovered' if recovering else 'interrupted',\n"
        "    status='completed' if recovering else 'failed', exit_code=code,\n"
        "    output='Complete' if recovering else 'Partial',\n"
        f"    error=None if recovering else dict(code='RUNTIME_ERROR', message={message!r}, retryable=False, details={{}}),\n"
        "    usage={'input_tokens':11,'output_tokens':7})), flush=True)\n"
        "sys.exit(code)\n"
    )
    executable.chmod(0o755)
    template = tmp_path / "config.yaml"
    template.write_text("models: {}\npermissions:\n  defaults:\n    '*': allow\n")
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    native = native_types.JiuwenWorker(executable, template, default_timeout_seconds=30)
    proxy = RecoveringJiuwenWorker(native)
    budget = BenchBudget(
        tmp_path / "ledger", BenchConfig(task_id="task", trial_id="trial", budget_seconds=600), "original-work"
    )
    proxy.budget = budget
    request = TurnRequest(role="worker", prompt="Original public task", workdir=workspace, timeout_seconds=30)
    result = await invoke(proxy, budget, request, tmp_path / "turn")
    assert result.status == "completed" and result.session_id == "recovered"
    parent = json.loads((tmp_path / "turn/metadata.json").read_text())
    child = json.loads((tmp_path / "turn/transport-recovery-1/metadata.json").read_text())
    assert parent["failure_code"] == "NATIVE_FAILED" and parent["actual_exit_code"] == 1
    assert parent["session_continuation"] is False and child["session_continuation"] is False
    assert parent["state_dir"] != child["state_dir"]
    assert child["requested_timeout_seconds"] < parent["requested_timeout_seconds"]
    assert budget.snapshot()["role_calls"] == 2
