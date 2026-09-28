"""Offline migration checks: native evidence, frozen S0, and budget accounting."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwenswarm.long_horizon.clarification import (
    JiuwenClarificationModel, SideCallBudgetExhausted, SideCallInterrupted, SideCallIntegrityError, WorkspaceSnapshotter,
    capture_workspace, run_clarification, save_pact_inputs, snapshot_workspace,
)
from jiuwenswarm.long_horizon.clarification.event_evidence import (
    _observed_lines, investigation_tool_calls, verified_evidence,
)
from jiuwenswarm.long_horizon.clarification.types import (
    ClarificationProposal, PactAcceptanceCriterion, PactGoalContract, PactInitialPlan, PactRuntimeInput,
)
from jiuwenswarm.long_horizon.clarification.verification import PublicVerification, json_digest


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


@pytest.fixture
def frozen(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Migration Test")
    git(repo, "config", "user.email", "test@localhost")
    git(repo, "config", "maintenance.auto", "false")
    (repo / "src").mkdir()
    (repo / "tests").mkdir()
    (repo / "src/api.py").write_text("def value():\n    return 1\n")
    (repo / "tests/test_api.py").write_text("def test_value():\n    assert True\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    snapshot = capture_workspace(repo, tmp_path / "s0")
    return repo, snapshot


def event_pair(call_id: str, name: str, arguments: dict, result: object, *, success=True):
    shared = {"schema_version": "0.1", "type": "event", "request_id": "r1", "session_id": "session-1"}
    call = {**shared, "sequence": 1, "event_type": "chat.tool_call", "payload": {
        "tool_call": {"tool_call_id": call_id, "name": name, "arguments": arguments},
    }}
    outcome = {"tool_call_id": call_id, "tool_name": name, "result": result}
    if success is not None:
        outcome["success"] = success
    return [call, {**shared, "sequence": 2, "event_type": "chat.tool_result", "payload": {"tool_result": outcome}}]


def native_evidence(root: Path, suffix: str = ""):
    return [
        *event_pair("grep" + suffix, "grep", {"pattern": "value", "path": "."}, "src/api.py:1:def value():"),
        *event_pair("read" + suffix, "read_file", {"file_path": "src/api.py"}, (root / "src/api.py").read_text()),
        *event_pair("test" + suffix, "read_file", {"file_path": "tests/test_api.py"}, (root / "tests/test_api.py").read_text()),
    ]


def proposal_payload():
    anchor = {"kind": "current_repo", "anchor": "src/api.py:1-2"}
    adjacent = {"kind": "current_repo", "anchor": "tests/test_api.py:1-2"}
    return {
        "verdict": "clarification_needed", "rationale": "The API defines the return contract and its tests define compatibility.",
        "coverage": [
            {"target": "primary", "status": "found", "summary": "The owner implements the value API.", "evidence": [anchor]},
            {"target": "adjacent", "status": "found", "summary": "The public test is a compatibility surface.", "evidence": [adjacent]},
        ],
        "evidence": [anchor, adjacent],
        "guidance_points": [{"category": "behavior", "statement": "Preserve the existing integer return contract.",
            "confidence": 0.95, "basis": "current_repo", "contract_cell": "values_defaults",
            "decision_impact": "Callers retain the existing return type.", "evidence": [anchor],
            "risk": "Changing the return type would break existing callers."}],
    }


class FixtureWorker:
    def __init__(self, *, events=True, status="completed"):
        self.calls = []
        self.events = events
        self.status = status

    async def run(self, *, stage, prompt, workspace, output_dir, read_only=False, schema=None,
                  timeout_seconds=None, session_id=None):
        self.calls.append(dict(stage=stage, workspace=workspace, read_only=read_only,
                               schema=schema, output_dir=output_dir, session_id=session_id))
        if stage == "clarification-selector":
            payload = {"reviews": [
                {"candidate_id": f"p{i}-g1", "decision": "select" if i == 1 else "reject",
                 "rationale": "The first owner guidance is sufficient; the others duplicate it."}
                for i in range(1, 4)
            ]}
        else:
            payload = proposal_payload()
        return SimpleNamespace(final_text=json.dumps(payload), events=native_evidence(workspace, str(len(self.calls))) if self.events else [],
            session_id=session_id or f"native-{len(self.calls)}", usage={"input_tokens": 10, "output_tokens": 5},
            status=self.status, exit_code=0)


def test_s0_contains_dirty_staged_untracked_and_does_not_follow_live_link(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@localhost")
    (repo / "a.py").write_text("base\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    (repo / "a.py").write_text("staged\n")
    git(repo, "add", "a.py")
    (repo / "a.py").write_text("dirty\n")
    (repo / "new.py").write_text("new\n")
    (repo / "alias.py").symlink_to(repo / "a.py")
    snapshot = capture_workspace(repo, tmp_path / "s0")
    view = snapshot_workspace(snapshot)
    (repo / "a.py").write_text("p0 edit\n")
    assert (view / "a.py").read_text() == "dirty\n"
    assert (view / "new.py").read_text() == "new\n"
    assert (view / "alias.py").read_text() == "dirty\n"
    assert (view / "alias.py").resolve().is_relative_to(view)


def test_native_proposal_requires_actual_read_search_and_keeps_frozen_cwd(frozen, tmp_path):
    repo, snapshot = frozen
    (repo / "src/api.py").write_text("P0 replacement must not enter clarification\n")
    worker = FixtureWorker()
    model = JiuwenClarificationModel(worker=worker, snapshot=snapshot, output_dir=tmp_path / "model")
    result = asyncio.run(model.propose("Change value safely", sample_index=1))
    assert result.proposal is not None
    assert result.investigation.status == "completed"
    assert len(result.investigation.verified_evidence) == 2
    assert worker.calls[0]["workspace"] == snapshot_workspace(snapshot)
    assert worker.calls[0]["read_only"] is True
    assert worker.calls[0]["schema"]["properties"]["verdict"]


def test_schema_valid_proposal_without_native_evidence_is_rejected(frozen, tmp_path):
    _, snapshot = frozen
    worker = FixtureWorker(events=False)
    model = JiuwenClarificationModel(worker=worker, snapshot=snapshot, output_dir=tmp_path / "model")
    result = asyncio.run(model.propose("Change value safely", sample_index=1))
    assert result.proposal is None
    assert result.investigation.status == "failed"
    assert "successful grep" in result.error
    assert len(worker.calls) == 1
    assert worker.calls[0]["session_id"] is None


def test_unknown_or_failed_tool_result_and_forged_file_bytes_do_not_count(frozen):
    _, snapshot = frozen
    events = [
        *event_pair("unknown", "read_file", {"file_path": "src/api.py"}, "def value():\n    return 1\n", success=None),
        *event_pair("failed", "grep", {"pattern": "value"}, "src/api.py", success=False),
        *event_pair("forged", "read_file", {"file_path": "src/api.py"}, "This is an invented read result.", success=True),
        *event_pair("escape", "read_file", {"file_path": "/etc/passwd"}, "root", success=True),
    ]
    calls = investigation_tool_calls(events, snapshot)
    assert len(calls) == 4
    assert not any(call.successful for call in calls)


def test_citation_outside_observed_read_lines_is_rejected(frozen):
    _, snapshot = frozen
    calls = investigation_tool_calls([
        *event_pair("read", "read_file", {"file_path": "src/api.py"}, "def value():"),
        *event_pair("test", "read_file", {"file_path": "tests/test_api.py"}, "def test_value():\n    assert True\n"),
    ], snapshot)
    references, errors = verified_evidence(ClarificationProposal.model_validate(proposal_payload()), calls)
    assert errors and "line range" in errors[0]
    assert [item.path for item in references] == ["tests/test_api.py"]


def test_native_numbered_read_preserves_source_indentation(frozen):
    _, snapshot = frozen
    source = "def value():\n    return 1\n"
    native_rendering = "     1\tdef value():\n     2\t    return 1"
    assert _observed_lines(source, native_rendering) == (1, 2)
    events = event_pair("native", "read_file", {"file_path": "src/api.py"},
        "success=True data={'content': 'native tool model representation'} error=None")
    events[-1]["payload"]["tool_result"]["rendered_result"] = native_rendering
    calls = investigation_tool_calls(events, snapshot)
    assert calls[0].successful is True
    assert calls[0].line_range == (1, 2)


def test_numbered_read_does_not_accept_changed_indent_or_unread_gaps():
    source = "def first():\n    return 1\n\ndef second():\n    return 2\n"
    wrong_indent = "     1\tdef first():\n     2\t   return 1"
    assert _observed_lines(source, wrong_indent) == (1, 1)
    disjoint = "     1\tdef first():\n     5\t    return 2"
    assert _observed_lines(source, disjoint) == (1, 1)
    assert _observed_lines(source, "     2:     return 1") == (2, 2)


def test_native_numbered_read_binds_repeated_content_to_exact_line():
    source = "header\n    return value\nseparator\nfooter\n    return value\n"
    assert _observed_lines(source, "     5\t    return value") == (5, 5)
    assert _observed_lines(source, "     4\t    return value") is None
    assert _observed_lines(source, "     0\t    return value") is None
    assert _observed_lines(source, "     6\t    return value") is None
    # A raw duplicate elsewhere in the event cannot undo a failed native claim.
    assert _observed_lines(source, "     4\t    return value\n    return value") is None
    assert _observed_lines(source, "     5:     return value") == (5, 5)
    # Retain legacy unnumbered matching only for wholly unnumbered output.
    assert _observed_lines(source, "    return value") == (2, 2)


def test_budget_exhaustion_does_not_enter_evidence_retry(frozen, tmp_path):
    _, snapshot = frozen
    worker = FixtureWorker(status="budget_exhausted")
    model = JiuwenClarificationModel(worker=worker, snapshot=snapshot, output_dir=tmp_path / "model")
    with pytest.raises(SideCallBudgetExhausted):
        asyncio.run(model.propose("Change value", sample_index=1))
    assert len(worker.calls) == 1


def test_native_interruption_escapes_three_proposer_service(frozen, tmp_path):
    _, snapshot = frozen
    worker = FixtureWorker(status="cancelled")
    with pytest.raises(SideCallInterrupted):
        asyncio.run(run_clarification(worker, snapshot, "Change value", tmp_path / "calls"))
    assert all(call["stage"].startswith("clarification-proposal-") for call in worker.calls)


def test_three_proposers_selector_and_private_artifacts_use_native_worker(frozen, tmp_path):
    _, snapshot = frozen
    worker = FixtureWorker()
    result = asyncio.run(run_clarification(worker, snapshot, "Change value safely", tmp_path / "clarification"))
    assert result.status == "completed"
    assert len(result.proposals) == 3
    assert result.delta
    assert {call["stage"] for call in worker.calls} == {
        "clarification-proposal-1", "clarification-proposal-2", "clarification-proposal-3", "clarification-selector",
    }
    artifacts = tmp_path / "clarification/requirement_clarification/turn_1"
    assert (artifacts / "01-input/requirement.md").is_file()
    assert list((artifacts / "03-clarification").rglob("*.json"))


def test_bound_public_inputs_are_hash_checked_and_never_overwritten(tmp_path):
    goal = PactGoalContract.model_validate({"schema": "pact-runtime/goal-contract/v1", "goal": "Keep value behavior",
        "acceptance_criteria": [{"id": "ac1", "text": "The value API returns an integer."}], "non_goals": []})
    plan = PactInitialPlan.model_validate({"schema": "pact-runtime/initial-plan/v1", "constraints": [], "missions": [
        {"id": "m1", "objective": "Keep value behavior", "target_ac_ids": ["ac1"], "dependencies": [],
         "verification_intent": "Run the public API test."},
    ]})
    verification = PublicVerification.model_validate({"schema": "jiuwenswarm/pact-public-verification/v1", "status": "ready",
        "reason": "The checked-in test exercises the API.", "checks": [
            {"id": "check1", "command": "python -m pytest tests/test_api.py", "target_ac_ids": ["ac1"], "rationale": "Exercises value behavior."},
        ]})
    data = PactRuntimeInput(goal_contract=goal, initial_plan=plan, public_verification=verification)
    paths = save_pact_inputs(data, tmp_path / "accepted")
    bound = json.loads(paths["public_verification_path"].read_text())
    assert bound["goal_contract_sha256"] == json_digest(json.loads(paths["contract_path"].read_text()))
    assert bound["initial_plan_sha256"] == json_digest(json.loads(paths["plan_path"].read_text()))
    assert bound["schema"] == "jiuwenswarm/pact-bound-public-verification/v1"
    with pytest.raises(FileExistsError):
        save_pact_inputs(data, tmp_path / "accepted")


def test_snapshot_blob_tamper_is_rejected_before_restore(frozen):
    repo, snapshot = frozen
    (repo / "src/api.py").write_text("retained candidate\n")
    # Target a file that restore consumes, not the independently archived Git
    # index (which file-only restore deliberately does not touch).
    target = Path(snapshot.artifact_root) / "blobs" / snapshot.roots[0].entries[0].content_hash
    target.write_bytes(b"tampered snapshot")
    with pytest.raises(OSError):
        WorkspaceSnapshotter().restore(snapshot)
    assert (repo / "src/api.py").read_text() == "retained candidate\n"


def test_worker_mutation_of_frozen_view_stops_clarification(frozen, tmp_path):
    _, snapshot = frozen

    class MutatingWorker(FixtureWorker):
        async def run(self, **kwargs):
            result = await super().run(**kwargs)
            (kwargs["workspace"] / "src/api.py").chmod(0o600)
            (kwargs["workspace"] / "src/api.py").write_text("mutated S0\n")
            return result

    model = JiuwenClarificationModel(worker=MutatingWorker(), snapshot=snapshot, output_dir=tmp_path / "model")
    with pytest.raises(SideCallIntegrityError):
        asyncio.run(model.propose("Change value safely", sample_index=1))


@pytest.mark.parametrize("fenced", [False, True])
def test_public_checks_receive_fresh_per_criterion_generation_and_review(frozen, tmp_path, fenced):
    _, snapshot = frozen

    class PublicWorker(FixtureWorker):
        async def run(self, **kwargs):
            result = await super().run(**kwargs)
            criterion = "ac1" if kwargs["stage"].endswith("ac1") else "ac2"
            result.final_text = json.dumps({"schema": "jiuwenswarm/pact-public-verification/v1", "status": "ready",
                "reason": "The frozen public test covers this API criterion.", "checks": [{"id": "check",
                    "command": "python -m pytest tests/test_api.py", "target_ac_ids": [criterion],
                    "rationale": "Check existing public behavior."}]})
            if fenced:
                result.final_text = "```json\n" + result.final_text + "\n```"
            return result

    worker = PublicWorker()
    model = JiuwenClarificationModel(worker=worker, snapshot=snapshot, output_dir=tmp_path / "model")
    criteria = (PactAcceptanceCriterion(id="ac1", text="Value remains an integer."),
                PactAcceptanceCriterion(id="ac2", text="Public API remains importable."))
    verification, usage = asyncio.run(model.generate_public_verification("Keep behavior", acceptance_criteria=criteria))
    assert [call["stage"] for call in worker.calls] == [
        "public-verification-ac1", "public-verification-review-ac1",
        "public-verification-ac2", "public-verification-review-ac2",
    ]
    assert all(call["session_id"] is None and call["read_only"] for call in worker.calls)
    assert {check.id for check in verification.checks} == {"ac1-check1", "ac2-check1"}
    assert usage["input_tokens"] == 40
    receipts = [json.loads(path.read_text()) for path in (tmp_path / "model").glob("accepted-*.json")]
    assert len(receipts) == 4
    assert all(record["structured_output"]["format"] == ("json_fence" if fenced else "json") for record in receipts)
    assert all(record["structured_output"]["payload_rewritten"] is False for record in receipts)


def test_public_verifier_cannot_name_uninspected_public_tests(frozen, tmp_path):
    _, snapshot = frozen

    class UninspectedWorker(FixtureWorker):
        async def run(self, **kwargs):
            result = await super().run(**kwargs)
            result.final_text = json.dumps({"schema": "jiuwenswarm/pact-public-verification/v1", "status": "ready",
                "reason": "Proposed test.", "checks": [{"id": "check", "command": "pytest tests/not_read.py",
                    "target_ac_ids": ["ac1"], "rationale": "Alleged validation."}]})
            return result

    worker = UninspectedWorker()
    model = JiuwenClarificationModel(worker=worker, snapshot=snapshot, output_dir=tmp_path / "model")
    with pytest.raises(ValueError, match="not inspected"):
        asyncio.run(model.generate_public_verification("Keep behavior",
            acceptance_criteria=(PactAcceptanceCriterion(id="ac1", text="Value is integer."),)))
    assert len(worker.calls) == 1
