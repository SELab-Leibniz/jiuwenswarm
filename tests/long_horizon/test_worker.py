"""Native transport integration tests with a fake executable, never a model."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

import yaml

from jiuwenswarm.channels.process_cli.protocol.model import OneShotRunInput
from jiuwenswarm.long_horizon.worker import (
    BASE_COMMIT, JiuwenWorker, READ_ONLY_TOOLS, WorkerBudgetExhaustedError,
    WorkerConfigurationError, WorkerExecutionError, WorkerProtocolError,
    WorkerTimeoutError, normalize_tool_events,
)


_FAKE_HEADER = """
import json, os, pathlib, signal, subprocess, sys, time
request = json.load(sys.stdin)
assert sys.argv[1:] == ['--run-json', '-']
def event(sequence=0, kind='chat.final', payload=None):
    return dict(schema_version='0.1', type='event', sequence=sequence,
                request_id=request['request_id'], session_id='real-native-session',
                event_type=kind, payload=payload)
def result(sequence=0, status='completed', exit_code=0, output='done', error=None):
    return dict(schema_version='0.1', type='result', sequence=sequence,
                request_id=request['request_id'], session_id='real-native-session',
                status=status, exit_code=exit_code, output=output, error=error,
                usage={'input_tokens': 11, 'output_tokens': 7})
def emit(record):
    print(json.dumps(record), flush=True)
"""


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.template = self.root / "config.yaml"
        self.template.write_text("models:\n  default:\n    api_key: ${TEST_MODEL_API_KEY}\npermissions:\n  defaults:\n    '*': allow\n  rules:\n    - id: allow-bash\n      tools: [bash]\n      action: allow\n")

    def tearDown(self):
        self.temp.cleanup()

    def worker(self, body: str) -> JiuwenWorker:
        executable = self.root / "fake-native"
        executable.write_text(f"#!{sys.executable}\n" + textwrap.dedent(_FAKE_HEADER) + textwrap.dedent(body))
        executable.chmod(0o755)
        return JiuwenWorker(executable, self.template, default_timeout_seconds=3)

    async def run_worker(self, worker: JiuwenWorker, **kwargs):
        return await worker.run(stage="test", prompt="inspect task", workspace=self.workspace,
                                output_dir=self.root / "stage", **kwargs)

    def metadata(self) -> dict:
        return json.loads((self.root / "stage/metadata.json").read_text())

    async def test_native_schema_state_and_readonly_policy(self):
        worker = self.worker("""
        state = pathlib.Path(os.environ['JIUWENSWARM_DATA_DIR'])
        assert state == pathlib.Path.cwd()
        assert os.environ['JIUWENSWARM_CONFIG_DIR'] == str(state / 'config')
        assert pathlib.Path(request['workspace']['cwd']).is_dir()
        assert request['mode'] == 'agent.code.normal'
        assert 'agent' not in request
        emit(event(payload={'content':'thinking'}))
        emit(result(sequence=1, output='{"locations":[]}'))
        """)
        result = await self.run_worker(worker, read_only=True, schema={"type": "object"})
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.session_id, "real-native-session")
        self.assertEqual(result.usage, {"input_tokens": 11, "output_tokens": 7})
        request = json.loads((self.root / "stage/request.json").read_text())
        OneShotRunInput.from_dict(request)
        self.assertIn("JSON Schema", request["input"])
        config = yaml.safe_load((self.root / "stage/state/config/config.yaml").read_text())
        self.assertEqual(config["permissions"]["defaults"], {"*": "deny"})
        self.assertEqual({name for name, level in config["permissions"]["tools"].items() if level == "allow"}, set(READ_ONLY_TOOLS))
        for name in ("bash", "write_file", "edit_file", "subagent_spawn", "tool_call"):
            self.assertEqual(config["permissions"]["tools"][name], "deny")
        self.assertEqual(config["permissions"]["rules"], [])
        self.assertEqual(config["models"]["default"]["api_key"], "${TEST_MODEL_API_KEY}")
        metadata = self.metadata()
        self.assertEqual(metadata["source"]["base_commit"], BASE_COMMIT)
        self.assertIn("checkout_commit", metadata["source"])
        self.assertEqual(metadata["actual_exit_code"], 0)
        self.assertEqual(metadata["automatic_stage_retries"], 0)
        self.assertEqual(result.events[0]["event_type"], "chat.final")
        self.assertEqual(len((self.root / "stage/events.jsonl").read_text().splitlines()), 2)

    async def test_normal_stage_uses_native_default_agent(self):
        worker = self.worker("assert 'agent' not in request\nemit(result())\n")
        await self.run_worker(worker)

    async def test_native_config_rewrite_has_distinct_archived_hash(self):
        worker = self.worker("""
        path = pathlib.Path(os.environ['JIUWENSWARM_CONFIG_DIR']) / 'config.yaml'
        path.write_text(path.read_text() + '\\n# native defaults merged\\n')
        emit(result())
        """)
        await self.run_worker(worker)
        metadata = self.metadata()
        self.assertNotEqual(metadata["requested_config_sha256"], metadata["archived_config_sha256"])

    async def test_no_fake_session_resume(self):
        with self.assertRaises(WorkerConfigurationError) as context:
            await self.run_worker(self.worker("raise AssertionError('must not run')"), session_id="old")
        self.assertEqual(context.exception.code, "CONTINUATION_UNSUPPORTED")

    async def test_inline_credentials_rejected_before_start(self):
        self.template.write_text("models:\n  api_key: hardcoded-secret-value\n")
        with self.assertRaises(WorkerConfigurationError):
            await self.run_worker(self.worker("raise AssertionError('must not run')"))
        self.assertEqual(self.metadata()["status"], "configuration_failed")
        self.assertFalse((self.root / "stage/state/config/config.yaml").exists())

    async def test_literal_default_credential_rejected(self):
        self.template.write_text("api_key: ${KEY:-literal-secret}\n")
        with self.assertRaises(WorkerConfigurationError):
            await self.run_worker(self.worker("raise AssertionError('must not run')"))

    async def test_immutable_output_and_no_retry(self):
        worker = self.worker("emit(result())")
        await self.run_worker(worker)
        original = (self.root / "stage/metadata.json").read_bytes()
        with self.assertRaises(WorkerConfigurationError):
            await self.run_worker(worker)
        self.assertEqual(original, (self.root / "stage/metadata.json").read_bytes())

    async def test_duplicate_terminal_is_protocol_failure(self):
        worker = self.worker("emit(result())\nemit(result(sequence=1))")
        with self.assertRaises(WorkerProtocolError) as context:
            await self.run_worker(worker)
        self.assertEqual(context.exception.result.status, "failed")
        self.assertIn('"sequence": 1', (self.root / "stage/stdout.jsonl").read_text())

    async def test_junk_stdout_and_duplicate_json_fields_are_rejected(self):
        for body in ("print('debug', flush=True)\nemit(result())",
                     "print('{\"type\":\"event\",\"type\":\"result\"}')\nemit(result())"):
            worker = self.worker(body)
            output = self.root / ("stage-" + str(len(list(self.root.glob("stage-*")))))
            with self.assertRaises(WorkerProtocolError):
                await worker.run(stage="test", prompt="inspect", workspace=self.workspace, output_dir=output)

    async def test_terminal_actual_exit_disagreement_fails(self):
        with self.assertRaises(WorkerProtocolError) as context:
            await self.run_worker(self.worker("emit(result())\nsys.exit(4)"))
        self.assertEqual(context.exception.result.exit_code, 4)
        self.assertEqual(self.metadata()["actual_exit_code"], 4)

    async def test_runtime_failure_keeps_usage_and_safe_error(self):
        worker = self.worker("""
        emit(result(status='failed', exit_code=1,
                    error={'code':'PROVIDER_ERROR', 'message':os.environ['TEST_MODEL_API_KEY'],
                           'retryable':False, 'details':{}}))
        print(os.environ['TEST_MODEL_API_KEY'], file=sys.stderr)
        sys.exit(1)
        """)
        with patch.dict(os.environ, {"TEST_MODEL_API_KEY": "secret-model-key-abcdefghijkl"}):
            with self.assertRaises(WorkerExecutionError) as context:
                await self.run_worker(worker)
        self.assertEqual(context.exception.result.usage["output_tokens"], 7)
        self.assertNotIn("secret-model-key", str(context.exception))
        for path in (self.root / "stage").rglob("*"):
            if path.is_file():
                self.assertNotIn("secret-model-key", path.read_text())

    async def test_explicit_budget_signal_preserved(self):
        worker = self.worker("""
        emit(event(kind='chat.delta', payload={'delta':'partial'}))
        emit(event(sequence=1, kind='chat.usage_metadata', payload={'metadata': {'usage_metadata': {'finish_reason':'length', 'output_tokens':7}}}))
        time.sleep(2)
        pathlib.Path('should-not-run').write_text('native continued after budget')
        emit(result(sequence=2, output='partial'))
        """)
        with self.assertRaises(WorkerBudgetExhaustedError) as context:
            await self.run_worker(worker)
        self.assertEqual(context.exception.result.status, "budget_exhausted")
        self.assertEqual(context.exception.result.final_text, "partial")
        self.assertNotEqual(self.metadata()["actual_exit_code"], 0)
        self.assertEqual(self.metadata()["status"], "budget_exhausted")
        self.assertTrue(self.metadata()["budget_stop_observed"])
        self.assertEqual(context.exception.result.usage["output_tokens"], 7)
        self.assertFalse((self.root / "stage/state/should-not-run").exists())
        self.assertFalse(self.metadata()["terminal_result_received"])

    async def test_words_in_model_text_do_not_fake_budget(self):
        result = await self.run_worker(self.worker("emit(result(output='BUDGET_EXHAUSTED finish_reason length'))"))
        self.assertEqual(result.status, "completed")

    async def test_timeout_saves_partial_and_terminates_group(self):
        worker = self.worker("""
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        pathlib.Path('child.pid').write_text(str(child.pid))
        emit(event(kind='chat.delta', payload={'delta':'partial'}))
        time.sleep(60)
        """)
        with self.assertRaises(WorkerTimeoutError) as context:
            await self.run_worker(worker, timeout_seconds=1.2)
        self.assertEqual(context.exception.result.status, "timed_out")
        self.assertEqual(len(context.exception.result.events), 1)
        self.assertTrue(self.metadata()["timed_out"])
        child_pid = int((self.root / "stage/state/child.pid").read_text())
        # Linux may retain a reparented zombie briefly; a live process is not allowed.
        status = subprocess_status(child_pid)
        self.assertIn(status, (None, "Z"))

    async def test_async_cancellation_records_interruption(self):
        worker = self.worker("emit(event(payload={'content':'start'}))\ntime.sleep(60)")
        task = asyncio.create_task(self.run_worker(worker))
        for _ in range(100):
            if (self.root / "stage/events.jsonl").exists():
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.metadata()["status"], "cancelled")


def subprocess_status(pid: int) -> str | None:
    import subprocess
    result = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    return result.stdout.strip()[:1] or None


class ToolNormalizationTests(unittest.TestCase):
    def native(self, kind: str, payload: dict, sequence=0) -> dict:
        return dict(type="event", event_type="chat." + kind, sequence=sequence,
                    request_id="req", session_id="session", schema_version="0.1", payload={kind: payload})

    def test_native_tool_evidence_has_explicit_success_and_provenance(self):
        call = self.native("tool_call", {"tool_call_id": "1", "name": "read_file",
                                        "arguments": '{"file_path":"a.py","offset":1,"limit":5}'})
        result = self.native("tool_result", {"tool_call_id": "1", "tool_name": "read_file",
                                            "success": True, "raw_output": {"content": "actual bytes"}}, 1)
        normalized = normalize_tool_events([call, result])
        self.assertEqual(normalized[0]["arguments"]["file_path"], "a.py")
        self.assertIs(normalized[1]["is_error"], False)
        self.assertEqual(normalized[1]["result"], {"content": "actual bytes"})
        self.assertEqual(normalized[1]["provenance"]["sequence"], 1)

    def test_missing_success_never_means_success(self):
        result = self.native("tool_result", {"tool_call_id": "1", "tool_name": "read_file", "result": "text"})
        self.assertIsNone(normalize_tool_events([result])[0]["success"])

    def test_failure_wins_over_conflicting_success(self):
        result = self.native("tool_result", {"tool_call_id": "1", "tool_name": "read_file",
                                            "success": True, "is_error": True, "result": "text"})
        self.assertFalse(normalize_tool_events([result])[0]["success"])


if __name__ == "__main__":
    unittest.main()
