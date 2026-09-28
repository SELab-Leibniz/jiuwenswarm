"""No-model tests of S0 confinement, tool evidence, and suspension semantics."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from jiuwenswarm.long_horizon.localization import LocalizationBudgetExhausted, LocalizationInterrupted, localize


def _location(path="src/logic.py", role="primary", quote="return value + 1"):
    return {"file_path": path, "role": role, "start_line": 1, "end_line": 2, "reason": "Implements the required increment.", "confidence": "high", "evidence": {"quote": quote}}


def _events(path="src/logic.py", *, success=True, read=True, search=True):
    calls = []
    if search:
        calls.append(("search-1", "grep", {"pattern": "increment", "path": "."}, f"{path}:1:def increment(value):"))
    if read:
        calls.append(("read-1", "read_file", {"file_path": path}, "1: def increment(value):\n2:     return value + 1"))
    events = []
    for call_id, name, arguments, output in calls:
        events.extend([
            {"type": "event", "event_type": "chat.tool_call", "sequence": len(events), "payload": {"event_type": "chat.tool_call", "tool_call": {"tool_call_id": call_id, "name": name, "arguments": json.dumps(arguments)}}},
            {"type": "event", "event_type": "chat.tool_result", "sequence": len(events) + 1, "payload": {"event_type": "chat.tool_result", "tool_result": {"tool_call_id": call_id, "tool_name": name, "result": output, "success": success}}},
        ])
    return events


class FakeWorker:
    def __init__(self, *, locations=None, events=None, status="completed", mutate=None, final_text=None):
        self.locations = [_location()] if locations is None else locations
        self.events = _events() if events is None else events
        self.status = status
        self.mutate = mutate
        self.final_text = final_text
        self.calls = []

    async def run(self, **kwargs):
        self.calls.append(kwargs)
        if self.mutate:
            self.mutate(kwargs)
        text = self.final_text if self.final_text is not None else json.dumps({"summary": "Inspected increment entry point.", "locations": self.locations})
        return SimpleNamespace(final_text=text, events=self.events, session_id="native-session-1", usage={"output_tokens": 11}, status=self.status, exit_code=0)


class LocalizationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "s0"
        self.live = self.base / "live"
        self.root.mkdir()
        self.live.mkdir()
        self._add_file("src/logic.py", "def increment(value):\n    return value + 1\n")
        self._add_file("tests/test_logic.py", "def increment(value):\n    return value + 1\n")
        entries = tuple(SimpleNamespace(relative_path=path.relative_to(self.root).as_posix(), kind="file", model_visible=True, content_hash=hashlib.sha256(path.read_bytes()).hexdigest()) for path in self.root.rglob("*") if path.is_file())
        self.snapshot = SimpleNamespace(snapshot_id="s0", manifest_hash="manifest-id", roots=(SimpleNamespace(source_root=str(self.live), view_root=str(self.root), is_primary=True, entries=entries),))

    def _add_file(self, relative, text):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    async def _run(self, worker, **kwargs):
        return await localize(worker, self.snapshot, "Increment the input.", self.base / "out", 30, **kwargs)

    async def test_success_requires_search_and_read_and_records_provenance(self):
        worker = FakeWorker()
        result = await self._run(worker)
        self.assertEqual("completed", result["status"])
        self.assertEqual("src/logic.py", result["primary"][0]["path"])
        self.assertEqual(["read-1"], result["locations"][0]["evidence"]["read_call_ids"])
        self.assertEqual(["search-1"], result["locations"][0]["evidence"]["search_call_ids"])
        self.assertEqual("disabled", result["memory"])
        self.assertEqual("disabled", result["semantic_index"])
        self.assertTrue(worker.calls[0]["read_only"])
        self.assertEqual(self.root, worker.calls[0]["workspace"])
        self.assertTrue((self.base / "out/localization.json").is_file())

    async def test_real_v2_shape_accepts_unique_fence_and_preserves_original_hash(self):
        payload = json.dumps({"locations": [_location()], "summary": "All workspace files inspected."}, indent=2)
        text = "  All workspace files inspected. The repository is a minimal fixture: `src/logic.py`.\r\n\r\n```json\r\n" + payload.replace("\n", "\r\n") + "\r\n```\r\n "
        worker = FakeWorker(final_text=text)
        result = await self._run(worker)
        self.assertEqual("completed", result["status"], result["warnings"])
        self.assertEqual("json_fence_with_prose", result["response_format"])
        self.assertTrue(result["response_wrapper_ignored"])
        self.assertEqual(hashlib.sha256(text.encode()).hexdigest(), result["response_sha256"])
        self.assertEqual(text, worker.final_text)

    async def test_plain_object_with_literal_backticks_remains_plain_json(self):
        text = json.dumps({"locations": [_location()], "summary": "Literal ``` is ordinary JSON string content."})
        result = await self._run(FakeWorker(final_text=text))
        self.assertEqual("completed", result["status"])
        self.assertEqual("json", result["response_format"])

    async def test_whole_response_json_fence_remains_supported(self):
        payload = json.dumps({"locations": [_location()], "summary": "Inspected."})
        result = await self._run(FakeWorker(final_text=f"```json\n{payload}\n```"))
        self.assertEqual("completed", result["status"])
        self.assertEqual("json_fence", result["response_format"])
        self.assertFalse(result["response_wrapper_ignored"])

    async def test_ambiguous_malformed_and_conflicting_fences_are_rejected(self):
        payload = json.dumps({"locations": [_location()], "summary": "Inspected."})
        fence = f"```json\n{payload}\n```"
        invalid = [
            fence + "\n" + fence,
            fence + "\n```json\n",
            fence + "\n```python\npass\n```",
            "```json\n" + payload,
            "```json\n" + payload + "\n``` trailing garbage",
            "```json\n{broken}\n```",
            fence + '\nAlternative: {"locations": [], "summary": "Other"}',
            fence + "\nCorrection: locations should be []",
            fence + '\nsummary: "Different result"',
            "Unfenced prose " + payload,
        ]
        for text in invalid:
            with self.subTest(text=text):
                result = await self._run(FakeWorker(final_text=text))
                self.assertEqual("degraded", result["status"])
                self.assertEqual([], result["locations"])
                self.assertEqual("invalid", result["response_format"])

    async def test_duplicate_keys_and_nonfinite_numbers_are_rejected(self):
        payload = json.dumps({"locations": [_location()], "summary": "Inspected."})
        invalid = [
            payload.replace('"summary": "Inspected."', '"summary": "A", "summary": "B"'),
            payload.replace('"role": "primary"', '"role": "validation", "role": "primary"'),
            payload.replace('"quote": "return value + 1"', '"quote": "wrong", "quote": "return value + 1"'),
            *(payload.replace('"start_line": 1', '"start_line": ' + number) for number in ("NaN", "Infinity", "-Infinity", "1e999")),
        ]
        for text in invalid:
            for wrapped in (text, f"Inspection complete.\n```json\n{text}\n```"):
                with self.subTest(text=wrapped):
                    result = await self._run(FakeWorker(final_text=wrapped))
                    self.assertEqual("degraded", result["status"])
                    self.assertEqual([], result["locations"])

    async def test_fenced_format_does_not_relax_schema_or_tool_evidence(self):
        valid = {"locations": [_location()], "summary": "Inspected."}
        for payload in ({**valid, "extra": True}, {**valid, "locations": [{**_location(), "extra": True}]},
                        {**valid, "locations": [{**_location(), "evidence": {"quote": "return value + 1", "extra": True}}]},
                        {**valid, "locations": [{**_location(), "confidence": ["high"]}]}):
            text = "Inspection complete.\n```json\n" + json.dumps(payload) + "\n```"
            result = await self._run(FakeWorker(final_text=text))
            self.assertEqual("degraded", result["status"])
            self.assertEqual([], result["locations"])
        text = "Inspection complete.\n```json\n" + json.dumps(valid) + "\n```"
        result = await self._run(FakeWorker(final_text=text, events=[]))
        self.assertEqual("degraded", result["status"])
        self.assertEqual([], result["locations"])

    async def test_every_role_has_evidence_and_paths_are_relative(self):
        locations = [_location(), _location(role="propagation"), _location("tests/test_logic.py", "validation")]
        extra_events = _events("tests/test_logic.py")
        for event in extra_events:
            kind = "tool_call" if event["event_type"] == "chat.tool_call" else "tool_result"
            event["payload"][kind]["tool_call_id"] += "-extra"
        events = _events() + extra_events
        result = await self._run(FakeWorker(locations=locations, events=events))
        self.assertEqual("completed", result["status"])
        for role in ("primary", "propagation", "validation"):
            self.assertEqual(1, len(result[role]))

    async def test_model_claim_without_tools_is_degraded_with_no_guess(self):
        result = await self._run(FakeWorker(events=[]))
        self.assertEqual("degraded", result["status"])
        self.assertEqual([], result["locations"])

    async def test_read_only_or_search_only_is_insufficient(self):
        for events in (_events(search=False), _events(read=False)):
            result = await self._run(FakeWorker(events=events))
            self.assertEqual([], result["locations"])

    async def test_failed_or_unpaired_tool_result_does_not_prove_read(self):
        for events in (_events(success=False), _events()[1::2]):
            result = await self._run(FakeWorker(events=events))
            self.assertEqual([], result["locations"])

    async def test_missing_explicit_tool_success_is_not_assumed(self):
        events = _events()
        for event in events:
            event["payload"].get("tool_result", {}).pop("success", None)
        result = await self._run(FakeWorker(events=events))
        self.assertEqual([], result["locations"])

    async def test_native_tooloutput_repr_uses_paired_rendered_source_lines(self):
        events = _events()
        search = events[1]["payload"]["tool_result"]
        search["result"] = "success=True data={'stdout': 'src/logic.py', 'exit_code': 0} error=None"
        search["rendered_result"] = "src/logic.py"
        read = events[3]["payload"]["tool_result"]
        rendered = "     1\tdef increment(value):\n     2\t    return value + 1"
        read["result"] = f"success=True data={{'content': {rendered!r}, 'line_count': 2}} error=None"
        read["rendered_result"] = rendered
        result = await self._run(FakeWorker(events=events))
        self.assertEqual("completed", result["status"], result["warnings"])
        self.assertEqual(["read-1"], result["locations"][0]["evidence"]["read_call_ids"])

    async def test_native_rendering_does_not_turn_denied_read_into_evidence(self):
        events = _events()
        read = events[3]["payload"]["tool_result"]
        read["result"] = "[PERMISSION_DENIED] Denied by rule: tools.read_file"
        read["rendered_result"] = "     1\tdef increment(value):\n     2\t    return value + 1"
        read["success"] = False
        result = await self._run(FakeWorker(events=events))
        self.assertEqual([], result["locations"])

    async def test_native_targeted_read_binds_repeated_source_to_actual_line_number(self):
        source = "def first():\n    return value + 1\n\ndef second():\n    return value + 1\n"
        (self.root / "src/logic.py").write_text(source)
        next(entry for entry in self.snapshot.roots[0].entries if entry.relative_path == "src/logic.py").content_hash = hashlib.sha256(source.encode()).hexdigest()
        events = _events()
        events[2]["payload"]["tool_call"]["arguments"] = json.dumps({"file_path": "src/logic.py", "offset": 5, "limit": 1})
        read = events[3]["payload"]["tool_result"]
        read["result"] = "success=True data={'content': '     5\\t    return value + 1'} error=None"
        read["rendered_result"] = "     5\t    return value + 1"
        actual = {**_location(), "start_line": 5, "end_line": 5}
        result = await self._run(FakeWorker(locations=[actual], events=events))
        self.assertEqual("completed", result["status"], result["warnings"])
        earlier_duplicate = {**actual, "start_line": 2, "end_line": 2}
        result = await self._run(FakeWorker(locations=[earlier_duplicate], events=events))
        self.assertEqual([], result["locations"])

    async def test_native_mismatched_line_number_does_not_prove_repeated_source(self):
        source = "def first():\n    return value + 1\n\ndef second():\n    return value + 1\n"
        (self.root / "src/logic.py").write_text(source)
        next(entry for entry in self.snapshot.roots[0].entries if entry.relative_path == "src/logic.py").content_hash = hashlib.sha256(source.encode()).hexdigest()
        events = _events()
        read = events[3]["payload"]["tool_result"]
        read["result"] = "success=True data={'content': '     4\\t    return value + 1'} error=None"
        read["rendered_result"] = "     4\t    return value + 1"
        candidate = {**_location(), "start_line": 2, "end_line": 2}
        result = await self._run(FakeWorker(locations=[candidate], events=events))
        self.assertEqual([], result["locations"])

    async def test_outside_snapshot_and_unseen_files_are_rejected(self):
        for path in ("../live/gold.py", "/etc/passwd", "missing.py"):
            result = await self._run(FakeWorker(locations=[_location(path)], events=_events(path)))
            self.assertEqual([], result["locations"])

    async def test_symlink_paths_are_rejected_even_when_target_is_inside_s0(self):
        (self.root / "alias.py").symlink_to(self.root / "src/logic.py")
        result = await self._run(FakeWorker(locations=[_location("alias.py")], events=_events("alias.py")))
        self.assertEqual([], result["locations"])

    async def test_source_quote_and_range_must_match_observed_s0(self):
        bad_quote = _location(quote="return value + 999")
        bad_range = {**_location(), "end_line": 900}
        for candidate in (bad_quote, bad_range):
            result = await self._run(FakeWorker(locations=[candidate]))
            self.assertEqual([], result["locations"])

    async def test_live_p0_changes_cannot_affect_localization(self):
        (self.live / "model.patch").write_text("secret P0 patch")
        worker = FakeWorker()
        result = await self._run(worker)
        self.assertEqual("completed", result["status"])
        self.assertNotIn(str(self.live), worker.calls[0]["prompt"])
        self.assertNotIn("secret P0 patch", worker.calls[0]["prompt"])

    async def test_mutation_before_worker_blocks_and_mutation_after_invalidates(self):
        original = (self.root / "src/logic.py").read_text()
        (self.root / "src/logic.py").write_text("changed")
        worker = FakeWorker()
        result = await self._run(worker)
        self.assertEqual([], worker.calls)
        self.assertEqual("degraded", result["status"])
        (self.root / "src/logic.py").write_text(original)
        worker = FakeWorker(mutate=lambda args: (args["workspace"] / "src/logic.py").write_text("changed again"))
        result = await self._run(worker)
        self.assertEqual("degraded", result["status"])
        self.assertEqual([], result["locations"])

    async def test_budget_stop_preserves_session_and_is_never_degraded(self):
        with self.assertRaises(LocalizationBudgetExhausted):
            await self._run(FakeWorker(status="budget_exhausted"))
        receipt = json.loads((self.base / "out/localization.json").read_text())
        self.assertEqual("budget_exhausted", receipt["status"])
        self.assertEqual("native-session-1", receipt["session_id"])
        self.assertEqual({"output_tokens": 11}, receipt["usage"])

    async def test_thrown_budget_error_is_not_swallowed(self):
        class WorkerBudgetExhaustedError(RuntimeError):
            pass

        class Worker:
            async def run(self, **kwargs):
                error = WorkerBudgetExhaustedError("stop")
                error.result = SimpleNamespace(status="budget_exhausted", session_id="saved", usage={})
                raise error

        with self.assertRaises(WorkerBudgetExhaustedError):
            await self._run(Worker())

    async def test_stage_deadline_is_a_budget_stop(self):
        with self.assertRaises(LocalizationBudgetExhausted):
            await self._run(FakeWorker(status="timed_out"))
        receipt = json.loads((self.base / "out/localization.json").read_text())
        self.assertEqual("budget_exhausted", receipt["status"])
        self.assertEqual("timed_out", receipt["worker_status"])

    async def test_interruption_is_never_degraded(self):
        with self.assertRaises(LocalizationInterrupted):
            await self._run(FakeWorker(status="cancelled"))
        receipt = json.loads((self.base / "out/localization.json").read_text())
        self.assertEqual("interrupted", receipt["status"])

    async def test_search_outside_s0_cannot_prove_discovery(self):
        events = _events()
        events[0]["payload"]["tool_call"]["arguments"] = json.dumps({"pattern": "increment", "path": str(self.live)})
        result = await self._run(FakeWorker(events=events))
        self.assertEqual([], result["locations"])

    async def test_duplicate_call_ids_cannot_prove_evidence(self):
        result = await self._run(FakeWorker(events=_events() + _events()))
        self.assertEqual([], result["locations"])

    async def test_read_quote_does_not_prove_unobserved_line_range(self):
        events = _events()
        events[3]["payload"]["tool_result"]["result"] = "2:     return value + 1"
        result = await self._run(FakeWorker(events=events))
        self.assertEqual([], result["locations"])

    async def test_external_hints_never_substitute_for_native_evidence(self):
        class Provider:
            async def candidates(self, **kwargs):
                return {"snapshot_id": kwargs["snapshot_id"], "release_id": "pinned-external-release", "locations": [{"file_path": "src/logic.py"}]}

        result = await self._run(FakeWorker(events=[]), contextgraph=Provider())
        self.assertEqual("degraded", result["status"])
        self.assertEqual([], result["locations"])
        self.assertEqual("external-hints", result["contextgraph"]["mode"])

    async def test_artifacts_cannot_be_written_in_s0(self):
        with self.assertRaises(ValueError):
            await localize(FakeWorker(), self.snapshot, "Requirement", self.root / "output", 30)


if __name__ == "__main__":
    unittest.main()
