# Copyright (c) 2026 Chrys. All rights reserved.

"""Event-driven controls for independent per-criterion native review lanes.

The worker is scripted. The scheduler, Jiuwen model, public criterion store,
transport receipts, checkpoints and the plugin factory in this file are real.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

pytest.importorskip(
    "jiuwenswarm",
    reason="native Jiuwen tests require the reviewed Jiuwen source on PYTHONPATH",
)

from jiuwenswarm.long_horizon.clarification import types
from jiuwenswarm.long_horizon.clarification.budget import SideCallBudgetExhausted, SideCallTicket
from jiuwenswarm.long_horizon.clarification.public_checkpoint import PublicCriterionStore
from jiuwenswarm.long_horizon.clarification.service import validate_pact_runtime_input
from jiuwenswarm.long_horizon.clarification.verification import PublicVerification

from jiuwenswarm.lh.planning_checkpoint import PlanningCheckpoint, PlanningCheckpointError
from jiuwenswarm.lh.plugins.native_checkpoint import NativeStageRunner
from jiuwenswarm.lh.public_review import public_review_failure, recoverable_public_review_error
from jiuwenswarm.lh.side_calls import SideCallIntegrityError, view_identity
from jiuwenswarm.lh.suite import incomplete_public_review


class Exhausted(RuntimeError):
    pass


def write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(value))
    path.chmod(0o600)


def snapshot_workspace_files(repo: Path) -> None:
    (repo / "src/api.py").write_text("def value():\n    return 1\n")
    (repo / "tests").mkdir()
    (repo / "tests/test_api.py").write_text("def test_value():\n    assert True\n")


class SharedAdmission:
    """One admission object and deadline shared by every child runner."""

    def __init__(self, *, deadline: float, limit: int | None = None) -> None:
        self.deadline = deadline
        self.limit = limit
        self.admitted: list[dict[str, object]] = []
        self.finished: list[tuple[str, str]] = []
        self.refused: str | None = None

    def admit(self, stage: str) -> SideCallTicket:
        if self.limit is not None and len(self.admitted) >= self.limit:
            self.refused = "test_cap"
            raise SideCallBudgetExhausted("test admission cap")
        self.admitted.append({"stage": stage, "deadline": self.deadline})
        return SideCallTicket(str(len(self.admitted)), max(0.001, self.deadline - time.time()))

    def finish(self, ticket: SideCallTicket, *, status: str, detail: str = "") -> None:
        self.finished.append((ticket.token, status))


def criterion_prompt_id(prompt: str) -> str:
    match = re.search(r"Generate checks only for this acceptance criterion: (\{[^\n]+\})", prompt)
    if match is None:
        match = re.search(r"acceptance criterion: (\{[^\n]+\})", prompt)
    assert match is not None, prompt[:500]
    return json.loads(match.group(1))["id"]


class ScriptedPublicWorker:
    """Real public stage receipts, deterministic text; gated per criterion."""

    def __init__(
        self, *, block: set[str] | None = None, binary: Path | None = None, config_template: Path | None = None
    ) -> None:
        self.source_commit = "scripted-public-criterion-lanes"
        self.binary = Path(binary) if binary is not None else None
        self.config_template = Path(config_template) if config_template is not None else None
        self.calls: list[dict[str, object]] = []
        self.active = 0
        self.peak = 0
        self.block = set(block or ())
        self.release = {f"ac{i}": asyncio.Event() for i in range(1, 9)}
        self.started = {name: asyncio.Event() for name in self.release}
        self.reviewed = {name: asyncio.Event() for name in self.release}
        self.blocked: set[str] = set()
        self.released: set[str] = set()

    async def run(self, **kwargs):
        stage = kwargs["stage"]
        criterion = criterion_prompt_id(kwargs["prompt"])
        self.calls.append({"stage": stage, "criterion": criterion, "timeout": kwargs["timeout_seconds"]})
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.started[criterion].set()
        try:
            if criterion in self.block and criterion not in self.released:
                self.blocked.add(criterion)
                await self.release[criterion].wait()
                self.released.add(criterion)
            session = "scripted-session-" + uuid4().hex
            events = []
            if kwargs.get("tool_policy") != "none":
                for number, (name, arguments, result) in enumerate(
                    [
                        ("grep", {"pattern": "value", "path": "."}, "src/api.py:1:def value():"),
                        ("read_file", {"file_path": "src/api.py"}, "def value():\n    return 1\n"),
                        (
                            "read_file",
                            {"file_path": "tests/test_api.py"},
                            "def test_value():\n    assert True\n",
                        ),
                    ]
                ):
                    shared = {"schema_version": "0.1", "type": "event", "request_id": session, "session_id": session}
                    call_id = session + "-" + str(number)
                    events.extend(
                        [
                            {
                                **shared,
                                "sequence": number * 2,
                                "event_type": "chat.tool_call",
                                "payload": {
                                    "tool_call": {"tool_call_id": call_id, "name": name, "arguments": arguments}
                                },
                            },
                            {
                                **shared,
                                "sequence": number * 2 + 1,
                                "event_type": "chat.tool_result",
                                "payload": {
                                    "tool_result": {
                                        "tool_call_id": call_id,
                                        "tool_name": name,
                                        "result": result,
                                        "success": True,
                                    }
                                },
                            },
                        ]
                    )
                text = "The public source and public test were inspected."
            else:
                text = json.dumps(
                    {
                        "schema": "jiuwenswarm/pact-public-verification/v1",
                        "status": "ready",
                        "reason": "Scripted worker boundary for real native validation",
                        "checks": [
                            {
                                "id": "native-original",
                                "command": "python3 -m pytest tests/test_api.py",
                                "target_ac_ids": [criterion],
                                "rationale": "Actual public source read occurred in this scripted transport",
                            }
                        ],
                    }
                )
                if stage.endswith("review-ac1-synthesis"):
                    self.reviewed[criterion].set()
            call = kwargs["output_dir"]
            write(
                call / "metadata.json",
                {
                    "stage": stage,
                    "status": "completed",
                    "session_id": session,
                    "read_only": kwargs["read_only"],
                    "tool_policy": kwargs.get("tool_policy", "configured"),
                },
            )
            write(call / "terminal-result.json", {"status": "completed", "session_id": session})
            return SimpleNamespace(
                final_text=text,
                events=events,
                session_id=session,
                usage={"input_tokens": 2},
                status="completed",
                exit_code=0,
            )
        finally:
            self.active -= 1


def scripted_worker(tmp_path: Path, *, block: set[str] | None = None) -> ScriptedPublicWorker:
    binary = tmp_path / "native-entry"
    binary.write_text("native launcher bytes\n")
    config_template = tmp_path / "config.yaml"
    config_template.write_text("model: scripted\n")
    return ScriptedPublicWorker(block=block, binary=binary, config_template=config_template)


class ParentRunner:
    def __init__(self, root: Path) -> None:
        self.output_dir = Path(root)
        self.runtime_evidence = ""


class LaneGate:
    """Prove every real scheduler lane started before any child outcome."""

    def __init__(self, expected: int) -> None:
        self.expected = expected
        self.started = 0
        self.active = 0
        self.peak = 0
        self.ready = asyncio.Event()
        self.release = asyncio.Event()

    def enter(self) -> None:
        self.started += 1
        self.active += 1
        self.peak = max(self.peak, self.active)
        if self.started >= self.expected:
            self.ready.set()

    def leave(self) -> None:
        self.active -= 1


class ArtifactRunner(ParentRunner):
    """Synthetic artifact writer used only for scheduler/error controls."""

    def __init__(
        self,
        root: Path,
        *,
        tag: str = "0001",
        failure: BaseException | None = None,
        gate: LaneGate | None = None,
        wait_after_release: asyncio.Event | None = None,
        hold_cancel: asyncio.Event | None = None,
        cancel_started: asyncio.Event | None = None,
    ) -> None:
        super().__init__(root)
        self.tag = tag
        self.failure = failure
        self.calls = 0
        self.expected_identity = None
        self.gate = gate
        self.wait_after_release = wait_after_release
        self.hold_cancel = hold_cancel
        self.cancel_started = cancel_started
        self.raised: list[BaseException] = []

    async def generate_public_verification(self, prompt: str, *, acceptance_criteria: tuple):
        (criterion,) = acceptance_criteria
        self.calls += 1
        if self.gate is not None:
            self.gate.enter()
        try:
            if self.gate is not None:
                await self.gate.release.wait()
            if self.wait_after_release is not None:
                await self.wait_after_release.wait()
            if self.failure is not None:
                self.raised.append(self.failure)
                raise self.failure
            value = PublicVerification(
                schema="jiuwenswarm/pact-public-verification/v1",
                status="ready",
                reason="Synthetic reviewed artifact",
                checks=[
                    {
                        "id": "native-original",
                        "command": "python3 -m pytest tests/test_api.py",
                        "target_ac_ids": [criterion.id],
                        "rationale": "Synthetic artifact for lane scheduling",
                    }
                ],
            )
            write_reviewed_files(
                self.output_dir,
                criterion,
                value,
                self.expected_identity(prompt, criterion),
                self.tag,
            )
            return value.model_copy(
                update={
                    "checks": [
                        check.model_copy(update={"id": f"ac1-check{n}"}) for n, check in enumerate(value.checks, 1)
                    ]
                }
            ), {"total_tokens": 3}
        except asyncio.CancelledError as cancelled:
            if self.cancel_started is not None:
                self.cancel_started.set()
            if self.hold_cancel is not None:
                while not self.hold_cancel.is_set():
                    try:
                        await self.hold_cancel.wait()
                    except asyncio.CancelledError:
                        continue
            raise cancelled
        finally:
            if self.gate is not None:
                self.gate.leave()


def write_reviewed_files(root: Path, criterion, value, identity, tag: str) -> None:
    provenance = {"independent_review": True}
    for role, stage in (("generation", "public-verification-ac1"), ("review", "public-verification-review-ac1")):
        turns = []
        for phase in ("investigation", "synthesis"):
            session = f"{tag}-{role}-{phase}"
            call = root / f"{tag}-{role}-{phase}"
            write(
                call / "metadata.json",
                {
                    "stage": stage + "-" + phase,
                    "status": "completed",
                    "session_id": session,
                    "read_only": True,
                    "tool_policy": "none" if phase == "synthesis" else "configured",
                },
            )
            write(call / "terminal-result.json", {"status": "completed", "session_id": session})
            turns.append(
                {"status": "completed", "session_id": session, "stage": stage + "-" + phase, "output_dir": str(call)}
            )
        receipt = {
            "stage": stage,
            "status": "completed",
            "synthesis_tool_policy": "none",
            "native_turns": turns,
            "investigation_session_id": turns[0]["session_id"],
            "synthesis_session_id": turns[1]["session_id"],
        }
        path = root / f"public-stage-{stage}-{tag}.json"
        write(path, receipt)
        provenance[role] = {
            "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "native_turns": turns,
            "investigation_session_id": turns[0]["session_id"],
            "synthesis_session_id": turns[1]["session_id"],
        }
        write(
            root / f"accepted-{stage}-{tag}.json",
            {
                "stage": stage,
                "session_id": turns[1]["session_id"],
                "public_stage_receipt": provenance[role],
                "value": value.model_dump(mode="json", by_alias=True),
            },
        )
    PublicCriterionStore(root / "public-verification-checkpoints", identity).save(criterion, value, provenance)


def runner_fixture(tmp_path: Path, factory, *, concurrency: int = 2) -> NativeStageRunner:
    snapshot = SimpleNamespace(roots=[], manifest_hash="a" * 64)
    context = SimpleNamespace(
        snapshot=snapshot,
        deadline=time.time() + 300,
        session_dir=tmp_path / "session",
        admission=None,
    )
    return NativeStageRunner(
        runner=ParentRunner(tmp_path / "parent"),
        checkpoint=PlanningCheckpoint(tmp_path / "session", {"work": "same"}),
        context=context,
        types=types,
        verification=PublicVerification,
        store_type=PublicCriterionStore,
        validate_pair=validate_pact_runtime_input,
        stopped=Exhausted,
        exhausted=Exhausted,
        abort=threading.Event(),
        snapshot_identity=view_identity(snapshot),
        expected_worker_identity={"fixture": "synthetic"},
        public_runner_factory=factory,
        public_concurrency=concurrency,
    )


def lane_cache(base: Path, children: list[ArtifactRunner], *, concurrency: int = 4) -> NativeStageRunner:
    pending = iter(children)
    cache = runner_fixture(base, lambda: next(pending), concurrency=concurrency)
    for child in children:
        child.expected_identity = cache.public_identity
    return cache


def criterion_set(count: int = 4) -> tuple:
    return tuple(types.PactAcceptanceCriterion(id=f"ac{i}", text=f"Criterion {i}") for i in range(1, count + 1))


def plugin_for(worker, workspace: Path):
    from jiuwenswarm.lh.plugins.jiuwen import JiuwenPlugin

    args = SimpleNamespace(workflow="suite", profile_name="Code")
    plugin = JiuwenPlugin(worker=worker)
    plugin._side_args = args
    plugin._side_context = {"abort": threading.Event()}
    plugin._side_binding = plugin.resolve_execution(workspace, args)
    return plugin


@pytest.fixture
def shipping_public_model(monkeypatch):
    """Use the shipping verifier and model even with an older external SDK checkout."""
    root = Path(__file__).parents[2] / "jiuwenswarm/long_horizon/clarification"
    for leaf in ("verification", "public_checkpoint", "model"):
        name = "jiuwenswarm.long_horizon.clarification." + leaf
        spec = importlib.util.spec_from_file_location(name, root / (leaf + ".py"))
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "unavailable_stage",
    [
        None,
        "public-verification-ac1-synthesis",
        "public-verification-review-ac1-synthesis",
    ],
)
@pytest.mark.asyncio
async def test_action_order_evidence_reaches_native_generation_and_review(
    tmp_path, git_repo_factory, monkeypatch, unavailable_stage, shipping_public_model
):
    from jiuwenswarm.long_horizon.clarification import capture_workspace, snapshot_workspace

    from jiuwenswarm.lh.plugins.native import PublicRepairCap

    repo = git_repo_factory(tmp_path / "public-repo")
    snapshot_workspace_files(repo)
    snapshot = capture_workspace(repo, tmp_path / "frozen")
    deadline = time.time() + 300
    admission = SharedAdmission(deadline=deadline)
    context = SimpleNamespace(
        snapshot=snapshot,
        deadline=deadline,
        session_dir=tmp_path / "session",
        admission=admission,
        session_id="public-action-order",
    )
    worker = scripted_worker(tmp_path)
    prompts = {}
    run = worker.run

    async def capture_prompt(**kwargs):
        prompts[kwargs["stage"]] = kwargs["prompt"]
        result = await run(**kwargs)
        if kwargs["stage"] == unavailable_stage:
            result.final_text = json.dumps(
                {
                    "schema": "jiuwenswarm/pact-public-verification/v1",
                    "status": "unavailable",
                    "reason": "No public activity log records both actions.",
                    "checks": [],
                }
            )
        return result

    monkeypatch.setattr(worker, "run", capture_prompt)
    plugin = plugin_for(worker, snapshot_workspace(snapshot))
    task = "Ask for clarification before resolving the request."
    criterion = types.PactAcceptanceCriterion(id="ac1", text=task)
    goal = types.PactGoalContract(
        schema="pact-runtime/goal-contract/v1", goal=task, acceptance_criteria=[criterion], non_goals=[]
    )
    plan = types.PactInitialPlan(
        schema="pact-runtime/initial-plan/v1",
        constraints=[],
        missions=[
            {
                "id": "m1",
                "objective": task,
                "target_ac_ids": ["ac1"],
                "dependencies": [],
                "verification_intent": "Check the sequence of actions.",
            }
        ],
    )
    runner = await plugin.planning_runner(context, task)
    review = plugin._bounded_public_verification(
        runner,
        context,
        PublicRepairCap(admission, plugin.artifacts.exhausted, attempts=3),
        SimpleNamespace(rendered=task),
        SimpleNamespace(delta=""),
        SimpleNamespace(goal_contract=goal, initial_plan=plan),
    )
    if unavailable_stage:
        with pytest.raises(plugin.artifacts.exhausted) as failure:
            await review
        progress = public_review_failure(failure.value)
        assert progress.reason == "public_evidence_unavailable"
        assert "No public activity log" in progress.detail
        assert progress.required_criteria == ("ac1",) and progress.completed_criteria == ()
        assert not list(context.session_dir.rglob("rejected-*.json"))
        assert not list(context.session_dir.rglob("public-verification-checkpoints/*/*.json"))
        (receipt,) = context.session_dir.rglob(
            "public-stage-" + unavailable_stage.removesuffix("-synthesis") + "-*.json"
        )
        assert json.loads(receipt.read_text())["status"] == "unavailable"
    else:
        assert (await review)["status"] == "ready"
    lanes = ("",) if unavailable_stage == "public-verification-ac1-synthesis" else ("", "review-")
    assert len(worker.calls) == 2 * len(lanes)
    assert set(prompts) == {
        f"public-verification-{lane}ac1-{phase}" for lane in lanes for phase in ("investigation", "synthesis")
    }
    for stage, prompt in prompts.items():
        assert "existing populated" in prompt and "only its payload removed" in prompt
        assert "neighboring records in the empty control" in prompt
        assert "only for the branch that explicitly requires it" in prompt
        assert "equivalent wording and any contract-permitted report" in prompt
        assert "underlying state alone does not prove the candidate reported it" in prompt
        assert "what the author asserts about the required" in prompt
        assert "A complete statement may cite a placeholder" in prompt
        if "-review-" in stage:
            assert "independently inspect the public example or schema" in prompt
            assert "what the submitted extractor returns for each control" in prompt
            assert "wrong subject or opposite state" in prompt
            assert "otherwise-correct report in the wrong place or form" in prompt
            assert "a valid statement citing placeholder wording" in prompt
            assert "leaving the quotation or example" in prompt
            assert "a blank field" in prompt and "complete statement about a different subject" in prompt
        assert "registered checks run in the detached candidate checkout" in prompt
        assert "CHRYS_PACT_SOURCE_WORKSPACE" not in prompt and "JIUWENSWARM_PACT_SOURCE_WORKSPACE" not in prompt
        assert "including zero comparisons" in prompt
        assert "preconditions for each named variant" in prompt
        assert "earlier actions" in prompt and "older reachable revisions" in prompt
        assert "A shared commit leaves their relative order unknown" in prompt
        assert "Require separate commits only when the public requirement explicitly requires them" in prompt
        assert "ordered public evidence available to the check at execution" in prompt
        assert "If a required sequence cannot be established, report unavailable" in prompt


@pytest.mark.asyncio
async def test_public_spec_shim_and_registered_check_use_separate_checkpoints(
    tmp_path, git_repo_factory, shipping_public_model, monkeypatch
):
    from jiuwenswarm.long_horizon.clarification import capture_workspace

    repo = git_repo_factory(tmp_path / "repo")
    snapshot_workspace_files(repo)
    snapshot = capture_workspace(repo, tmp_path / "snapshot")
    worker = scripted_worker(tmp_path)
    model = shipping_public_model.JiuwenClarificationModel(
        worker=worker, snapshot=snapshot, output_dir=tmp_path / "out"
    )
    run = worker.run
    prompts = []

    async def capture(**kwargs):
        prompts.append(kwargs["prompt"])
        return await run(**kwargs)

    worker.run = capture
    criteria = criterion_set(1)
    await model.generate_public_verification("task", acceptance_criteria=criteria)
    assert len(prompts) == 4
    assert all(
        "JIUWENSWARM_PACT_SOURCE_WORKSPACE" in prompt and "CHRYS_PACT_SOURCE_WORKSPACE" not in prompt
        for prompt in prompts
    )
    model.public_spec_shim = False
    await model.generate_public_verification("task", acceptance_criteria=criteria)
    assert len(prompts) == 8
    assert all("registered checks run in the detached candidate checkout" in prompt for prompt in prompts[4:])
    await model.generate_public_verification("task", acceptance_criteria=criteria)
    assert len(prompts) == 8
    records = list((model.output_dir / "public-verification-checkpoints").glob("*/*.json"))
    assert len({json.loads(path.read_text())["identity"]["prompt_sha256"] for path in records}) == 2
    monkeypatch.setattr(
        shipping_public_model,
        "PUBLIC_VERIFICATION_REVIEW_INSTRUCTIONS",
        shipping_public_model.PUBLIC_VERIFICATION_REVIEW_INSTRUCTIONS + "\nAudit revised evidence.",
    )
    await model.generate_public_verification("task", acceptance_criteria=criteria)
    assert len(prompts) == 12
    records = list((model.output_dir / "public-verification-checkpoints").glob("*/*.json"))
    assert len({json.loads(path.read_text())["identity"]["public_check_instructions_sha256"] for path in records}) == 2


def test_public_shell_fallbacks_follow_the_final_observation(tmp_path, shipping_public_model):
    validate = shipping_public_model.PublicVerification

    def check(command):
        validate(
            schema="jiuwenswarm/pact-public-verification/v1",
            status="ready",
            reason="Observe the result",
            checks=[dict(id="observe", target_ac_ids=["ac1"], rationale="Read the result", command=command)],
        ).validate_generated_commands()

    producer = tmp_path / "produce"
    producer.write_text("#!/bin/sh\ncat fixture 2>/dev/null\nexit 8\n")
    producer.chmod(0o700)
    capture = (
        "./produce >result 2>errors || true\n"
        "if grep -q 'SUCCESS' result; then\n"
        "  echo checked\n  exit 0\nfi\n"
        "cat errors >&2 || :\nexit 1"
    )
    check(capture)
    # Producer exits nonzero for both fixtures; this check's verdict is its output.
    fixture = tmp_path / "fixture"
    for supplied, expected in [("FAIL", 1), ("SUCCESS", 0), ("", 1)]:
        fixture.write_text(supplied)
        assert subprocess.run(["/bin/sh", "-c", capture], cwd=tmp_path, capture_output=True).returncode == expected

    diagnosed = "if test -s condition; then\nexit 0\nfi\ncat diagnostics >&2 || true\nexit 1"
    for unsafe in [
        "trap 'exit 0' EXIT\n" + diagnosed,
        "force_pass() {\nexit 0\n}\ntrap force_pass 0\n" + diagnosed,
        "bash -c :\ncat <<'DOC'\n'Hello\nDOC\ntest -s missing || true",
    ]:
        with pytest.raises(ValueError, match="unconditional success fallback"):
            check(unsafe)
        for exists in (False, True):
            if exists:
                (tmp_path / "condition").write_text("yes")
            else:
                (tmp_path / "condition").unlink(missing_ok=True)
            assert subprocess.run(["/bin/bash", "-c", unsafe], cwd=tmp_path, capture_output=True).returncode == 0
    check("tidy() {\nrm -f diagnostics\n}\ntrap tidy EXIT\n" + capture)

    for command in [
        "false || true",
        "false ||\n true",
        "false ||\n :",
        "false || true\nprintf done",
        "false || true; exit 0",
        "test -s ./src/C#example || true",
        'test -s "./src/C"#example || true',
        "cat >test.sh <<'CHECK'\nfalse || true\nCHECK\n/bin/sh test.sh",
        """eval "$(cat <<'CHECK'\nfalse || true\nCHECK\n)" """,
        "cat <<'CHECK' | /bin/sh\nfalse || true\nCHECK",
        capture.replace("./produce", "printf SUCCESS"),
        capture.replace(">result", ">>result"),
        capture.replace("SUCCESS' result", "SUCCESS' stale"),
        capture.replace("result; then", "result || true; then"),
        capture.replace("exit 1", "exit 0"),
        "false || true\n" + capture,
    ]:
        with pytest.raises(ValueError, match="unconditional success fallback"):
            check(command)
    for command in [
        "cat <<'DOC'\nexample || true\nDOC\ntest -s fixture",
        "test -s fixture\n# example || true",
        "if test -s fixture; then\nexit 0\nfi\ncat errors >&2 || true\nexit 1",
    ]:
        check(command)


def test_public_check_validates_unhandled_literal_regex_in_a_python_entrypoint(shipping_public_model):
    def validate(command):
        value = shipping_public_model.PublicVerification(
            schema="jiuwenswarm/pact-public-verification/v1",
            status="ready",
            reason="Confirm public records",
            checks=[dict(id="check", target_ac_ids=["ac1"], rationale="Read public records", command=command)],
        )
        value.validate_generated_commands()

    def python(body):
        return "python3 - <<'VERIFY'\nimport re\n" + body + "\nVERIFY"

    with pytest.raises(ValueError, match=r"invalid Python regular expression.*line 2.*global flags"):
        validate(python('pattern = re.compile(r"""\n   (?ix) public-record""")'))
    with pytest.raises(ValueError, match="unterminated character set"):
        validate(python("pattern = re.compile('[')"))
    for program in [
        "pattern = re.compile('(?i)public-record')",
        "pattern = re.compile('\\n  (?ix) public-record', re.VERBOSE)",
        "pattern = re.compile(candidate_pattern)",
        "example = \"re.compile('[')\"",
        "if False:\n    example = re.compile('[')",
        "try:\n    re.compile('[')\nexcept re.error:\n    pass",
        "def example():\n    return re.compile('[')",
        "import sys\nsys.exit(0)\nre.compile('[')",
        "import re, fixture_matcher as re\nre.compile('[')",
        "import fixture_matcher as re\npattern = re.compile('[')",
        "re.compile = make_matcher\npattern = re.compile('[')",
        "setup(re)\npattern = re.compile('[')",
    ]:
        validate(python(program))
    for prelude in [
        "if enabled:\n    assert matches_fixture\n    raise SystemExit(0)",
        "if enabled:\n    assert matches_fixture\n    raise SystemExit",
        "import sys\nfinish = sys.exit\nif enabled:\n    finish()",
        "from sys import exit as finish\nif enabled:\n    finish()",
    ]:
        body = prelude + "\nimport re\nfallback = re.compile(' (?i)public')"
        validate("python3 - <<'VERIFY'\n" + body + "\nVERIFY")
        validate(python(body))
    with pytest.raises(ValueError, match="unterminated character set"):
        validate(python("if bad_input:\n    raise SystemExit(2)\nre.compile('[')"))
    for command in [
        "cat <<'EXAMPLE'\n" + python("re.compile('[')") + "\nEXAMPLE",
        "if false; then\n" + python("re.compile('[')") + "\nfi",
        "python3 -c 'print(7)' <<'DATA'\nimport re\nre.compile('[')\nDATA",
        python("re.compile('[')").replace("python3 -", "python3.10 -"),
    ]:
        validate(command)


def test_only_explicit_unsupported_shell_workspace_preconditions_are_rejected(shipping_public_model):
    validate = shipping_public_model.validate_public_workspace_dependencies
    variable = "JIUWENSWARM_PACT_SOURCE_WORKSPACE"
    guard = ': "${' + variable + ':?required for provenance}"'
    shell = "bash <<'CHECK'\nset -eu\n" + guard + "\nCHECK"
    with pytest.raises(ValueError, match="verification runtime does not supply"):
        validate(shell, public_spec_shim=False)
    validate(shell, public_spec_shim=True)
    for prior in ["cat <<'FIX'\nfixture\nFIX", "# Example << data"]:
        with pytest.raises(ValueError, match="verification runtime does not supply"):
            validate(prior + "\n" + guard, public_spec_shim=False)
    for prior in [variable + "=candidate env >/dev/null", variable + "=candidate\nunset " + variable]:
        with pytest.raises(ValueError, match="verification runtime does not supply"):
            validate(prior + "\n" + guard, public_spec_shim=False)
    with pytest.raises(ValueError, match="CHRYS_PACT_SOURCE_WORKSPACE"):
        validate(guard.replace("JIUWENSWARM", "CHRYS"), public_spec_shim=True)
    for text in [
        'test -z "${' + variable + ':-}"',
        "cat <<'DATA'\n" + guard + "\nDATA",
        "printf '%s' '\n" + guard + "\n'",
        "# " + guard,
        ': "${' + variable + ':=candidate}"\n' + guard,
        'if [ -n "${' + variable + ':-}" ]; then\n' + guard + '\nfi\ntest -n "fallback"',
        "export " + variable + "=fixture\n" + guard,
    ]:
        validate(text, public_spec_shim=False)


def test_required_nonempty_alias_uses_the_active_workspace_capability(shipping_public_model):
    validate = shipping_public_model.validate_public_workspace_dependencies
    prefix = (
        "set -e\n"
        "[ -f project.toml ] || { echo 'missing project'; exit 1; }\n"
        "git rev-parse --is-inside-work-tree >/dev/null\n"
        'test -z "$(git status --porcelain --untracked-files=no)" || { echo "dirty"; exit 1; }\n'
        "head=$(git rev-parse --verify HEAD)\n"
    )
    alias = 'src=${JIUWENSWARM_PACT_SOURCE_WORKSPACE:-}\n'
    require = '[ -n "$src" ] || { echo "missing origin" >&2; exit 1; }'
    with pytest.raises(ValueError, match="requires JIUWENSWARM_PACT_SOURCE_WORKSPACE"):
        validate(prefix + alias + require, public_spec_shim=False)
    validate(prefix + alias + require, public_spec_shim=True)
    for shim in (False, True):
        missing = alias.replace("JIUWENSWARM", "CHRYS")
        with pytest.raises(ValueError, match="requires CHRYS_PACT_SOURCE_WORKSPACE"):
            validate(prefix + missing + 'second="$src"\ntest -n "$second" || exit 1', public_spec_shim=shim)
        for valid in (
            missing + 'src="$PWD"\n' + require,
            'export CHRYS_PACT_SOURCE_WORKSPACE="$PWD"\n' + missing + require,
            'src="${CHRYS_PACT_SOURCE_WORKSPACE:-$PWD}"\n' + require,
            missing + 'if [ -n "$src" ]; then\n  echo "inspect refs"\nfi\ngit rev-parse HEAD',
            missing + 'src=$(choose_workspace)\n' + require,
            "choose_workspace && exit 0\n" + missing + require,
            missing + '[ -n "$src" ] || src="$PWD"\n' + require,
            'SECONDS=${CHRYS_PACT_SOURCE_WORKSPACE:-}\ntest -n "$SECONDS" || exit 1',
        ):
            validate(prefix + valid, public_spec_shim=shim)


def test_attached_head_fallback_depends_on_the_probed_checkout(shipping_public_model):
    validate = shipping_public_model.validate_public_workspace_dependencies
    guard = ': "${CHRYS_PACT_SOURCE_WORKSPACE:?need primary checkout}"'
    check = (
        'if ref="$(git -C "$probe" symbolic-ref -q --short HEAD 2>/dev/null)"; then\n'
        '  echo "attached"\nelse\n' + guard + "\nfi"
    )
    current = 'probe="$PWD"\n' + check
    with pytest.raises(ValueError, match="CHRYS_PACT_SOURCE_WORKSPACE"):
        validate(current, public_spec_shim=False)
    for prefix in ['probe="/primary"', 'probe="$PWD"\nprobe="/primary"', 'export GIT_DIR=/primary/.git\nprobe="$PWD"']:
        validate(prefix + "\n" + check, public_spec_shim=False)
    validate(current, public_spec_shim=True)


@pytest.mark.asyncio
async def test_eight_criteria_use_four_work_conserving_real_jiuwen_lanes(tmp_path, git_repo_factory):
    from jiuwenswarm.long_horizon.clarification import capture_workspace, snapshot_workspace

    repo = git_repo_factory(tmp_path / "public-repo")
    snapshot_workspace_files(repo)
    snapshot = capture_workspace(repo, tmp_path / "frozen")
    workspace = snapshot_workspace(snapshot)
    criteria = tuple(types.PactAcceptanceCriterion(id=f"ac{i}", text=f"Public criterion {i}") for i in range(1, 9))
    deadline = time.time() + 300
    admission = SharedAdmission(deadline=deadline)
    worker = scripted_worker(tmp_path, block={"ac1", "ac2", "ac4", "ac5"})
    context = SimpleNamespace(
        snapshot=snapshot,
        deadline=deadline,
        session_dir=tmp_path / "session",
        admission=admission,
        session_id="public-criterion-lanes",
    )
    plugin = plugin_for(worker, workspace)

    async def invoke():
        runner = await plugin.planning_runner(context, "Original full public task")
        return runner, await runner.generate_public_verification(
            "Original full public task", acceptance_criteria=criteria
        )

    task = asyncio.create_task(invoke())
    await asyncio.wait_for(worker.started["ac5"].wait(), 15)
    # #5 starts only after #3's independent review; #1 is still in flight.
    assert worker.reviewed["ac3"].is_set()
    assert "ac1" in worker.blocked and "ac1" not in worker.released
    assert worker.active == worker.peak == 4
    for event in worker.release.values():
        event.set()
    runner, (value, usage) = await asyncio.wait_for(task, 30)

    assert runner.public_concurrency == 4 and runner.public_runner_factory is not None
    assert len(worker.calls) == 32
    assert len({call["stage"] for call in worker.calls}) == 4
    assert {call["stage"] for call in worker.calls} == {
        "public-verification-ac1-investigation",
        "public-verification-ac1-synthesis",
        "public-verification-review-ac1-investigation",
        "public-verification-review-ac1-synthesis",
    }
    assert usage["input_tokens"] == 64
    assert len(admission.admitted) == len(admission.finished) == 32
    assert {row["deadline"] for row in admission.admitted} == {deadline}
    assert [check.id for check in value.checks] == [f"ac{i}-check1" for i in range(1, 9)]
    assert [check.target_ac_ids for check in value.checks] == [[f"ac{i}"] for i in range(1, 9)]

    native_root = context.session_dir / "native-attempts"
    records = sorted(native_root.glob("*/public-verification-checkpoints/*/*.json"))
    assert len(records) == 8
    child_dirs = {record.parents[2] for record in records}
    assert len(child_dirs) == 8
    assert all(re.fullmatch(r"[0-9a-f]{32}", directory.name) for directory in child_dirs)
    assert all(
        len(list((directory / "public-verification-checkpoints").glob("*/*.json"))) == 1 for directory in child_dirs
    )
    assert {json.loads(record.read_text())["criterion"]["id"] for record in records} == {c.id for c in criteria}
    stage_receipts = [path for directory in child_dirs for path in directory.glob("public-stage-*.json")]
    assert len(stage_receipts) == 16 and len({str(path) for path in stage_receipts}) == 16
    sessions = set()
    for path in stage_receipts:
        receipt = json.loads(path.read_text())
        sessions.update({receipt["investigation_session_id"], receipt["synthesis_session_id"]})
    assert len(sessions) == 32


@pytest.mark.asyncio
async def test_shared_parent_runner_factory_is_rejected(tmp_path):
    holder = {}

    def factory():
        return holder["runner"].runner

    cache = runner_fixture(tmp_path, factory, concurrency=2)
    holder["runner"] = cache
    criteria = tuple(types.PactAcceptanceCriterion(id=f"ac{i}", text=f"Criterion {i}") for i in (1, 2))
    with pytest.raises(PlanningCheckpointError, match="must not share"):
        await cache.generate_public_verification("full prompt", acceptance_criteria=criteria)
    assert not list((tmp_path / "session/completed-planning").glob("*.json"))


@pytest.mark.asyncio
async def test_integrity_failure_priority_is_single_and_not_an_exception_group(tmp_path):
    ordinary = ValueError("ordinary lane failure")
    integrity = SideCallIntegrityError("frozen source changed")
    children = iter(
        [
            ArtifactRunner(tmp_path / "child-1", failure=ordinary),
            ArtifactRunner(tmp_path / "child-2", failure=integrity),
        ]
    )
    cache = runner_fixture(tmp_path, lambda: next(children), concurrency=2)
    criteria = tuple(types.PactAcceptanceCriterion(id=f"ac{i}", text=f"Criterion {i}") for i in (1, 2))
    with pytest.raises(SideCallIntegrityError) as failure:
        await cache.generate_public_verification("full prompt", acceptance_criteria=criteria)
    assert failure.value is integrity
    assert not isinstance(failure.value, BaseExceptionGroup)
    assert any("also failed" in note for note in failure.value.__notes__)
    assert not list((tmp_path / "session/completed-planning").glob("*.json"))


@pytest.mark.asyncio
async def test_enospc_in_cached_is_an_oserror_not_taskgroup_cancellation(tmp_path, monkeypatch):
    import errno

    base = tmp_path / "enospc"
    gate = LaneGate(4)
    children = [ArtifactRunner(base / f"child-{index}", tag=f"e{index}", gate=gate) for index in range(1, 5)]
    cache = lane_cache(base, children, concurrency=4)
    capacity = OSError(errno.ENOSPC, "No space left on device")

    def capacity_save(stage, payload):
        raise capacity

    monkeypatch.setattr(cache.checkpoint, "save", capacity_save)
    caller = None

    async def invoke():
        nonlocal caller
        caller = asyncio.current_task()
        with pytest.raises(OSError) as failure:
            await cache.generate_public_verification("full prompt", acceptance_criteria=criterion_set())
        return failure.value

    task = asyncio.create_task(invoke())
    await asyncio.wait_for(gate.ready.wait(), 15)
    assert gate.started == gate.peak == 4
    gate.release.set()
    error = await asyncio.wait_for(task, 15)
    assert error is capacity
    assert caller is not None and caller.cancelling() == 0
    assert gate.active == 0
    assert all(child.calls == 1 for child in children)
    assert not list(cache.checkpoint.root.glob("*.json"))


@pytest.mark.parametrize("error_type", [Exhausted, TimeoutError, ValueError])
@pytest.mark.asyncio
async def test_ordinary_lane_failure_keeps_its_original_exception(tmp_path, error_type):
    base = tmp_path / error_type.__name__
    gate = LaneGate(4)
    failures = [error_type(f"ordinary lane failure {index}") for index in range(1, 5)]
    children = [
        ArtifactRunner(base / f"child-{index}", tag=f"o{index}", failure=failures[index - 1], gate=gate)
        for index in range(1, 5)
    ]
    cache = lane_cache(base, children, concurrency=4)
    caller = None

    async def invoke():
        nonlocal caller
        caller = asyncio.current_task()
        with pytest.raises(error_type) as failure:
            await cache.generate_public_verification("full prompt", acceptance_criteria=criterion_set())
        return failure.value

    task = asyncio.create_task(invoke())
    await asyncio.wait_for(gate.ready.wait(), 15)
    gate.release.set()
    error = await asyncio.wait_for(task, 15)
    assert isinstance(error, error_type)
    assert str(error).startswith("ordinary lane failure")
    assert caller is not None and caller.cancelling() == 0
    assert gate.active == 0
    assert not isinstance(error, BaseExceptionGroup)
    assert not list(cache.checkpoint.root.glob("*.json"))


@pytest.mark.asyncio
async def test_serial_public_failure_keeps_its_original_exception(tmp_path):
    ordinary = ValueError("ordinary serial failure")
    cache = runner_fixture(tmp_path / "serial", None, concurrency=1)

    async def fail(prompt, *, acceptance_criteria):
        raise ordinary

    cache.runner.generate_public_verification = fail
    caller = asyncio.current_task()
    with pytest.raises(ValueError) as failure:
        await cache.generate_public_verification("full prompt", acceptance_criteria=criterion_set(1))
    assert failure.value is ordinary
    assert caller is not None and caller.cancelling() == 0
    assert not list(cache.checkpoint.root.glob("*.json"))


@pytest.mark.asyncio
async def test_external_cancel_joins_every_lane_and_reclaims_it(tmp_path):
    base = tmp_path / "external-cancel"
    gate = LaneGate(4)
    children = [ArtifactRunner(base / f"child-{index}", tag=f"c{index}", gate=gate) for index in range(1, 5)]
    cache = lane_cache(base, children, concurrency=4)

    async def invoke():
        await cache.generate_public_verification("full prompt", acceptance_criteria=criterion_set())

    task = asyncio.create_task(invoke())
    await asyncio.wait_for(gate.ready.wait(), 15)
    assert gate.started == gate.peak == 4
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 15)
    assert task.cancelled()
    assert gate.active == 0
    assert all(child.calls == 1 for child in children)
    assert not list(cache.checkpoint.root.glob("*.json"))


@pytest.mark.asyncio
async def test_external_cancel_outranks_an_ordinary_lane_failure(tmp_path):
    base = tmp_path / "cancel-and-ordinary"
    gate = LaneGate(4)
    hold = asyncio.Event()
    sibling_cancelled = asyncio.Event()
    block = asyncio.Event()
    ordinary = ValueError("ordinary lane failure")
    children = [
        ArtifactRunner(base / "child-1", tag="x1", failure=ordinary, gate=gate),
        ArtifactRunner(
            base / "child-2",
            tag="x2",
            gate=gate,
            wait_after_release=block,
            hold_cancel=hold,
            cancel_started=sibling_cancelled,
        ),
        ArtifactRunner(base / "child-3", tag="x3", gate=gate, wait_after_release=block, hold_cancel=hold),
        ArtifactRunner(base / "child-4", tag="x4", gate=gate, wait_after_release=block, hold_cancel=hold),
    ]
    cache = lane_cache(base, children, concurrency=4)

    async def invoke():
        await cache.generate_public_verification("full prompt", acceptance_criteria=criterion_set())

    task = asyncio.create_task(invoke())
    await asyncio.wait_for(gate.ready.wait(), 15)
    gate.release.set()
    await asyncio.wait_for(sibling_cancelled.wait(), 15)
    assert children[0].raised == [ordinary]
    task.cancel()
    hold.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 15)
    assert task.cancelled()
    assert gate.active == 0


@pytest.mark.asyncio
async def test_integrity_failure_outranks_external_cancel(tmp_path):
    base = tmp_path / "integrity-and-cancel"
    gate = LaneGate(4)
    hold = asyncio.Event()
    sibling_cancelled = asyncio.Event()
    block = asyncio.Event()
    integrity = SideCallIntegrityError("frozen source changed")
    children = [
        ArtifactRunner(base / "child-1", tag="i1", failure=integrity, gate=gate),
        ArtifactRunner(
            base / "child-2",
            tag="i2",
            gate=gate,
            wait_after_release=block,
            hold_cancel=hold,
            cancel_started=sibling_cancelled,
        ),
        ArtifactRunner(base / "child-3", tag="i3", gate=gate, wait_after_release=block, hold_cancel=hold),
        ArtifactRunner(base / "child-4", tag="i4", gate=gate, wait_after_release=block, hold_cancel=hold),
    ]
    cache = lane_cache(base, children, concurrency=4)

    async def invoke():
        await cache.generate_public_verification("full prompt", acceptance_criteria=criterion_set())

    task = asyncio.create_task(invoke())
    await asyncio.wait_for(gate.ready.wait(), 15)
    gate.release.set()
    await asyncio.wait_for(sibling_cancelled.wait(), 15)
    assert children[0].raised == [integrity]
    task.cancel()
    hold.set()
    with pytest.raises(SideCallIntegrityError) as failure:
        await asyncio.wait_for(task, 15)
    assert failure.value is integrity
    assert not isinstance(failure.value, BaseExceptionGroup)
    assert gate.active == 0


@pytest.mark.asyncio
async def test_nested_reused_task_keeps_a_zero_cancel_count(tmp_path):
    first_base = tmp_path / "nested-first"
    ordinary = ValueError("ordinary lane failure")
    first = lane_cache(
        first_base,
        [ArtifactRunner(first_base / "child", failure=ordinary)],
        concurrency=1,
    )
    second_base = tmp_path / "nested-second"
    success = lane_cache(
        second_base,
        [ArtifactRunner(second_base / "child", tag="reused")],
        concurrency=1,
    )
    counts = []
    outer_task = None

    async def invoke():
        me = asyncio.current_task()
        assert me.cancelling() == 0
        with pytest.raises(ValueError):
            await first.generate_public_verification("first prompt", acceptance_criteria=criterion_set(1))
        counts.append(me.cancelling())
        assert me.cancelling() == 0
        value, usage = await success.generate_public_verification("second prompt", acceptance_criteria=criterion_set(1))
        counts.append(me.cancelling())
        assert [check.target_ac_ids for check in value.checks] == [["ac1"]]
        assert usage["total_tokens"] == 3
        assert me.cancelling() == 0

    async def outer():
        nonlocal outer_task
        outer_task = asyncio.current_task()
        async with asyncio.TaskGroup() as group:
            group.create_task(invoke())
        assert outer_task.cancelling() == 0

    await outer()
    assert counts == [0, 0]
    assert outer_task is not None and outer_task.cancelling() == 0


@pytest.mark.parametrize("mechanism", ["wait_for", "timeout"])
@pytest.mark.asyncio
async def test_external_timeout_is_timeout_and_joins_lanes(tmp_path, mechanism):
    base = tmp_path / mechanism
    gate = LaneGate(4)
    children = [ArtifactRunner(base / f"child-{index}", tag=f"t{index}", gate=gate) for index in range(1, 5)]
    cache = lane_cache(base, children, concurrency=4)
    owner = asyncio.current_task()

    async def invoke():
        await cache.generate_public_verification("full prompt", acceptance_criteria=criterion_set())

    task = asyncio.create_task(invoke())
    await asyncio.wait_for(gate.ready.wait(), 15)
    with pytest.raises(TimeoutError):
        if mechanism == "wait_for":
            await asyncio.wait_for(task, 0.1)
        else:
            async with asyncio.timeout(0.1):
                await task
    assert task.cancelled()
    assert gate.started == gate.peak == 4
    assert gate.active == 0
    assert owner is not None and owner.cancelling() == 0
    assert not list(cache.checkpoint.root.glob("*.json"))


@pytest.mark.asyncio
async def test_external_timeout_during_ordinary_failure_stays_timeout(tmp_path):
    base = tmp_path / "timeout-ordinary"
    gate = LaneGate(4)
    hold = asyncio.Event()
    sibling_cancelled = asyncio.Event()
    block = asyncio.Event()
    ordinary = ValueError("ordinary lane failure")
    children = [
        ArtifactRunner(base / "child-1", tag="u1", failure=ordinary, gate=gate),
        ArtifactRunner(
            base / "child-2",
            tag="u2",
            gate=gate,
            wait_after_release=block,
            hold_cancel=hold,
            cancel_started=sibling_cancelled,
        ),
        ArtifactRunner(base / "child-3", tag="u3", gate=gate, wait_after_release=block, hold_cancel=hold),
        ArtifactRunner(base / "child-4", tag="u4", gate=gate, wait_after_release=block, hold_cancel=hold),
    ]
    cache = lane_cache(base, children, concurrency=4)

    async def invoke():
        await cache.generate_public_verification("full prompt", acceptance_criteria=criterion_set())

    task = asyncio.create_task(invoke())
    await asyncio.wait_for(gate.ready.wait(), 15)
    gate.release.set()
    await asyncio.wait_for(sibling_cancelled.wait(), 15)
    assert children[0].raised == [ordinary]
    # The timeout fires while the worker is still joining its held siblings.
    asyncio.get_running_loop().call_later(0.2, hold.set)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(task, 0.1)
    assert task.cancelled()
    assert gate.active == 0


@pytest.mark.asyncio
async def test_nested_timeouts_keep_the_callers_cancel_state_sound(tmp_path):
    base = tmp_path / "nested-timeouts"
    gate = LaneGate(4)
    children = [ArtifactRunner(base / f"child-{index}", tag=f"n{index}", gate=gate) for index in range(1, 5)]
    cache = lane_cache(base, children, concurrency=4)
    owner = asyncio.current_task()

    async def invoke():
        async with asyncio.timeout(0.5):
            await cache.generate_public_verification("full prompt", acceptance_criteria=criterion_set())

    task = asyncio.create_task(invoke())
    await asyncio.wait_for(gate.ready.wait(), 15)
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.1):
            await task
    assert task.cancelled()
    assert gate.started == gate.peak == 4
    assert gate.active == 0
    assert owner is not None and owner.cancelling() == 0


async def wait_for_checkpoints(root: Path, count: int) -> None:
    for _ in range(1000):
        if len(list(root.glob("*.json"))) >= count:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"only {len(list(root.glob('*.json')))} completed planning checkpoints")


@pytest.mark.asyncio
async def test_interruption_joins_then_resume_reuses_only_completed_criteria(tmp_path, git_repo_factory):
    from jiuwenswarm.long_horizon.clarification import capture_workspace, snapshot_workspace

    repo = git_repo_factory(tmp_path / "public-repo")
    snapshot_workspace_files(repo)
    snapshot = capture_workspace(repo, tmp_path / "frozen")
    workspace = snapshot_workspace(snapshot)
    criteria = tuple(types.PactAcceptanceCriterion(id=f"ac{i}", text=f"Public criterion {i}") for i in range(1, 9))
    deadline = time.time() + 300
    admission = SharedAdmission(deadline=deadline)
    worker = scripted_worker(tmp_path, block={"ac4", "ac5", "ac6", "ac7", "ac8"})
    context = SimpleNamespace(
        snapshot=snapshot,
        deadline=deadline,
        session_dir=tmp_path / "session",
        admission=admission,
        session_id="public-criterion-interrupt",
    )
    plugin = plugin_for(worker, workspace)

    async def invoke():
        runner = await plugin.planning_runner(context, "Original full public task")
        return await runner.generate_public_verification("Original full public task", acceptance_criteria=criteria)

    task = asyncio.create_task(invoke())
    await asyncio.wait_for(worker.reviewed["ac3"].wait(), 15)
    await wait_for_checkpoints(tmp_path / "session/completed-planning", 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 15)
    assert worker.active == 0
    assert len(admission.admitted) == len(admission.finished)

    resumed_admission = SharedAdmission(deadline=time.time() + 300)
    resumed_worker = scripted_worker(tmp_path)
    resumed_context = SimpleNamespace(
        snapshot=snapshot,
        deadline=resumed_admission.deadline,
        session_dir=tmp_path / "session",
        admission=resumed_admission,
        session_id="public-criterion-resume",
    )
    resumed_plugin = plugin_for(resumed_worker, workspace)
    resumed_runner = await resumed_plugin.planning_runner(resumed_context, "Original full public task")
    value, usage = await asyncio.wait_for(
        resumed_runner.generate_public_verification("Original full public task", acceptance_criteria=criteria), 30
    )

    # Three durable criteria are reused without any worker call or usage.
    assert len(resumed_worker.calls) == 20
    assert {call["criterion"] for call in resumed_worker.calls} == {f"ac{i}" for i in range(4, 9)}
    assert usage["input_tokens"] == 40
    assert [check.id for check in value.checks] == [f"ac{i}-check1" for i in range(1, 9)]
    assert [check.target_ac_ids for check in value.checks] == [[f"ac{i}"] for i in range(1, 9)]
    assert len(list((tmp_path / "session/completed-planning").glob("*.json"))) >= 8


def test_incomplete_parallel_review_is_a_typed_recoverable_and_old_data_is_optional():
    ready = {"public_verification": {"status": "ready", "requires_complete_review": True, "checks": [{}]}}
    assert incomplete_public_review(ready) is None
    incomplete = {
        "public_verification": {
            "status": "unavailable",
            "requires_complete_review": True,
            "checks": [],
            "degradation": {"reason": "trial_budget_refused"},
        }
    }
    progress = incomplete_public_review(incomplete)
    assert progress is not None and progress.reason == "trial_budget_refused"
    error = recoverable_public_review_error(Exhausted, progress)
    assert isinstance(error, Exhausted)
    assert public_review_failure(error) is progress
    # Historical serial callers carry no marker and keep the optional policy.
    assert (
        incomplete_public_review(
            {"public_verification": {"status": "unavailable", "checks": [], "degradation": {"reason": "old"}}}
        )
        is None
    )


def test_acceptance_criterion_schema_already_rejects_path_unsafe_ids(tmp_path):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        types.PactAcceptanceCriterion(id="../a b/ü", text="Unsafe original identity")
    assert not list(tmp_path.iterdir())


def test_only_real_jiuwen_suite_requests_a_complete_public_review(tmp_path):
    from jiuwenswarm.lh.plugins.jiuwen import JiuwenPlugin

    plugin = JiuwenPlugin(worker=SimpleNamespace())
    plugin._side_args = SimpleNamespace(workflow="suite")
    assert plugin.requires_complete_public_review
    plugin._side_args = SimpleNamespace(workflow="suite", verify_command="python3 -m pytest")
    assert not plugin.requires_complete_public_review
    plugin._side_args = SimpleNamespace(workflow="suite")
    plugin.verification_strategy = "candidate-review"
    assert not plugin.requires_complete_public_review
    plugin.verification_strategy = "frozen-public-checks"
    plugin._runner_factory = lambda context: None
    assert not plugin.requires_complete_public_review
    plugin._runner_factory = None
    plugin._side_args = SimpleNamespace(workflow="benchmark")
    assert not plugin.requires_complete_public_review


def _suite_plugin(*, generate_pact_input, artifacts=None):
    class Artifacts:
        exhausted = Exhausted
        stopped = type("Stopped", (Exhausted,), {})

    plugin = SimpleNamespace(
        artifacts=artifacts or Artifacts(),
        requires_complete_public_review=True,
        degraded_clarification=lambda reason: SimpleNamespace(delta="", reason=reason),
    )

    async def clarify(context, original_task):
        return SimpleNamespace(delta="")

    async def recall(context, original_task):
        return ""

    plugin.clarify = clarify
    plugin.recall = recall
    plugin.generate_pact_input = generate_pact_input
    return plugin


@pytest.mark.asyncio
async def test_bounded_contract_plan_timeout_degrades_before_public_review(monkeypatch):
    from jiuwenswarm.lh.suite import _prepare_suite_bounded

    async def generate_pact_input(context, result, original_task, *, hints):
        raise TimeoutError("planning window closed")

    async def localize(*args, **kwargs):
        return {"status": "disabled", "text": "", "artifacts": []}

    monkeypatch.setattr("jiuwenswarm.lh.suite.localize_suite", localize)
    context = SimpleNamespace(snapshot=SimpleNamespace(roots=[]))
    _, prepared = await _prepare_suite_bounded(
        _suite_plugin(generate_pact_input=generate_pact_input),
        context,
        "Original task",
        planning_deadline=time.time() + 5,
    )
    assert prepared["planning_status"] == "degraded_original_task"
    assert "public_verification" not in prepared
    assert prepared["planning_diagnostics"] == [{"stage": "contract-plan", "reason": "TimeoutError"}]


@pytest.mark.asyncio
async def test_legacy_contract_plan_failure_degrades_before_public_review(monkeypatch):
    from pact_core.runtime import RuntimeContractError

    from jiuwenswarm.lh.suite import _prepare_suite_legacy

    async def generate_pact_input(context, result, original_task, *, hints):
        raise RuntimeContractError("invalid plan")

    async def localize(*args, **kwargs):
        return {"status": "disabled", "text": "", "artifacts": []}

    monkeypatch.setattr("jiuwenswarm.lh.suite.localize_suite", localize)
    context = SimpleNamespace(snapshot=SimpleNamespace(roots=[]))
    _, prepared = await _prepare_suite_legacy(
        _suite_plugin(generate_pact_input=generate_pact_input), context, "Original task"
    )
    assert prepared["planning_status"] == "degraded_original_task"
    assert "public_verification" not in prepared
    assert prepared["planning_diagnostics"] == [{"stage": "contract-plan", "reason": "RuntimeContractError"}]


@pytest.mark.asyncio
async def test_outer_planning_timeout_after_public_review_began_is_recoverable(monkeypatch):
    from jiuwenswarm.lh.public_review import PublicReviewProgress, set_public_review
    from jiuwenswarm.lh.suite import _prepare_suite_bounded

    async def generate_pact_input(context, result, original_task, *, hints):
        set_public_review(
            PublicReviewProgress(started=True, required_criteria=("ac1", "ac2"), completed_criteria=("ac1",))
        )
        await asyncio.sleep(10)

    async def localize(*args, **kwargs):
        return {"status": "disabled", "text": "", "artifacts": []}

    monkeypatch.setattr("jiuwenswarm.lh.suite.localize_suite", localize)
    context = SimpleNamespace(snapshot=SimpleNamespace(roots=[]))
    with pytest.raises(Exhausted) as failure:
        await _prepare_suite_bounded(
            _suite_plugin(generate_pact_input=generate_pact_input),
            context,
            "Original task",
            planning_deadline=time.time() + 0.2,
        )
    progress = public_review_failure(failure.value)
    assert progress is not None
    assert progress.reason == "timeout_before_planning_deadline"
    assert progress.started and progress.completed_criteria == ("ac1",)


def test_cancellation_precedes_an_ordinary_sibling_but_never_integrity(tmp_path):
    cache = runner_fixture(tmp_path, lambda: ArtifactRunner(tmp_path / "unused"), concurrency=2)
    ordinary = ValueError("ordinary sibling failure")
    integrity = SideCallIntegrityError("frozen source changed")
    assert isinstance(
        cache._public_group_failure(ValueError("group"), {1: ordinary}, cancelling=True), asyncio.CancelledError
    )
    assert cache._public_group_failure(ValueError("group"), {1: integrity}, cancelling=True) is integrity
    assert cache._public_group_failure(ValueError("group"), {1: ordinary}, cancelling=False) is ordinary
    assert isinstance(
        cache._public_group_failure(asyncio.CancelledError(), {}, cancelling=False), asyncio.CancelledError
    )


@pytest.mark.asyncio
async def test_complete_review_budget_refusal_is_a_typed_recoverable(tmp_path):
    from jiuwenswarm.lh.plugins.jiuwen import JiuwenPlugin
    from jiuwenswarm.lh.plugins.native import PublicRepairCap
    from jiuwenswarm.lh.public_review import public_review_failure

    plugin = JiuwenPlugin(worker=SimpleNamespace())
    plugin._side_args = SimpleNamespace(verify_command=None, workflow="suite", budget_seconds=120)
    plugin.public_verification_degradation = None

    class Admission:
        refused = "trial_budget_refused"
        admitted: list[str] = []

        def admit(self, stage):
            self.admitted.append(stage)
            return SimpleNamespace(token=stage)

        def finish(self, ticket, *, status, detail=""):
            pass

    class FailingRunner:
        public_concurrency = 2
        public_runner_factory = object()
        public_review_completed = ("ac1",)

        async def generate_public_verification(self, prompt, *, acceptance_criteria):
            raise plugin.artifacts.exhausted("trial budget refused")

        def public_review_progress(self, prompt, acceptance_criteria):
            return ("ac1",)

    criteria = (
        types.PactAcceptanceCriterion(id="ac1", text="First public criterion"),
        types.PactAcceptanceCriterion(id="ac2", text="Second public criterion"),
    )
    goal = types.PactGoalContract(
        schema="pact-runtime/goal-contract/v1",
        goal="Original task",
        acceptance_criteria=list(criteria),
        non_goals=[],
    )
    plan = types.PactInitialPlan(
        schema="pact-runtime/initial-plan/v1",
        constraints=[],
        missions=[
            {
                "id": "m1",
                "objective": "Original task",
                "target_ac_ids": ["ac1", "ac2"],
                "dependencies": [],
                "verification_intent": "Check both criteria",
            }
        ],
    )
    admission = Admission()
    context = SimpleNamespace(deadline=time.time() + 300, admission=admission)
    cap = PublicRepairCap(admission, plugin.artifacts.exhausted, attempts=3)
    pair = SimpleNamespace(goal_contract=goal, initial_plan=plan)

    with pytest.raises(plugin.artifacts.exhausted) as failure:
        await plugin._bounded_public_verification(
            FailingRunner(),
            context,
            cap,
            SimpleNamespace(rendered="Original task"),
            SimpleNamespace(delta=""),
            pair,
        )
    progress = public_review_failure(failure.value)
    assert progress is not None
    assert progress.reason == "trial_budget_refused"
    assert progress.completed_criteria == ("ac1",)
    assert progress.refused is True
    assert progress.deadline_unix == context.deadline


@pytest.mark.asyncio
async def test_shared_admission_limit_is_never_exceeded_by_parallel_lanes(tmp_path, git_repo_factory):
    from jiuwenswarm.long_horizon.clarification import capture_workspace, snapshot_workspace

    repo = git_repo_factory(tmp_path / "public-repo")
    snapshot_workspace_files(repo)
    snapshot = capture_workspace(repo, tmp_path / "frozen")
    workspace = snapshot_workspace(snapshot)
    criteria = tuple(types.PactAcceptanceCriterion(id=f"ac{i}", text=f"Public criterion {i}") for i in range(1, 5))
    worker = scripted_worker(tmp_path)
    context = SimpleNamespace(
        snapshot=snapshot,
        deadline=time.time() + 300,
        session_dir=tmp_path / "session",
        admission=SharedAdmission(deadline=time.time() + 300, limit=3),
        session_id="public-criterion-shared-limit",
    )
    plugin = plugin_for(worker, workspace)
    runner = await plugin.planning_runner(context, "Original full public task")
    with pytest.raises(plugin.artifacts.exhausted):
        await runner.generate_public_verification("Original full public task", acceptance_criteria=criteria)
    assert context.admission.refused == "test_cap"
    assert len(context.admission.admitted) <= 3


@pytest.mark.asyncio
async def test_bounded_verify_gate_degrades_contract_plan_failure_with_real_plugin(monkeypatch):
    from jiuwenswarm.lh.plugins.jiuwen import JiuwenPlugin
    from jiuwenswarm.lh.suite import _prepare_suite_bounded

    plugin = JiuwenPlugin(worker=SimpleNamespace())
    plugin._side_args = SimpleNamespace(
        workflow="suite", verify_command="python3 -m pytest", budget_seconds=120, profile_name="Code"
    )
    plugin._side_context = {"abort": threading.Event()}
    assert not plugin.requires_complete_public_review

    async def clarify(context, original_task):
        return SimpleNamespace(delta="")

    async def recall(context, original_task):
        return ""

    async def generate_pact_input(context, result, original_task, *, hints):
        raise TimeoutError("bounded planning window closed")

    async def localize(*args, **kwargs):
        return {"status": "disabled", "text": "", "artifacts": []}

    plugin.clarify = clarify
    plugin.recall = recall
    plugin.generate_pact_input = generate_pact_input
    monkeypatch.setattr("jiuwenswarm.lh.suite.localize_suite", localize)
    context = SimpleNamespace(snapshot=SimpleNamespace(roots=[]))
    _, prepared = await _prepare_suite_bounded(plugin, context, "Original task", planning_deadline=time.time() + 5)
    assert prepared["planning_status"] == "degraded_original_task"
    assert "public_verification" not in prepared


@pytest.mark.asyncio
async def test_public_review_outer_expiry_joins_every_child(tmp_path, git_repo_factory, monkeypatch):
    from jiuwenswarm.long_horizon.clarification import capture_workspace, snapshot_workspace

    import jiuwenswarm.lh.plugins.native as native_module
    from jiuwenswarm.lh.plugins.native import PublicRepairCap

    repo = git_repo_factory(tmp_path / "public-repo")
    snapshot_workspace_files(repo)
    snapshot = capture_workspace(repo, tmp_path / "frozen")
    workspace = snapshot_workspace(snapshot)
    criteria = tuple(types.PactAcceptanceCriterion(id=f"ac{i}", text=f"Public criterion {i}") for i in range(1, 5))
    worker = scripted_worker(tmp_path, block={"ac1", "ac2", "ac3", "ac4"})
    deadline = time.time() + 2.0
    admission = SharedAdmission(deadline=deadline)
    context = SimpleNamespace(
        snapshot=snapshot,
        deadline=deadline,
        session_dir=tmp_path / "session",
        admission=admission,
        session_id="public-criterion-outer-expiry",
    )
    plugin = plugin_for(worker, workspace)
    monkeypatch.setattr(native_module, "PUBLIC_VERIFICATION_DEADLINE_MARGIN_SECONDS", 0.0)
    runner = await plugin.planning_runner(context, "Original full public task")

    goal = types.PactGoalContract(
        schema="pact-runtime/goal-contract/v1",
        goal="Original full public task",
        acceptance_criteria=list(criteria),
        non_goals=[],
    )
    plan = types.PactInitialPlan(
        schema="pact-runtime/initial-plan/v1",
        constraints=[],
        missions=[
            {
                "id": "m1",
                "objective": "Original full public task",
                "target_ac_ids": [criterion.id for criterion in criteria],
                "dependencies": [],
                "verification_intent": "Check every criterion",
            }
        ],
    )
    pair = SimpleNamespace(goal_contract=goal, initial_plan=plan)
    cap = PublicRepairCap(admission, plugin.artifacts.exhausted, attempts=3)
    with pytest.raises(plugin.artifacts.exhausted) as failure:
        await plugin._bounded_public_verification(
            runner,
            context,
            cap,
            SimpleNamespace(rendered="Original full public task"),
            SimpleNamespace(delta=""),
            pair,
        )
    progress = public_review_failure(failure.value)
    assert progress is not None and progress.reason == "timeout_before_planning_deadline"
    assert progress.started and progress.completed_criteria == ()
    # The TaskGroup joined every lane before the outer timeout escaped.
    assert worker.active == 0
    assert len(admission.admitted) == len(admission.finished)
    assert any(event.is_set() for event in worker.started.values())


@pytest.mark.asyncio
async def test_public_review_capacity_is_recoverable_but_permission_is_not(
    tmp_path, git_repo_factory, monkeypatch
):
    import errno

    from jiuwenswarm.long_horizon.clarification import capture_workspace, snapshot_workspace

    from jiuwenswarm.lh.plugins.native import PublicRepairCap

    repo = git_repo_factory(tmp_path / "public-repo")
    snapshot_workspace_files(repo)
    snapshot = capture_workspace(repo, tmp_path / "frozen")
    workspace = snapshot_workspace(snapshot)
    criteria = tuple(types.PactAcceptanceCriterion(id=f"ac{i}", text=f"Public criterion {i}") for i in (1, 2))
    worker = scripted_worker(tmp_path)
    context = SimpleNamespace(
        snapshot=snapshot,
        deadline=time.time() + 300,
        session_dir=tmp_path / "session",
        admission=SharedAdmission(deadline=time.time() + 300),
        session_id="public-criterion-capacity",
    )
    plugin = plugin_for(worker, workspace)
    runner = await plugin.planning_runner(context, "Original full public task")
    goal = types.PactGoalContract(
        schema="pact-runtime/goal-contract/v1",
        goal="Original full public task",
        acceptance_criteria=list(criteria),
        non_goals=[],
    )
    plan = types.PactInitialPlan(
        schema="pact-runtime/initial-plan/v1",
        constraints=[],
        missions=[
            {
                "id": "m1",
                "objective": "Original full public task",
                "target_ac_ids": [criterion.id for criterion in criteria],
                "dependencies": [],
                "verification_intent": "Check both criteria",
            }
        ],
    )
    pair = SimpleNamespace(goal_contract=goal, initial_plan=plan)
    cap = PublicRepairCap(context.admission, plugin.artifacts.exhausted, attempts=3)
    arguments = (
        runner,
        context,
        cap,
        SimpleNamespace(rendered="Original full public task"),
        SimpleNamespace(delta=""),
        pair,
    )

    original_save = runner.checkpoint.save
    saves = []

    def capacity_save(stage, payload):
        saves.append(stage)
        if len(saves) == 2:
            raise OSError(errno.ENOSPC, "No space left on device")
        return original_save(stage, payload)

    monkeypatch.setattr(runner.checkpoint, "save", capacity_save)
    with pytest.raises(plugin.artifacts.exhausted) as failure:
        await plugin._bounded_public_verification(*arguments)
    progress = public_review_failure(failure.value)
    assert progress is not None and progress.reason == "storage_capacity"
    assert progress.completed_criteria
    assert list(runner.checkpoint.root.glob("*.json"))

    def permission_save(stage, payload):
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(runner.checkpoint, "save", permission_save)
    with pytest.raises(PlanningCheckpointError, match="storage failed"):
        await plugin._bounded_public_verification(*arguments)


@pytest.mark.asyncio
async def test_bounded_capacity_oserror_is_recoverable_and_permission_fails_closed(monkeypatch):
    import errno

    from jiuwenswarm.lh.suite import _prepare_suite_bounded

    async def localize(*args, **kwargs):
        return {"status": "disabled", "text": "", "artifacts": []}

    monkeypatch.setattr("jiuwenswarm.lh.suite.localize_suite", localize)
    context = SimpleNamespace(snapshot=SimpleNamespace(roots=[]))

    async def capacity(context, result, original_task, *, hints):
        raise OSError(errno.ENOSPC, "No space left on device")

    with pytest.raises(Exhausted) as failure:
        await _prepare_suite_bounded(
            _suite_plugin(generate_pact_input=capacity),
            context,
            "Original task",
            planning_deadline=time.time() + 5,
        )
    progress = public_review_failure(failure.value)
    assert progress is not None and progress.reason == "storage_capacity"

    async def permission(context, result, original_task, *, hints):
        raise OSError(errno.EACCES, "Permission denied")

    with pytest.raises(OSError) as denied:
        await _prepare_suite_bounded(
            _suite_plugin(generate_pact_input=permission),
            context,
            "Original task",
            planning_deadline=time.time() + 5,
        )
    assert denied.value.errno == errno.EACCES


def test_validate_preparation_refuses_bound_incomplete_review(tmp_path):
    from jiuwenswarm.lh.suite import validate_preparation

    controller = SimpleNamespace(_plugin=SimpleNamespace(artifacts=SimpleNamespace(exhausted=Exhausted)))
    state = SimpleNamespace(
        delivery=None,
        advisory={
            "long_horizon_suite": {
                "public_verification": {
                    "status": "unavailable",
                    "requires_complete_review": True,
                    "checks": [],
                    "degradation": {"reason": "timeout_before_planning_deadline"},
                }
            }
        },
    )
    with pytest.raises(Exhausted) as failure:
        validate_preparation(controller, tmp_path, state)
    progress = public_review_failure(failure.value)
    assert progress is not None and progress.reason == "timeout_before_planning_deadline"


async def _deliver_one_prior_cancel() -> asyncio.Task:
    """Leave a real, already-delivered cancellation on the caller task."""
    caller = asyncio.current_task()
    assert caller is not None
    caller.cancel()
    try:
        await asyncio.sleep(0)
    except asyncio.CancelledError:
        pass
    assert caller.cancelling() == 1
    return caller


@pytest.mark.asyncio
async def test_prior_delivered_cancel_does_not_mask_enospc(tmp_path, monkeypatch):
    import errno

    base = tmp_path / "prior-cancel-enospc"
    gate = LaneGate(4)
    children = [
        ArtifactRunner(base / f"child-{index}", tag=f"p{index}", gate=gate) for index in range(1, 5)
    ]
    cache = lane_cache(base, children, concurrency=4)
    capacity = OSError(errno.ENOSPC, "No space left on device")

    def capacity_save(stage, payload):
        raise capacity

    monkeypatch.setattr(cache.checkpoint, "save", capacity_save)
    caller = None

    async def invoke():
        nonlocal caller
        caller = await _deliver_one_prior_cancel()
        with pytest.raises(OSError) as failure:
            await cache.generate_public_verification("full prompt", acceptance_criteria=criterion_set())
        return failure.value

    task = asyncio.create_task(invoke())
    await asyncio.wait_for(gate.ready.wait(), 15)
    assert gate.started == gate.peak == 4
    gate.release.set()
    error = await asyncio.wait_for(task, 15)
    assert error is capacity
    assert error.errno == errno.ENOSPC
    assert caller is not None and caller.cancelling() == 1
    assert gate.active == 0
    assert not list(cache.checkpoint.root.glob("*.json"))


@pytest.mark.parametrize("integrity", [False, True])
@pytest.mark.asyncio
async def test_prior_delivered_cancel_overlaps_failure_and_new_cancel(tmp_path, integrity):
    base = tmp_path / f"prior-overlap-{int(integrity)}"
    gate = LaneGate(4)
    hold = asyncio.Event()
    sibling_cancelled = asyncio.Event()
    block = asyncio.Event()
    failure = (
        SideCallIntegrityError("frozen source changed")
        if integrity
        else ValueError("ordinary lane failure")
    )
    children = [
        ArtifactRunner(base / "child-1", tag="v1", failure=failure, gate=gate),
        ArtifactRunner(
            base / "child-2",
            tag="v2",
            gate=gate,
            wait_after_release=block,
            hold_cancel=hold,
            cancel_started=sibling_cancelled,
        ),
        ArtifactRunner(base / "child-3", tag="v3", gate=gate, wait_after_release=block, hold_cancel=hold),
        ArtifactRunner(base / "child-4", tag="v4", gate=gate, wait_after_release=block, hold_cancel=hold),
    ]
    cache = lane_cache(base, children, concurrency=4)
    caller = None

    async def invoke():
        nonlocal caller
        caller = await _deliver_one_prior_cancel()
        await cache.generate_public_verification("full prompt", acceptance_criteria=criterion_set())

    task = asyncio.create_task(invoke())
    await asyncio.wait_for(gate.ready.wait(), 15)
    gate.release.set()
    await asyncio.wait_for(sibling_cancelled.wait(), 15)
    assert children[0].raised == [failure]
    task.cancel()
    hold.set()
    if integrity:
        with pytest.raises(SideCallIntegrityError) as raised:
            await asyncio.wait_for(task, 15)
        assert raised.value is failure
    else:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 15)
        assert task.cancelled()
    assert caller is not None and caller.cancelling() == 2
    assert gate.active == 0


def test_eager_match_groups_must_exist_when_the_probe_requires_matches(shipping_public_model):
    def check(selection, *, pattern="(?P<value>x)(y)", prefix="", guard="if not rows: sys.exit(1)", loop="", prelude="", caught=False):
        body = f"import re, sys\n{prelude}rows = [{selection} for hit in re.finditer({pattern!r}, 'xy'){loop}]\n{guard}"
        if caught:
            body = "try:\n" + "\n".join("    " + line for line in body.splitlines()) + "\nexcept IndexError:\n    pass"
        command = prefix + "python3 - \"$log\" <<'PROBE'\n" + body + "\nPROBE"
        artifact = shipping_public_model.PublicVerification(
            schema="jiuwenswarm/pact-public-verification/v1", status="ready", reason="Read public records",
            checks=[dict(id="p", target_ac_ids=["ac1"], rationale="Measure the recorded groups", command=command)],
        )
        artifact.validate_generated_commands()

    with pytest.raises(ValueError, match=r"capture group 3 at line 2 \(command line 4\).*2 capture groups"):
        check("(hit.group('value'), int(hit.group(3)))", prefix="set -e\n")
    with pytest.raises(ValueError, match="capture group 'missing'"):
        check("hit.group('missing')", guard="assert rows")
    check("hit.group(0, 'value', 2)")
    check("hit.group(select_group())")
    check("hit.group(3)", guard="assert not rows", pattern="nomatch")
    check("hit.group(3) if False else hit.group(1)")
    check("hit.group(3)", loop=" if False")
    check("hit.group(3)", prefix="exit 0\n")
    check("hit.group(3)", prefix="set -n\n")
    check("hit.group(3)", prefix="trap 'exit 0' EXIT\n")
    check("hit.group(3)", prelude="sys.exit(0)\n")
    check("hit.group(3)", prelude="re = my_search_adapter\n")
    check("hit.group(3)", prelude="from local_api import sys\n")
    check("hit.group(3)", prelude="sys.exit = log_warning\n")
    check("hit.group(3)", caught=True)
    check("hit.group(3)", prelude="from unittest.mock import Mock, patch\nfixture=patch('re.finditer', return_value=[Mock()])\nfixture.start()\n")
    check("hit.group(3)", prelude="prepare_output_parser()\n")
    check("hit.group(3)", prelude="handle = configure_runtime()\n")
