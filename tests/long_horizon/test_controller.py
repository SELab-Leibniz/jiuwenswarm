"""Model-free workflow tests: sequencing, evidence gates and recovery."""

import asyncio
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile
import sys
from types import SimpleNamespace
import unittest

from jiuwenswarm.long_horizon.controller import (
    BASE_AGENT_COMMIT, SuiteConfig, SuiteController, SuiteServices, capture_recovery,
)


class NativeFailure(RuntimeError):
    def __init__(self, status, code="NATIVE_FAILED"):
        super().__init__(status)
        self.code = code
        self.result = SimpleNamespace(status=status, final_text="partial", session_id="partial-session", exit_code=137)


class FakeWorker:
    def __init__(self, fail_stage=None, failure=None):
        self.calls = []
        self.fail_stage, self.failure = fail_stage, failure

    async def run(self, **kwargs):
        self.calls.append(kwargs)
        kwargs["output_dir"].mkdir(parents=True)
        stage = kwargs["stage"]
        (kwargs["workspace"] / "source.py").write_text(f"{stage}\n")
        if stage == self.fail_stage:
            raise self.failure
        return SimpleNamespace(status="completed", exit_code=0, final_text=f"{stage} answer", events=[], usage={}, session_id=f"{stage}-session")


class PipelineWorker:
    """The only fake boundary in the real-component integration test.

    Tool observations describe actual fixture reads. Public checks, Git
    checkpoint/promotion, clarification validation and PACT run for real.
    """

    def __init__(self, *, fail_repair=False):
        self.calls = []
        self.fail_repair = fail_repair

    async def run(self, **kwargs):
        self.calls.append(kwargs)
        stage, workspace, output = kwargs["stage"], kwargs["workspace"], kwargs["output_dir"]
        output.mkdir(parents=True, exist_ok=True)
        session = f"fixture-{len(self.calls)}"
        events = []
        if kwargs.get("tool_policy") == "none":
            events.append({"schema_version": "0.1", "type": "event", "request_id": session,
                           "session_id": session, "sequence": 0, "event_type": "chat.tool_policy",
                           "payload": {"policy": "none", "registered_tools": [], "verified": True, "profile": "code.no-tools.v1"}})
        if kwargs.get("read_only") and not stage.startswith("pact_") and kwargs.get("tool_policy") != "none":
            for index, (name, arguments, value) in enumerate([
                ("grep", {"pattern": "value", "path": "."}, "src/api.py:1:def value():"),
                ("read_file", {"file_path": "src/api.py"}, (workspace / "src/api.py").read_text()),
                ("read_file", {"file_path": "tests/test_api.py"}, (workspace / "tests/test_api.py").read_text()),
            ]):
                call_id = f"{session}-{index}"
                for event_type, payload in [
                    ("chat.tool_call", {"tool_call": {"tool_call_id": call_id, "name": name, "arguments": arguments}}),
                    ("chat.tool_result", {"tool_result": {"tool_call_id": call_id, "tool_name": name, "result": value, "success": True}}),
                ]:
                    events.append({"schema_version": "0.1", "type": "event", "request_id": session,
                                   "session_id": session, "sequence": len(events), "event_type": event_type, "payload": payload})
        anchor = {"kind": "current_repo", "anchor": "src/api.py:1-2"}
        adjacent = {"kind": "current_repo", "anchor": "tests/test_api.py:1-2"}
        if stage.startswith("clarification-proposal-"):
            payload = {
                "verdict": "clarification_needed", "rationale": "The API defines the integer return contract.",
                "coverage": [
                    {"target": "primary", "status": "found", "summary": "API owner.", "evidence": [anchor]},
                    {"target": "adjacent", "status": "found", "summary": "Public compatibility test.", "evidence": [adjacent]},
                ], "evidence": [anchor, adjacent], "guidance_points": [
                    {"category": "behavior", "statement": "Preserve the integer return type while changing the value.",
                     "confidence": 0.95, "basis": "current_repo", "contract_cell": "values_defaults",
                     "decision_impact": "Existing callers retain the integer return type.", "evidence": [anchor],
                     "risk": "Changing the return type breaks callers."}]}
        elif stage == "clarification-selector":
            payload = {"reviews": [{"candidate_id": f"p{i}-g1", "decision": "select" if i == 1 else "reject",
                                     "rationale": "Retain one evidenced owner guidance and remove duplicates."} for i in (1, 2, 3)]}
        elif stage == "localization":
            payload = {"summary": "Located the API owner.", "locations": [
                {"file_path": "src/api.py", "role": "primary", "start_line": 1, "end_line": 2,
                 "reason": "Owns value return.", "confidence": "high", "evidence": {"quote": "return 1"}}]}
        elif stage == "pact-goal-contract":
            payload = {"schema": "pact-runtime/goal-contract/v1", "goal": "Return integer two from value()",
                       "acceptance_criteria": [{"id": "AC1", "text": "value() returns integer 2"}], "non_goals": []}
        elif stage == "pact-initial-plan":
            payload = {"schema": "pact-runtime/initial-plan/v1", "constraints": [], "missions": [
                {"id": "M1", "objective": "Set value() to two", "target_ac_ids": ["AC1"], "dependencies": [],
                 "verification_intent": "Import the public API and assert its value."}]}
        elif stage.startswith("public-verification-"):
            payload = {"schema": "jiuwenswarm/pact-public-verification/v1", "status": "ready",
                       "reason": "Original public requirement and inspected API.", "checks": [
                           {"id": "check1", "command": shlex.quote(sys.executable) + " -c 'from src.api import value; assert value() == 2 and type(value()) is int'",
                            "target_ac_ids": ["AC1"], "rationale": "Directly asserts the requested public behavior."}]}
        elif stage == "pact_manager":
            projection = json.loads(re.search(r"<revision-bound-input>\s*(.*?)\s*</revision-bound-input>", kwargs["prompt"], re.S).group(1))
            eligible = projection["work_state"]["frontier"]["eligible"]
            payload = {"schema": "pact-runtime/manager-decision-proposal/v1", "phase": "route",
                       "expected_plan_revision": projection["plan_revision"], "expected_work_state_revision": projection["work_state_revision"],
                       "action": "select" if eligible and projection["trigger"] == "frontier_selection" else "block",
                       "reason": "Select the declared eligible fixture mission."}
            if payload["action"] == "select":
                payload["selected_mission_id"] = eligible[0]
        elif stage == "pact_reviewer":
            payload = {"verdict": "complete"}
        elif stage in {"p0", "repair", "pact_worker"}:
            value = {"p0": 0, "repair": 3, "pact_worker": 2}[stage]
            (workspace / "src/api.py").write_text(f"def value():\n    return {value}\n")
            if stage == "repair" and self.fail_repair:
                subprocess.run(["git", "-C", str(workspace), "add", "src/api.py"], check=True)
                subprocess.run(["git", "-C", str(workspace), "commit", "-qm", "failed repair candidate"], check=True)
            payload = f"Fixture {stage} candidate prepared."
        else:
            raise AssertionError(f"Unexpected model boundary: {stage}")
        text = json.dumps(payload) if isinstance(payload, dict) else payload
        result = SimpleNamespace(final_text=text, events=events, session_id=session, usage={}, status="completed", exit_code=0)
        if stage == "repair" and self.fail_repair:
            result.status, result.exit_code = "failed", 1
        terminal = {"schema_version": "0.1", "type": "result", "request_id": session, "sequence": len(events),
                    "session_id": session, "status": result.status, "exit_code": result.exit_code,
                    "error": {"code": "NATIVE_FAILED"} if result.exit_code else None, "output": text, "usage": {}}
        # These are explicit test doubles of the native-worker boundary, never
        # model measurements; the host evidence reader can consume the fixture.
        worker_path = Path(__file__).resolve().parents[2] / "jiuwenswarm/long_horizon/worker.py"
        metadata = {"stage": stage, "actual_exit_code": result.exit_code, "timed_out": False, "signal": None,
                    "terminal_result_received": True, "status": result.status, "session_id": session, "request_id": session,
                    "tool_policy": kwargs.get("tool_policy", "configured"),
                    "source": {"base_commit": BASE_AGENT_COMMIT, "adapter_sha256": hashlib.sha256(worker_path.read_bytes()).hexdigest()}}
        if kwargs.get("tool_policy") == "none":
            source_root = Path(__file__).resolve().parents[2]
            metadata["tool_policy_proof_sequences"] = [0]
            for key, relative in (
                ("native_tool_free_sha256", "jiuwenswarm/long_horizon/native_tool_free.py"),
                ("native_code_spec_sha256", "jiuwenswarm/agents/harness/code/spec.py"),
                ("native_code_interface_sha256", "jiuwenswarm/server/runtime/agent_adapter/interface_code.py"),
            ):
                metadata["source"][key] = hashlib.sha256((source_root / relative).read_bytes()).hexdigest()
        for name, value in (("metadata.json", metadata), ("request.json", {"request_id": session}), ("terminal-result.json", terminal)):
            (output / name).write_text(json.dumps(value))
        (output / "events.jsonl").write_text("".join(json.dumps(value) + "\n" for value in [*events, terminal]))
        if result.exit_code:
            error = NativeFailure("failed")
            error.result = result
            raise error
        return result


class SuiteControllerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "source.py").write_text("original\n")
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Model-free test")
        self.git("add", "source.py")
        self.git("commit", "-qm", "initial")
        self.prompt = self.root / "prompt.md"
        self.prompt.write_bytes(b"Original authoritative requirement\r\n")
        self.config = SuiteConfig(self.workspace, self.prompt, self.root / "output", timeout_seconds=60, reserve_seconds=0)
        self.seen = []
        self.clarification_status = "completed"
        self.campaign_status = "completed"
        self.emit_receipt = True
        self.mutate_spec = False

    def tearDown(self):
        self.temporary.cleanup()

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.workspace), *args], capture_output=True, check=True).stdout

    def services(self):
        def capture(workspace, destination):
            destination.mkdir(parents=True)
            view = destination / "view"
            view.mkdir()
            shutil.copyfile(workspace / "source.py", view / "source.py")
            return {"view_root": str(view)}

        async def clarify(worker, snapshot, requirement, output_dir, timeout_seconds):
            self.seen.append(("clarification", (Path(snapshot["view_root"]) / "source.py").read_text(), requirement))
            return SimpleNamespace(status=self.clarification_status, delta="Repository-grounded guidance", empty_reason="")

        async def localize(worker, snapshot, requirement, output_dir, timeout_seconds):
            self.seen.append(("localization", (Path(snapshot["view_root"]) / "source.py").read_text(), requirement))
            return {"status": "completed", "primary": [{"path": "source.py"}]}

        async def generate(worker, snapshot, requirement, result, output_dir, **kwargs):
            self.seen.append(("pact_inputs", (Path(snapshot["view_root"]) / "source.py").read_text(), requirement))
            return {"goal": 1}

        def save(inputs, destination):
            destination.mkdir(parents=True)
            paths = {}
            for key, name in (("contract_path", "goal-contract.json"), ("plan_path", "initial-plan.json"), ("public_verification_path", "public-verification.json")):
                paths[key] = destination / name
                paths[key].write_text(json.dumps({"test_fixture": name}) + "\n")
            return paths

        async def campaign(worker, workspace, contract_path, plan_path, public_verification_path, output_dir, **kwargs):
            self.seen.append(("pact", (workspace / "source.py").read_text(), ""))
            output_dir.mkdir(parents=True)
            digest = hashlib.sha256(public_verification_path.read_bytes()).hexdigest()
            receipt = output_dir / "receipt.json"
            if self.emit_receipt:
                receipt.write_text(json.dumps({"fixture": "actual subprocess verification boundary is tested in pact tests"}))
            if self.mutate_spec:
                public_verification_path.write_text("changed")
            return {"status": self.campaign_status,
                    "public_verification": {"status": "executed", "spec_sha256": digest,
                                            "receipt_paths": [str(receipt.relative_to(self.config.output_dir))] if self.emit_receipt else []}}

        return SuiteServices(capture, lambda snapshot: Path(snapshot["view_root"]), clarify, generate, save, localize, campaign)

    async def test_complete_flow_keeps_s0_isolated_and_original_bytes(self):
        worker = FakeWorker()
        result = await SuiteController(self.config, worker, self.services()).run()
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["suite_complete"])
        self.assertIsNone(result["resolved"])
        self.assertEqual([call["stage"] for call in worker.calls], ["p0", "repair"])
        self.assertIn("Original authoritative requirement", worker.calls[1]["prompt"])
        self.assertEqual([text for stage, text, _ in self.seen if stage != "pact"], ["original\n"] * 3)
        self.assertEqual((self.config.output_dir / "requirement.md").read_bytes(), self.prompt.read_bytes())
        self.assertEqual(result["baseline"], "p1")
        self.assertEqual(result["memory"]["enabled"], False)
        self.assertTrue((self.config.output_dir / "recovery/current/history.bundle").is_file())

    async def test_degraded_clarification_promotes_p0_then_requires_actual_campaign(self):
        self.clarification_status = "degraded"
        worker = FakeWorker()
        result = await SuiteController(self.config, worker, self.services()).run()
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["suite_complete"])
        self.assertEqual(result["baseline"], "p0")
        self.assertEqual([call["stage"] for call in worker.calls], ["p0"])
        self.assertEqual(result["stages"]["clarification"]["status"], "degraded")
        self.assertEqual(result["stages"]["repair"]["status"], "skipped")
        self.assertEqual(result["stages"]["pact"]["status"], "completed")
        self.assertEqual((self.workspace / "source.py").read_text(), "p0\n")

    async def test_budget_exhaustion_keeps_partial_workspace_and_never_replays(self):
        worker = FakeWorker("repair", NativeFailure("budget_exhausted", "BUDGET_EXHAUSTED"))
        result = await SuiteController(self.config, worker, self.services()).run()
        self.assertEqual(result["status"], "budget_exhausted")
        self.assertFalse(result["suite_complete"])
        self.assertIsNone(result["resolved"])
        self.assertEqual([call["stage"] for call in worker.calls], ["p0", "repair"])
        self.assertEqual((self.config.output_dir / "snapshots/p0/view/source.py").read_text(), "p0\n")
        self.assertEqual((self.workspace / "source.py").read_text(), "repair\n")
        self.assertIn("+repair", (self.config.output_dir / "recovery/current/candidate.patch").read_text())

    async def test_signal_like_native_failure_stops_instead_of_promoting_baseline(self):
        worker = FakeWorker("p0", NativeFailure("cancelled", "INTERRUPTED"))
        result = await SuiteController(self.config, worker, self.services()).run()
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(len(worker.calls), 1)
        self.assertEqual(self.seen, [])
        self.assertIsNone(result["resolved"])
        self.assertTrue((self.config.output_dir / "recovery/current/history.bundle").is_file())

    async def test_invalid_native_protocol_stops_instead_of_promoting_baseline(self):
        from jiuwenswarm.long_horizon.worker import WorkerProtocolError

        error = WorkerProtocolError("Missing terminal result", code="MISSING_RESULT",
                                    output_dir=self.config.output_dir / "workers/p0")
        worker = FakeWorker("p0", error)
        result = await SuiteController(self.config, worker, self.services()).run()
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["suite_complete"])
        self.assertEqual([call["stage"] for call in worker.calls], ["p0"])
        self.assertEqual(self.seen, [])

    async def test_campaign_without_execution_receipt_cannot_complete(self):
        self.emit_receipt = False
        result = await SuiteController(self.config, FakeWorker(), self.services()).run()
        self.assertEqual(result["status"], "degraded")
        self.assertFalse(result["suite_complete"])

    async def test_public_spec_byte_mutation_is_rejected(self):
        self.mutate_spec = True
        result = await SuiteController(self.config, FakeWorker(), self.services()).run()
        self.assertEqual(result["status"], "failed")
        self.assertIn("bytes changed", result["incomplete_reason"])

    async def test_live_phase_timeout_is_bounded_and_does_not_retry(self):
        worker = FakeWorker()
        calls = []

        async def never_complete(**kwargs):
            calls.append(kwargs["stage"])
            await asyncio.sleep(100)

        worker.run = never_complete
        config = replace(self.config, timeout_seconds=2, p0_timeout_seconds=0.01, cleanup_timeout_seconds=1)
        result = await SuiteController(config, worker, self.services()).run()
        self.assertEqual(result["status"], "budget_exhausted")
        self.assertEqual(calls, ["p0"])

    async def test_existing_output_cannot_silently_resume(self):
        self.config.output_dir.mkdir()
        (self.config.output_dir / "summary.json").write_text("previous")
        with self.assertRaisesRegex(ValueError, "fresh"):
            await SuiteController(self.config, FakeWorker(), self.services()).run()

    async def test_p0_native_stage_timeout_preserves_partial_baseline_and_usage(self):
        error = NativeFailure("timed_out", "TIMEOUT")
        error.result.usage = {"input_tokens": 11, "output_tokens": 7}
        result = await SuiteController(self.config, FakeWorker("p0", error), self.services()).run()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["stages"]["p0"]["status"], "failed")
        self.assertEqual(result["stages"]["p0"]["partial_reason"], "stage_time_budget")
        self.assertEqual(result["worker_calls"][0]["usage"], error.result.usage)

    async def test_optional_localization_timeout_does_not_skip_campaign(self):
        services = self.services()
        async def timeout(*_args, **_kwargs):
            raise TimeoutError("optional stage deadline")
        result = await SuiteController(self.config, FakeWorker(), replace(services, localize=timeout)).run()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["stages"]["localization"]["reason"], "localization_time_budget")
        self.assertEqual(result["stages"]["pact"]["status"], "completed")

    async def test_localization_integrity_error_is_fatal_even_with_timeout_result(self):
        from jiuwenswarm.long_horizon.clarification.budget import SideCallIntegrityError
        from jiuwenswarm.long_horizon.worker import WorkerProtocolError

        errors = [WorkerProtocolError("Invalid native receipt", code="MALFORMED_EVENT",
                                     output_dir=self.config.output_dir / "localization"),
                  SideCallIntegrityError("S0 changed")]
        for index, error in enumerate(errors):
            with self.subTest(error=type(error).__name__):
                error.result = SimpleNamespace(status="timed_out")
                async def fail(*_args, **_kwargs):
                    raise error
                worker = FakeWorker()
                config = replace(self.config, output_dir=self.root / f"integrity-{index}")
                result = await SuiteController(config, worker, replace(self.services(), localize=fail)).run()
                self.assertEqual(result["status"], "failed")
                self.assertFalse(result["suite_complete"])
                self.assertEqual(result["stages"]["localization"]["status"], "failed")
                self.assertNotIn("pact", result["stages"])
                self.assertEqual([call["stage"] for call in worker.calls], ["p0"])

    async def test_input_generation_failure_still_repairs_but_cannot_complete_suite(self):
        services = self.services()
        async def fail(*_args, **_kwargs):
            raise ValueError("rejected goal JSON")
        worker = FakeWorker()
        result = await SuiteController(self.config, worker, replace(services, generate_pact_inputs=fail)).run()
        self.assertEqual([call["stage"] for call in worker.calls], ["p0", "repair"])
        self.assertEqual(result["stages"]["pact_inputs"]["status"], "failed")
        self.assertEqual(result["status"], "degraded")
        self.assertFalse(result["suite_complete"])

    async def test_invalid_recovery_is_fatal_before_any_model_call(self):
        package = self.root / "invalid-package"
        package.mkdir()
        worker = FakeWorker()
        config = replace(self.config, recovery_directory=package, recovery_package_sha256="0" * 64)
        result = await SuiteController(config, worker, self.services()).run()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(worker.calls, [])
        self.assertNotIn("p0", result["stages"])

    async def test_actual_components_connect_through_a_real_pact_campaign(self):
        (self.workspace / "src").mkdir()
        (self.workspace / "tests").mkdir()
        (self.workspace / "src/api.py").write_text("def value():\n    return 1\n")
        (self.workspace / "tests/test_api.py").write_text("def test_value():\n    assert True\n")
        (self.workspace / ".gitignore").write_text(".pact/\n.pact-io/\n__pycache__/\n")
        self.git("add", ".")
        self.git("commit", "-qm", "public API fixture")
        self.prompt.write_text("Change the public value() API to return integer 2.")
        worker = PipelineWorker()
        result = await SuiteController(self.config, worker).run()
        self.assertEqual(result["status"], "completed", result.get("incomplete_reason"))
        self.assertEqual(result["stages"]["localization"]["status"], "completed")
        self.assertEqual((self.workspace / "src/api.py").read_text(), "def value():\n    return 2\n")
        self.assertEqual(result["public_verification"]["status"], "executed")
        self.assertTrue(result["public_verification"]["receipt_paths"])
        stages = [call["stage"] for call in worker.calls]
        self.assertEqual(stages[0], "p0")
        self.assertIn("clarification-proposal-3", stages)
        self.assertIn("public-verification-review-ac1-synthesis", stages)
        self.assertIn("pact_reviewer", stages)
        self.assertLess(stages.index("pact-goal-contract"), stages.index("repair"))
        self.assertIsNone(result["resolved"])
        self.assertEqual([item["stage"] for item in result["checkpoints"]],
                         ["snapshot", "initial_implementation", "clarification", "pact_input", "repair", "delivery"])
        self.assertFalse(result.get("checkpoint_failures"))
        from jiuwenswarm.long_horizon.checkpoints import verify_stage_checkpoint
        for item in result["checkpoints"]:
            payload, _snapshot = verify_stage_checkpoint(self.config.output_dir / item["record"])
            self.assertEqual(payload["metadata"]["identities"], result["identities"])
        self.integration_result = result

    async def test_explicit_p0_recovery_skips_model_baseline_and_runs_remaining_real_components(self):
        await self.test_actual_components_connect_through_a_real_pact_campaign()
        from jiuwenswarm.long_horizon.recovery import export_p0_recovery
        package = self.root / "p0-package"
        manifest = export_p0_recovery(self.config.output_dir, package)
        digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
        target = self.root / "recovery-target"
        subprocess.run(["git", "clone", "--quiet", str(self.workspace), str(target)], check=True, capture_output=True)
        base = self.integration_result["identities"]["workspace_base_commit"]
        subprocess.run(["git", "-C", str(target), "checkout", "--quiet", "--detach", base], check=True, capture_output=True)
        for key, value in (("user.name", "Recovery Test"), ("user.email", "recovery@example.invalid")):
            subprocess.run(["git", "-C", str(target), "config", key, value], check=True)
        config = replace(self.config, workspace=target, output_dir=self.root / "recovered-output",
                         recovery_directory=package, recovery_package_sha256=digest)
        worker = PipelineWorker()
        result = await SuiteController(config, worker).run()
        self.assertEqual(result["status"], "completed", result.get("incomplete_reason"))
        self.assertNotIn("p0", [call["stage"] for call in worker.calls])
        self.assertEqual(result["stages"]["p0"]["status"], "restored")
        self.assertEqual(result["evaluation_kind"], "recovered_attempt_not_fresh_pass_at_1")
        self.assertEqual(json.loads((config.output_dir / "recovery-origin.json").read_text()), json.loads(manifest.read_text()))
        self.assertEqual(result["recovery_origin"]["package_sha256"], digest)
        self.assertIsNone(result["resolved"])

    async def test_failed_repair_commit_is_saved_and_p0_really_restored_before_pact(self):
        (self.workspace / "src").mkdir()
        (self.workspace / "tests").mkdir()
        (self.workspace / "src/api.py").write_text("def value():\n    return 1\n")
        (self.workspace / "tests/test_api.py").write_text("def test_value():\n    assert True\n")
        (self.workspace / ".gitignore").write_text(".pact/\n.pact-io/\n__pycache__/\n")
        self.git("add", ".")
        self.git("commit", "-qm", "public API fixture")
        self.prompt.write_text("Change the public value() API to return integer 2.")
        result = await SuiteController(self.config, PipelineWorker(fail_repair=True)).run()
        self.assertEqual(result["status"], "completed", result.get("incomplete_reason"))
        self.assertEqual(result["baseline"], "p0")
        self.assertEqual(result["stages"]["repair"]["status"], "failed")
        self.assertTrue(result["stages"]["repair"]["baseline_restored"])
        fallback = json.loads((self.config.output_dir / "repair-fallback.json").read_text())
        self.assertTrue(fallback["head_restored"])
        self.assertTrue(fallback["index_restored"])
        p0_patch = (self.config.output_dir / "recovery/p0/candidate.patch").read_bytes()
        self.assertEqual(hashlib.sha256(p0_patch).hexdigest(), fallback["patch_sha256"])
        self.assertIn("return 3", (self.config.output_dir / "recovery/failed-repair/candidate.patch").read_text())
        self.assertEqual((self.workspace / "src/api.py").read_text(), "def value():\n    return 2\n")

    def test_recovery_preserves_real_index_and_includes_new_files(self):
        (self.workspace / "new.py").write_text("new content\n")
        (self.workspace / "source.py").write_text("staged content\n")
        self.git("add", "source.py")
        before = self.git("diff", "--cached", "--binary")
        base = self.git("rev-parse", "HEAD").decode().strip()
        receipt = capture_recovery(self.workspace, self.root / "recovery", base)
        self.assertEqual(receipt["status"], "captured")
        self.assertEqual(self.git("diff", "--cached", "--binary"), before)
        patch = (self.root / "recovery/candidate.patch").read_text()
        self.assertIn("new.py", patch)
        self.assertIn("staged content", patch)


if __name__ == "__main__":
    unittest.main()
