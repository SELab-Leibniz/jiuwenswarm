# Copyright (c) 2026 Chrys. All rights reserved.

"""No-provider controls for exact own-Work native stage restoration."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from types import SimpleNamespace

import pytest

pytest.importorskip(
    "jiuwenswarm",
    reason=(
        "native Jiuwen tests require the external Jiuwen source; set JIUWEN_SOURCE and add it to "
        "PYTHONPATH (see docs/meridian-lh-runtime.md)"
    ),
)

from jiuwenswarm.long_horizon.clarification import types
from jiuwenswarm.long_horizon.clarification.public_checkpoint import PublicCriterionStore
from jiuwenswarm.long_horizon.clarification.service import validate_pact_runtime_input
from jiuwenswarm.long_horizon.clarification.verification import PublicVerification

from jiuwenswarm.lh.planning_checkpoint import PlanningCheckpoint, PlanningCheckpointError
from jiuwenswarm.lh.plugins.native_checkpoint import NativeStageRunner, owned_file
from jiuwenswarm.lh.side_calls import view_identity


class Exhausted(RuntimeError):
    pass


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(value))
    path.chmod(0o600)


def reviewed_files(root, criterion, value, identity, *, independent=True):
    """Synthetic public artifacts only; not an actual independent model review."""
    provenance = {"independent_review": True}
    for role, stage in (("generation", "public-verification-ac1"), ("review", "public-verification-review-ac1")):
        turns = []
        for phase in ("investigation", "synthesis"):
            session = f"{criterion.id}-{role}-{phase}" if independent else phase
            call = root / (criterion.id + "-" + stage + "-" + phase)
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
        path = root / (stage + "-" + criterion.id + ".json")
        write(path, receipt)
        provenance[role] = {
            "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "native_turns": turns,
            "investigation_session_id": turns[0]["session_id"],
            "synthesis_session_id": turns[1]["session_id"],
        }
        write(
            root / ("accepted-" + stage + "-0001-" + criterion.id + ".json"),
            {
                "stage": stage,
                "session_id": turns[1]["session_id"],
                "public_stage_receipt": provenance[role],
                "value": value.model_dump(mode="json", by_alias=True),
            },
        )
    PublicCriterionStore(root / "public-verification-checkpoints", identity).save(criterion, value, provenance)


class Runner:
    def __init__(self, root, *, stop=None, independent=True):
        self.output_dir, self.stop, self.independent = root, stop, independent
        self.calls = []
        self.runtime_evidence = ""
        self.large = False

    async def propose(self, prompt, *, sample_index):
        self.calls.append((prompt, "proposal"))
        anchor = {"kind": "original_requirement", "anchor": "original task"}
        proposal = types.ClarificationProposal(
            verdict="requirement_complete",
            rationale="Synthetic valid typed object",
            coverage=[
                {"target": t, "status": "found", "summary": "Synthetic coverage", "evidence": [anchor]}
                for t in ("primary", "adjacent")
            ],
            evidence=[anchor],
            guidance_points=[],
        )
        investigation = types.ProposalInvestigation(
            sample_index=sample_index,
            status="completed",
            investigation_attempts=1,
            synthesis_attempts=1,
            coverage_status="sufficient",
        )
        return types.ProposalModelResult(proposal, investigation, usage_details=({"total_tokens": 7},))

    async def generate_pact_goal_contract(self, prompt):
        self.calls.append((prompt, "goal"))
        return types.PactGoalContract(
            schema="pact-runtime/goal-contract/v1",
            goal="Original task",
            acceptance_criteria=list(criteria()),
            non_goals=[],
        ), {"total_tokens": 3}

    async def generate_pact_initial_plan(self, prompt):
        self.calls.append((prompt, "plan"))
        return types.PactInitialPlan(
            schema="pact-runtime/initial-plan/v1",
            constraints=[],
            missions=[
                {
                    "id": "m1",
                    "objective": "Original task",
                    "target_ac_ids": ["ac1", "ac2"],
                    "dependencies": [],
                    "verification_intent": "Check all task obligations",
                }
            ],
        ), {"total_tokens": 4}

    async def generate_public_verification(self, prompt, *, acceptance_criteria):
        (criterion,) = acceptance_criteria
        self.calls.append((prompt, criterion.id))
        if self.stop == criterion.id:
            raise Exhausted("synthetic window ended")
        value = PublicVerification(
            schema="jiuwenswarm/pact-public-verification/v1",
            status="ready",
            reason="synthetic",
            checks=[
                {
                    "id": "original-id",
                    "command": "python3 -c 'assert 1 == 1'",
                    "target_ac_ids": [criterion.id],
                    "rationale": "Synthetic command validity only, not benchmark verification",
                }
            ],
        )
        if self.large:
            value = value.model_copy(
                update={
                    "checks": [
                        value.checks[0].model_copy(
                            update={"id": f"original-{n}", "command": "python3 -c 'assert 1 == 1' #" + "s" * 4000}
                        )
                        for n in range(100)
                    ]
                }
            )
        reviewed_files(
            self.output_dir, criterion, value, self.expected_identity(prompt, criterion), independent=self.independent
        )
        return value.model_copy(
            update={
                "checks": [check.model_copy(update={"id": f"ac1-check{n}"}) for n, check in enumerate(value.checks, 1)]
            }
        ), {"total_tokens": 3}


def fixture(tmp_path, *, binding="same-work", stop=None, independent=True, ordinal=1):
    source = tmp_path / "s0"
    source.mkdir(exist_ok=True)
    (source / "public.txt").write_text("Frozen public task\n")
    snapshot = SimpleNamespace(
        roots=[
            SimpleNamespace(
                view_root=str(source), source_root=str(source), label="public", is_primary=True, git_head=None
            )
        ],
        manifest_hash="a" * 64,
    )
    context = SimpleNamespace(snapshot=snapshot, deadline=time.time() + 300, session_dir=tmp_path)
    native = Runner(tmp_path / f"native-{ordinal}", stop=stop, independent=independent)
    cache = NativeStageRunner(
        runner=native,
        checkpoint=PlanningCheckpoint(tmp_path / "session", {"work": binding}, max_bytes=16 * 1024 * 1024),
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
    )
    native.expected_identity = cache.public_identity
    return cache, native


def criteria():
    return tuple(types.PactAcceptanceCriterion(id=f"ac{i}", text=f"Public criterion {i}") for i in (1, 2))


@pytest.mark.asyncio
async def test_partial_resume_preserves_reviewed_criterion_and_has_no_reused_usage(tmp_path):
    cache, native = fixture(tmp_path, stop="ac2")
    with pytest.raises(Exhausted):
        await cache.generate_public_verification("full original prompt", acceptance_criteria=criteria())
    resumed, second = fixture(tmp_path, ordinal=2)
    value, usage = await resumed.generate_public_verification("full original prompt", acceptance_criteria=criteria())
    assert [c for _, c in native.calls] == ["ac1", "ac2"]
    assert [c for _, c in second.calls] == ["ac2"]
    assert usage["total_tokens"] == 3
    assert [c.id for c in value.checks] == ["ac1-check1", "ac2-check1"]
    assert [c.target_ac_ids for c in value.checks] == [["ac1"], ["ac2"]]
    final, third = fixture(tmp_path, ordinal=3)
    _, usage = await final.generate_public_verification("full original prompt", acceptance_criteria=criteria())
    assert third.calls == [] and usage == {}


@pytest.mark.parametrize("change", ["work", "prompt", "order", "content"])
@pytest.mark.asyncio
async def test_full_identity_change_does_not_reuse(tmp_path, change):
    cache, _ = fixture(tmp_path)
    await cache.generate_public_verification("full prompt", acceptance_criteria=criteria())
    resumed, native = fixture(tmp_path, binding="other-work" if change == "work" else "same-work", ordinal=2)
    ids = criteria()
    if change == "order":
        ids = tuple(reversed(ids))
    if change == "content":
        ids = (ids[0].model_copy(update={"text": "Changed explicit task requirement"}), ids[1])
    await resumed.generate_public_verification(
        "changed prompt" if change == "prompt" else "full prompt", acceptance_criteria=ids
    )
    assert len(native.calls) == 2


@pytest.mark.parametrize("drift", ["receipt", "missing", "checkpoint", "source", "expired", "cancelled"])
@pytest.mark.asyncio
async def test_tampered_provenance_or_expired_budget_never_reuses(tmp_path, drift):
    cache, _ = fixture(tmp_path)
    await cache.generate_public_verification("full", acceptance_criteria=criteria()[:1])
    resumed, native = fixture(tmp_path, ordinal=2)
    if drift == "receipt":
        p = next((tmp_path / "native-1").glob("accepted-*.json"))
        p.write_text("{}")
    elif drift == "missing":
        next((tmp_path / "native-1").glob("accepted-*.json")).unlink()
    elif drift == "checkpoint":
        next((tmp_path / "session/completed-planning").glob("*.json")).write_text("{}")
    elif drift == "source":
        (tmp_path / "s0/public.txt").write_text("mutated")
    elif drift == "expired":
        resumed.context.deadline = time.time() - 1
    else:
        resumed.abort.set()
    with pytest.raises((PlanningCheckpointError, Exhausted)):
        await resumed.generate_public_verification("full", acceptance_criteria=criteria()[:1])
    assert native.calls == []


@pytest.mark.asyncio
async def test_shared_generation_review_session_cannot_be_cached(tmp_path):
    cache, _ = fixture(tmp_path, independent=False)
    with pytest.raises(PlanningCheckpointError, match="independent"):
        await cache.generate_public_verification("full", acceptance_criteria=criteria()[:1])
    assert not list((tmp_path / "session/completed-planning").glob("*.json"))


def test_unsafe_receipt_paths_reject(tmp_path):
    (tmp_path / "root").mkdir()
    (tmp_path / "outside").write_text("private")
    (tmp_path / "root/link").symlink_to(tmp_path / "outside")
    for path in [tmp_path / "root/link", tmp_path / "root/../outside"]:
        with pytest.raises(PlanningCheckpointError):
            owned_file(tmp_path / "root", path)


def test_receipt_alias_above_owned_root_is_allowed(tmp_path):
    actual = tmp_path / "actual"
    (actual / "root").mkdir(parents=True)
    (actual / "root/proof.json").write_text("{}")
    alias = tmp_path / "alias"
    alias.symlink_to(actual, target_is_directory=True)
    assert owned_file(alias / "root", alias / "root/proof.json") == b"{}"


def test_preexisting_loose_checkpoint_directory_is_refused_without_chmod(tmp_path):
    folder = tmp_path / "completed-planning"
    folder.mkdir(mode=0o755)
    folder.chmod(0o755)
    with pytest.raises(PlanningCheckpointError, match="owner-only"):
        PlanningCheckpoint(tmp_path, {"work": "same"})
    assert folder.stat().st_mode & 0o077


def test_failed_atomic_publication_leaves_no_corrupt_checkpoint(tmp_path, monkeypatch):
    cache = PlanningCheckpoint(tmp_path, {"work": "same"})
    monkeypatch.setattr(
        "jiuwenswarm.lh.planning_checkpoint.os.link", lambda *args: (_ for _ in ()).throw(OSError("injected"))
    )
    with pytest.raises(OSError):
        cache.save("stage", {"ready": True})
    assert cache.read("stage") is None
    assert list(cache.root.iterdir()) == []


@pytest.mark.asyncio
async def test_native_typed_proposal_goal_and_plan_survive_runner_recreation(tmp_path):
    cache, native = fixture(tmp_path)
    proposal = await cache.propose("proposal", sample_index=1)
    assert proposal.proposal is not None and proposal.usage_details
    await cache.generate_pact_goal_contract("goal")
    await cache.generate_pact_initial_plan("plan")
    resumed, fresh = fixture(tmp_path, ordinal=2)
    result = await resumed.propose("proposal", sample_index=1)
    assert result.proposal == proposal.proposal and result.usage_details == ()
    _, usage = await resumed.generate_pact_goal_contract("goal")
    assert usage == {}
    _, usage = await resumed.generate_pact_initial_plan("plan")
    assert usage == {} and fresh.calls == [] and len(native.calls) == 3


@pytest.mark.asyncio
async def test_invalid_proposal_is_not_frozen_as_a_success(tmp_path):
    cache, native = fixture(tmp_path)
    original = native.propose

    async def invalid(*args, **kwargs):
        result = await original(*args, **kwargs)
        return types.ProposalModelResult(None, result.investigation, error="synthetic failure")

    native.propose = invalid
    value = await cache.propose("proposal", sample_index=1)
    assert value.proposal is None
    assert list(cache.checkpoint.root.glob("*.json")) == []


@pytest.mark.asyncio
async def test_valid_json_hash_cannot_bypass_typed_schema(tmp_path):
    cache, _ = fixture(tmp_path)
    await cache.generate_pact_goal_contract("goal")
    path = next(cache.checkpoint.root.glob("*.json"))
    row = json.loads(path.read_text())
    row["payload"]["value"]["goal"] = 42
    from jiuwenswarm.lh.planning_checkpoint import _bytes

    row["payload_sha256"] = hashlib.sha256(_bytes(row["payload"])).hexdigest()
    path.write_text(json.dumps(row))
    resumed, native = fixture(tmp_path, ordinal=2)
    with pytest.raises(PlanningCheckpointError):
        await resumed.generate_pact_goal_contract("goal")
    assert native.calls == []


@pytest.mark.asyncio
async def test_plugin_reconstruction_restores_completed_typed_service_result(tmp_path, monkeypatch):
    from jiuwenswarm.long_horizon.clarification import event_evidence

    from jiuwenswarm.lh.plugins.jiuwen import JiuwenPlugin

    context_cache, _ = fixture(tmp_path)
    context = context_cache.context
    context.snapshot.manifest_hash = "a" * 64
    context.session_dir = tmp_path / "plugin-session"
    context.session_id = "same-work"
    monkeypatch.setattr(event_evidence, "validate_model_view", lambda snapshot: None)
    runners = []

    class Service:
        def __init__(self, runner):
            self.runner = runner

        async def clarify(self, **kwargs):
            values = [await self.runner.propose("fixed proposal", sample_index=i) for i in (1, 2, 3)]
            return types.ClarificationResult(
                strategy_version="synthetic-completed-service",
                revision=1,
                delta="",
                selection=types.ClarificationSelection(),
                proposals=tuple(v.proposal for v in values),
                proposal_sample_indices=(1, 2, 3),
                investigations=tuple(v.investigation for v in values),
                usage_details=tuple(u for v in values for u in v.usage_details),
            )

    def plugin():
        value = JiuwenPlugin(worker=SimpleNamespace())
        value._side_args = SimpleNamespace(workflow="suite", profile_name="Code")
        value._side_binding = SimpleNamespace(
            fingerprint="same-frozen-native-binding",
            summary={"source_commit": "fixed", "binary_sha256": "b" * 64, "config_template_sha256": "c" * 64},
        )
        value._side_context = {"abort": threading.Event()}
        value.configuration = lambda profile: {"profile": profile, "source_commit": "fixed"}
        value.evidence_configuration = lambda: {"memory": "same-frozen-bundle"}

        def runner(context):
            item = Runner(tmp_path / ("native-plugin-" + str(len(runners))))
            runners.append(item)
            return item

        value.default_runner = runner
        value._service = Service
        return value

    first = await plugin().clarify(context, "Original exact task")
    second = await plugin().clarify(context, "Original exact task")
    assert first.proposals == second.proposals
    assert first.usage_details and second.usage_details == ()
    assert len(runners[0].calls) == 3 and runners[1].calls == []


@pytest.mark.parametrize("damage", ["invalid-json", "unsafe-mode", "changed-value"])
@pytest.mark.asyncio
async def test_corrupt_proposal_stops_real_service_before_aggregate_checkpoint(tmp_path, monkeypatch, damage):
    from unittest.mock import AsyncMock

    from jiuwenswarm.long_horizon.clarification.budget import SideCallIntegrityError
    from jiuwenswarm.long_horizon.clarification.prompts import build_proposal_prompt

    from jiuwenswarm.lh.plugins.jiuwen import JiuwenPlugin
    from jiuwenswarm.lh.plugins.native_checkpoint import canonical

    cache, native = fixture(tmp_path)
    task, evidence = "Original exact task", "Frozen public evidence"
    keys = []
    for index in (1, 2, 3):
        prompt = build_proposal_prompt(task, "", evidence, index)
        await cache.propose(prompt, sample_index=index)
        keys.append("proposal:" + hashlib.sha256(canonical({"prompt": prompt, "sample": index})).hexdigest())
    damaged = cache.checkpoint._path(keys[0])
    intact = {key: cache.checkpoint._path(key).read_bytes() for key in keys[1:]}
    if damage == "invalid-json":
        damaged.write_text("{")
    elif damage == "unsafe-mode":
        damaged.chmod(0o644)
    else:
        row = json.loads(damaged.read_bytes())
        row["payload"]["value"]["proposal"]["rationale"] = "Unattested replacement"
        damaged.write_text(json.dumps(row))
    damaged_bytes = damaged.read_bytes()
    native.calls.clear()
    plugin = JiuwenPlugin(worker=SimpleNamespace())
    monkeypatch.setattr(plugin, "planning_runner", AsyncMock(return_value=cache))
    monkeypatch.setattr(plugin, "frozen_input_options", AsyncMock(return_value={"base_evidence": evidence}))

    # Use the real native concurrent proposal service and the real host aggregate
    # checkpoint wrapper. Two valid proposals must not hide unsafe evidence.
    with pytest.raises(SideCallIntegrityError) as failure:
        await plugin.clarify(cache.context, task)
    assert isinstance(failure.value.__cause__, PlanningCheckpointError)
    aggregate_key = "completed-clarification:" + hashlib.sha256(canonical(task)).hexdigest()
    assert cache.checkpoint.read(aggregate_key) is None
    assert native.calls == [] and cache.calls.active == {}
    assert plugin._active_clarification is None
    assert damaged.read_bytes() == damaged_bytes
    assert all(cache.checkpoint._path(key).read_bytes() == raw for key, raw in intact.items())


@pytest.mark.parametrize("stage", ["goal", "plan"])
@pytest.mark.parametrize("damage", ["invalid-json", "unsafe-mode", "changed-value"])
@pytest.mark.asyncio
async def test_bounded_preparation_does_not_degrade_corrupt_planning_checkpoint(tmp_path, monkeypatch, stage, damage):
    from unittest.mock import AsyncMock

    from jiuwenswarm.lh.plugins.jiuwen import JiuwenPlugin
    from jiuwenswarm.lh.plugins.native_checkpoint import canonical
    from jiuwenswarm.lh.suite import prepare_suite

    cache, native = fixture(tmp_path)
    task = "Original exact task"
    result = types.ClarificationResult(
        strategy_version="synthetic-completed-service",
        revision=1,
        delta="",
        selection=types.ClarificationSelection(),
    )
    plugin = JiuwenPlugin(worker=SimpleNamespace())
    plugin._side_args = SimpleNamespace(verify_command="python3 -m pytest")
    monkeypatch.setattr(plugin, "planning_runner", AsyncMock(return_value=cache))
    monkeypatch.setattr(plugin, "frozen_input_options", AsyncMock(return_value={"base_evidence": "Frozen evidence"}))
    monkeypatch.setattr(plugin, "clarify", AsyncMock(return_value=result))
    monkeypatch.setattr(plugin, "recall", AsyncMock(return_value=""))
    monkeypatch.setattr(
        "jiuwenswarm.lh.suite.localize_suite",
        AsyncMock(return_value={"status": "unavailable", "text": "", "artifacts": []}),
    )
    # Prime real typed checkpoints through the native Goal/Plan service, then
    # exercise the full bounded host preparation with the damaged checkpoint.
    await plugin.generate_pact_input(cache.context, result, task, hints="")
    prompt = next(prompt for prompt, called_stage in native.calls if called_stage == stage)
    key = stage + ":" + hashlib.sha256(canonical(prompt)).hexdigest()
    damaged = cache.checkpoint._path(key)
    if damage == "invalid-json":
        damaged.write_text("{")
    elif damage == "unsafe-mode":
        damaged.chmod(0o644)
    else:
        row = json.loads(damaged.read_bytes())
        row["payload"]["value"]["unattested"] = "replacement"
        damaged.write_text(json.dumps(row))
    preserved = {path: path.read_bytes() for path in cache.checkpoint.root.glob("*.json")}
    native.calls.clear()

    expected = PlanningCheckpointError if stage == "goal" else cache.integrity_error
    with pytest.raises(expected) as failure:
        await prepare_suite(plugin, cache.context, task, optional_seconds=30, planning_deadline=time.time() + 30)
    if stage == "plan":
        assert isinstance(failure.value.__cause__, PlanningCheckpointError)
    assert native.calls == [] and cache.calls.active == {}
    assert plugin._active_clarification is None
    assert {path: path.read_bytes() for path in cache.checkpoint.root.glob("*.json")} == preserved


def test_checkpoint_alias_above_trusted_root_is_allowed(tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(actual, target_is_directory=True)
    cache = PlanningCheckpoint(alias / "session", {"work": "same"})
    cache.save("stage", {"value": "verified"})
    assert cache.read("stage") == {"value": "verified"}
    assert not cache.root.is_symlink()


@pytest.mark.asyncio
async def test_real_plugin_snapshot_mutation_precedes_default_runner_expired_budget(tmp_path, git_repo_factory):
    from jiuwenswarm.long_horizon.clarification import capture_workspace, snapshot_workspace

    from jiuwenswarm.lh.plugins.jiuwen import JiuwenPlugin

    repo = git_repo_factory(tmp_path / "public-repo")
    snapshot = capture_workspace(repo, tmp_path / "native-s0")
    target = snapshot_workspace(snapshot) / "src/README.md"
    target.chmod(0o600)  # Deliberately simulate an owner mutating this new test-only snapshot.
    target.write_text("Modified frozen view")
    plugin = JiuwenPlugin(worker=SimpleNamespace())
    plugin._side_args = SimpleNamespace(workflow="suite")
    context = SimpleNamespace(snapshot=snapshot, deadline=time.time() - 1, session_dir=tmp_path / "session")
    with pytest.raises(PlanningCheckpointError, match="frozen source changed"):
        await plugin.planning_runner(context, "Original task")


@pytest.mark.parametrize("stop", ["expired", "cancelled"])
@pytest.mark.asyncio
async def test_snapshot_integrity_wins_over_an_expired_or_cancelled_window(tmp_path, stop):
    cache, native = fixture(tmp_path)
    (tmp_path / "s0/public.txt").write_text("mutated during a stopping call")
    if stop == "expired":
        cache.context.deadline = time.time() - 1
    else:
        cache.abort.set()
    with pytest.raises(PlanningCheckpointError, match="source changed"):
        await cache.generate_pact_goal_contract("goal")
    assert native.calls == []


def test_crash_leftover_hardlink_checkpoint_rejects_as_integrity(tmp_path):
    import os

    cache = PlanningCheckpoint(tmp_path, {"work": "same"})
    cache.save("completed", {"value": "valid"})
    os.link(cache._path("completed"), cache.root / ".planning-crash-leftover.tmp")
    with pytest.raises(PlanningCheckpointError):
        cache.read("completed")


@pytest.mark.asyncio
async def test_large_valid_native_criterion_can_be_restored(tmp_path):
    cache, native = fixture(tmp_path)
    native.large = True
    value, _ = await cache.generate_public_verification("large", acceptance_criteria=criteria()[:1])
    assert len(value.checks) == 100
    assert next(cache.checkpoint.root.glob("*.json")).stat().st_size > 200_000
    second, fresh = fixture(tmp_path, ordinal=2)
    restored, usage = await second.generate_public_verification("large", acceptance_criteria=criteria()[:1])
    assert restored == value and usage == {} and fresh.calls == []


@pytest.mark.parametrize("field", ["prompt_sha256", "snapshot_manifest_hash", "worker_identity", "acceptance_criteria"])
@pytest.mark.asyncio
async def test_native_record_must_self_bind_current_inputs(tmp_path, field):
    cache, native = fixture(tmp_path)
    expected = native.expected_identity

    def substitute(prompt, criterion):
        identity = expected(prompt, criterion)
        identity[field] = {"other": "runtime"} if field == "worker_identity" else "f" * 64
        return identity

    native.expected_identity = substitute
    with pytest.raises(PlanningCheckpointError, match="current public inputs"):
        await cache.generate_public_verification("full", acceptance_criteria=criteria()[:1])
    assert not list(cache.checkpoint.root.glob("*.json"))


@pytest.mark.asyncio
async def test_expected_public_identity_matches_pinned_native_augmentation(tmp_path, monkeypatch):
    from jiuwenswarm.long_horizon.clarification import model

    cache, fake = fixture(tmp_path)
    fake.runtime_evidence = "Verified public executable capability"
    cache.context.snapshot.roots[0].git_head = "c" * 40
    declared = {"scope": "own-work-stages/v1", "work_scope": str(tmp_path)}
    cache.expected_worker_identity = {
        "declared": declared,
        "actual": {"worker_type": "types.SimpleNamespace", "source_commit": "fixed"},
        "read_only": True,
        "investigation_tool_policy": "configured",
        "synthesis_tool_policy": "none",
    }
    criterion = criteria()[0]
    value = PublicVerification(
        schema="jiuwenswarm/pact-public-verification/v1",
        status="ready",
        reason="synthetic",
        checks=[
            {
                "id": "example",
                "command": "python3 -c 'assert 1 == 1'",
                "target_ac_ids": [criterion.id],
                "rationale": "Synthetic public identity control",
            }
        ],
    )
    # Only this synthetic view check is replaced. The real native public method
    # must recognize the exact store identity and return without any worker.run.
    monkeypatch.setattr(model, "validate_model_view", lambda snapshot: None)
    native = model.JiuwenClarificationModel(
        worker=SimpleNamespace(source_commit="fixed"),
        snapshot=cache.context.snapshot,
        output_dir=fake.output_dir,
        runtime_evidence=fake.runtime_evidence,
        checkpoint_identity=declared,
    )
    fake.public_check_repository_context = getattr(native, "public_check_repository_context", "")
    fake.public_check_instructions_sha256 = getattr(native, "public_check_instructions_sha256", None)
    reviewed_files(fake.output_dir, criterion, value, cache.public_identity("exact full prompt", criterion))
    output, usage = await native.generate_public_verification("exact full prompt", acceptance_criteria=(criterion,))
    assert output.checks[0].target_ac_ids == [criterion.id]
    assert output.checks[0].id == "ac1-check1"
    assert not any(usage.values())


@pytest.mark.parametrize("invalid", ["unavailable", "duplicate_ids"])
@pytest.mark.asyncio
async def test_public_aggregation_cannot_promote_an_invalid_native_value(tmp_path, invalid):
    cache, native = fixture(tmp_path)
    original = native.generate_public_verification

    async def invalid_value(*args, **kwargs):
        value, usage = await original(*args, **kwargs)
        if invalid == "unavailable":
            return value.model_copy(update={"status": "unavailable"}), usage
        return value.model_copy(update={"checks": [value.checks[0], value.checks[0]]}), usage

    native.generate_public_verification = invalid_value
    with pytest.raises(PlanningCheckpointError, match="own contract"):
        await cache.generate_public_verification("full", acceptance_criteria=criteria()[:1])
    assert list(cache.checkpoint.root.glob("*.json")) == []


class ScriptedPublicWorker:
    """Scripted native transport that emits the real public stage receipts."""

    def __init__(self, *, source_commit, binary=None, config_template=None):
        self.source_commit = source_commit
        if binary is not None:
            self.binary = binary
        if config_template is not None:
            self.config_template = config_template
        self.calls = []

    async def run(self, **kwargs):
        import re

        self.calls.append(kwargs["stage"])
        session = "scripted-session-" + str(len(self.calls))
        events = []
        if kwargs.get("tool_policy") != "none":
            for number, (name, args, result) in enumerate(
                [
                    ("grep", {"pattern": "value", "path": "."}, "src/api.py:1:def value():"),
                    ("read_file", {"file_path": "src/api.py"}, (kwargs["workspace"] / "src/api.py").read_text()),
                    (
                        "read_file",
                        {"file_path": "tests/test_api.py"},
                        (kwargs["workspace"] / "tests/test_api.py").read_text(),
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
                            "payload": {"tool_call": {"tool_call_id": call_id, "name": name, "arguments": args}},
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
            match = re.search(r"Generate checks only for this acceptance criterion: (\{[^\n]+\})", kwargs["prompt"])
            criterion = json.loads(match.group(1))["id"]
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
        call = kwargs["output_dir"]
        write(
            call / "metadata.json",
            {
                "stage": kwargs["stage"],
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


@pytest.mark.asyncio
async def test_real_native_fresh_generation_writes_accepted_proofs_and_wrapper_resumes(tmp_path, git_repo_factory):
    """Real pinned model/service/store code; only worker responses are scripted."""
    from jiuwenswarm.long_horizon.clarification import JiuwenClarificationModel, capture_workspace

    repo = git_repo_factory(tmp_path / "public-repo")
    (repo / "src/api.py").write_text("def value():\n    return 1\n")
    (repo / "tests").mkdir()
    (repo / "tests/test_api.py").write_text("def test_value():\n    assert True\n")
    snapshot = capture_workspace(repo, tmp_path / "frozen")

    context = SimpleNamespace(snapshot=snapshot, deadline=time.time() + 300, session_dir=tmp_path)
    worker = ScriptedPublicWorker(source_commit="scripted-public-boundary")
    declared = {"scope": "own-work-stages/v1", "work_scope": str(tmp_path)}
    expected = {
        "declared": declared,
        "actual": {
            "worker_type": type(worker).__module__ + "." + type(worker).__qualname__,
            "source_commit": worker.source_commit,
        },
        "read_only": True,
        "investigation_tool_policy": "configured",
        "synthesis_tool_policy": "none",
    }

    def wrapper(ordinal):
        native = JiuwenClarificationModel(
            worker=worker,
            snapshot=snapshot,
            output_dir=tmp_path / f"real-native-{ordinal}",
            checkpoint_identity=declared,
        )
        return NativeStageRunner(
            runner=native,
            checkpoint=PlanningCheckpoint(tmp_path / "real-session", {"work": "same"}),
            context=context,
            types=types,
            verification=PublicVerification,
            store_type=PublicCriterionStore,
            validate_pair=validate_pact_runtime_input,
            stopped=Exhausted,
            exhausted=Exhausted,
            abort=threading.Event(),
            snapshot_identity=view_identity(snapshot),
            expected_worker_identity=expected,
        )

    first = wrapper(1)
    value, usage = await first.generate_public_verification(
        "Original full public task", acceptance_criteria=criteria()[:1]
    )
    assert len(worker.calls) == 4 and usage["input_tokens"] == 8
    assert len(list(first.runner.output_dir.glob("accepted-*.json"))) == 2
    second = wrapper(2)
    restored, usage = await second.generate_public_verification(
        "Original full public task", acceptance_criteria=criteria()[:1]
    )
    assert restored == value and usage == {} and len(worker.calls) == 4


def production_plugin(worker, workspace):
    from jiuwenswarm.lh.plugins.jiuwen import JiuwenPlugin

    args = SimpleNamespace(workflow="suite", profile_name="Code")
    plugin = JiuwenPlugin(worker=worker)
    plugin._side_args = args
    plugin._side_context = {"abort": threading.Event()}
    binding = plugin.resolve_execution(workspace, args)
    plugin._side_binding = binding
    return plugin, binding


async def public_through_plugin(plugin, context, prompt, criterion):
    runner = await plugin.planning_runner(context, prompt)
    value, usage = await runner.generate_public_verification(prompt, acceptance_criteria=(criterion,))
    return runner, value, usage


@pytest.mark.parametrize("source_commit", ["", "a" * 40])
@pytest.mark.asyncio
async def test_real_plugin_planning_runner_binds_supported_source_identity(tmp_path, git_repo_factory, source_commit):
    """The actual plugin entry accepts and resumes the pinned producer's identity shape."""
    from jiuwenswarm.long_horizon.clarification import capture_workspace, snapshot_workspace

    repo = git_repo_factory(tmp_path / "public-repo")
    (repo / "src/api.py").write_text("def value():\n    return 1\n")
    (repo / "tests").mkdir()
    (repo / "tests/test_api.py").write_text("def test_value():\n    assert True\n")
    snapshot = capture_workspace(repo, tmp_path / "frozen")
    workspace = snapshot_workspace(snapshot)
    binary = tmp_path / "native-entry"
    binary.write_text("native launcher bytes\n")
    config_template = tmp_path / "config.yaml"
    config_template.write_text("model: pinned\n")
    worker = ScriptedPublicWorker(source_commit=source_commit, binary=binary, config_template=config_template)
    context = SimpleNamespace(snapshot=snapshot, deadline=time.time() + 300, session_dir=tmp_path, admission=None)
    prompt, criterion = "Original full public task", criteria()[0]

    first, binding = production_plugin(worker, workspace)
    runner, value, usage = await public_through_plugin(first, context, prompt, criterion)
    actual = runner.expected_worker_identity["actual"]
    assert actual["worker_type"] == f"{type(worker).__module__}.{type(worker).__qualname__}"
    assert actual["binary_sha256"] == hashlib.sha256(binary.read_bytes()).hexdigest()
    assert actual["config_template_sha256"] == hashlib.sha256(config_template.read_bytes()).hexdigest()
    if source_commit:
        assert actual["source_commit"] == source_commit
    else:
        assert "source_commit" not in actual
    assert len(worker.calls) == 4 and usage["input_tokens"] == 8
    record = json.loads(
        next(
            path
            for root in [runner.runner.output_dir, *runner.runner.output_dir.parent.glob("*")]
            for path in (root / "public-verification-checkpoints").glob("*/*.json")
        ).read_text()
    )
    assert record["identity"]["worker_identity"]["actual"] == actual

    second, resumed_binding = production_plugin(worker, workspace)
    assert resumed_binding.fingerprint == binding.fingerprint
    resumed, restored, resumed_usage = await public_through_plugin(second, context, prompt, criterion)
    assert resumed.expected_worker_identity["actual"] == actual
    assert restored == value and resumed_usage == {} and len(worker.calls) == 4


@pytest.mark.parametrize("field", ["source_commit", "binary_sha256", "config_template_sha256"])
@pytest.mark.asyncio
async def test_real_plugin_planning_runner_rejects_independent_identity_mismatch(tmp_path, git_repo_factory, field):
    """A nonempty source or digest tamper cannot be skipped by an older reviewed record."""
    from jiuwenswarm.long_horizon.clarification import capture_workspace, snapshot_workspace

    repo = git_repo_factory(tmp_path / "public-repo")
    (repo / "src/api.py").write_text("def value():\n    return 1\n")
    (repo / "tests").mkdir()
    (repo / "tests/test_api.py").write_text("def test_value():\n    assert True\n")
    snapshot = capture_workspace(repo, tmp_path / "frozen")
    workspace = snapshot_workspace(snapshot)
    binary = tmp_path / "native-entry"
    binary.write_text("native launcher bytes\n")
    config_template = tmp_path / "config.yaml"
    config_template.write_text("model: pinned\n")
    worker = ScriptedPublicWorker(source_commit="a" * 40, binary=binary, config_template=config_template)
    context = SimpleNamespace(snapshot=snapshot, deadline=time.time() + 300, session_dir=tmp_path, admission=None)
    prompt, criterion = "Original full public task", criteria()[0]

    first, binding = production_plugin(worker, workspace)
    runner, value, usage = await public_through_plugin(first, context, prompt, criterion)
    assert len(worker.calls) == 4 and usage["input_tokens"] == 8

    second, resumed_binding = production_plugin(worker, workspace)
    assert resumed_binding.fingerprint == binding.fingerprint
    second._side_binding.summary[field] = {
        "source_commit": "b" * 40,
        "binary_sha256": "f" * 64,
        "config_template_sha256": "e" * 64,
    }[field]
    resumed = await second.planning_runner(context, prompt)
    with pytest.raises(PlanningCheckpointError, match="current public inputs/runtime"):
        await resumed.generate_public_verification(prompt, acceptance_criteria=(criterion,))
    assert len(worker.calls) == 4


@pytest.mark.parametrize(
    ("mode", "status", "first_selects", "resumed_slots", "resumed_selects"),
    [
        ("selected", "completed", 1, [], 0),
        ("rejected", "completed", 2, [], 0),
        ("no_candidates", "completed", 0, [], 0),
        ("invalid_selector", "degraded", 2, [1, 2], 2),
        ("no_proposal", "degraded", 0, [1, 2, 3], 0),
    ],
)
@pytest.mark.asyncio
async def test_resume_single_proposal_after_native_selection(
    tmp_path, mode, status, first_selects, resumed_slots, resumed_selects
):
    from dataclasses import replace
    from unittest.mock import AsyncMock, Mock

    from jiuwenswarm.lh.plugins.jiuwen import JiuwenPlugin

    async def clarify(ordinal):
        cache, native = fixture(tmp_path, ordinal=ordinal)
        propose = native.propose
        slots = []

        async def one_valid(prompt, *, sample_index):
            slots.append(sample_index)
            result = await propose(prompt, sample_index=sample_index)
            if sample_index != 3 or mode == "no_proposal":
                failed = result.investigation.model_copy(update={"status": "failed", "coverage_status": "insufficient"})
                return replace(result, proposal=None, investigation=failed)
            if mode == "no_candidates":
                return result
            point = types.ProposalGuidancePoint(
                category="compatibility", statement="Preserve the documented ordering of existing responses.",
                confidence=0.95, basis="current_repo", contract_cell="compatibility_scope",
                decision_impact="Callers depend on the response order.", evidence=result.proposal.evidence,
                risk="Reordering responses breaks existing callers.",
            )
            return replace(result, proposal=result.proposal.model_copy(
                update={"verdict": "clarification_needed", "guidance_points": [point]}
            ))

        native.propose = one_valid
        review = types.SelectorCandidateReview(
            candidate_id="p3-g1" if mode == "invalid_selector" else "p1-g1",
            decision="reject" if mode == "rejected" else "select", rationale="Examined the candidate.",
        )
        native.select = AsyncMock(return_value=(types.ClarificationSelectorDecision(reviews=[review]), {}))
        native.record_selector_pass = Mock()
        plugin = JiuwenPlugin(worker=SimpleNamespace())
        plugin.planning_runner = AsyncMock(return_value=cache)
        plugin.frozen_input_options = AsyncMock(return_value={"base_evidence": "Frozen public evidence"})
        result = await plugin.clarify(cache.context, "Original exact task")
        return result, sorted(slots), native.select.await_count

    first, slots, selects = await clarify(1)
    assert first.status == status and slots == [1, 2, 3] and selects == first_selects
    if mode == "selected":
        assert first.proposal_sample_indices == (3,) and first.raw_selection.reviews[0].candidate_id == "p1-g1"
        assert first.delta
    second, slots, selects = await clarify(2)
    assert second.status == status and slots == resumed_slots and selects == resumed_selects
    assert first.selection == second.selection and first.delta == second.delta
    if status == "completed":
        assert second.usage_details == ()
    if mode == "selected":
        from jiuwenswarm.lh.plugins.native_checkpoint import canonical

        key = "completed-clarification:" + hashlib.sha256(canonical("Original exact task")).hexdigest()
        checkpoint, _ = fixture(tmp_path, ordinal=3)
        path = checkpoint.checkpoint._path(key)
        record = json.loads(path.read_bytes())
        record["payload"]["value"]["raw_selection"] = None
        record["payload_sha256"] = hashlib.sha256(canonical(record["payload"])).hexdigest()
        path.write_text(json.dumps(record))
        with pytest.raises(PlanningCheckpointError, match="invalid completed native planning checkpoint"):
            await clarify(4)
