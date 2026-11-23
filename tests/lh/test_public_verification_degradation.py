# Copyright (c) 2026 Chrys. All rights reserved.

"""Invalid generated public verification degrades; it never stops PACT planning.

Synthetic native service/runner doubles; no model, task data or grade.
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwenswarm.lh.planning_checkpoint import NativeArtifactInvalid
from jiuwenswarm.lh.plugins.native import PUBLIC_VERIFICATION_ATTEMPTS, NativeClarification
from jiuwenswarm.lh.ports import ClarificationContext
from jiuwenswarm.lh.suite import validate_pair

CONTRACT = {
    "schema": "pact-runtime/goal-contract/v1",
    "goal": "Synthetic original requirement",
    "acceptance_criteria": [{"id": "ac1", "text": "The synthetic behavior holds."}],
    "non_goals": [],
}
PLAN = {
    "schema": "pact-runtime/initial-plan/v1",
    "constraints": [],
    "missions": [
        {
            "id": "m1",
            "objective": "Implement the synthetic behavior.",
            "target_ac_ids": ["ac1"],
            "dependencies": [],
            "verification_intent": "Check the synthetic behavior.",
        }
    ],
}


class Exhausted(RuntimeError):
    pass


class Stopped(Exhausted):
    pass


class NativeIntegrity(RuntimeError):
    pass


budget_module = types.ModuleType("synthetic_native_clarification.budget")
budget_module.SideCallIntegrityError = NativeIntegrity
sys.modules.setdefault("synthetic_native_clarification", types.ModuleType("synthetic_native_clarification"))
sys.modules.setdefault("synthetic_native_clarification.budget", budget_module)


class Model:
    def __init__(self, value):
        self.value = value
        self.acceptance_criteria = [SimpleNamespace(**row) for row in value.get("acceptance_criteria", [])]

    def model_dump(self, **kwargs):
        return dict(self.value)

    def model_dump_json(self, **kwargs):
        return str(self.value)


@dataclass
class Admission:
    refuse_at: int | None = None
    admitted: list[str] = field(default_factory=list)
    refused: str | None = None

    def admit(self, stage):
        if self.refuse_at is not None and len(self.admitted) >= self.refuse_at:
            self.refused = "deadline"
            raise Exhausted("trial budget refused " + stage)
        self.admitted.append(stage)
        return SimpleNamespace(token=stage)

    def finish(self, ticket, *, status, detail=""):
        pass


class Verification:
    def __init__(self, checks):
        self.checks = checks

    def validate_coverage(self, ids):
        assert {check["target_ac_ids"][0] for check in self.checks} == ids

    def model_dump(self, **kwargs):
        return {"status": "ready", "checks": self.checks}


class Runner:
    """Mimics a native per-criterion verifier: every reply is admitted, then repaired."""

    def __init__(self, context, mode):
        self.context, self.mode = context, mode

    async def generate_public_verification(self, prompt, *, acceptance_criteria):
        assert "Goal Contract" in prompt and "Initial Plan" in prompt
        if self.mode == "stopped":
            raise Stopped("invocation stopped")
        if self.mode == "integrity":
            raise NativeIntegrity("frozen view changed")
        if self.mode == "invalid_native":
            raise NativeArtifactInvalid("native public verification did not pass its own contract")
        for attempt in range(1, 7):
            self.context.admission.admit(f"requirement-clarification-public-verification:ac1:attempt-{attempt}")
            if self.mode == "valid":
                checks = [{"id": "ac1-check1", "command": "python3 -c 'assert 1'", "target_ac_ids": ["ac1"]}]
                return Verification(checks), {}
        raise Exhausted("Public verifier repair budget exhausted")


class Service:
    def __init__(self, runner):
        self.runner = runner

    async def generate_pact_input(self, *, generate_public_verification, **kwargs):
        # Goal/Plan never wait for the optional verification stage.
        assert generate_public_verification is False
        return SimpleNamespace(
            goal_contract=Model(CONTRACT),
            initial_plan=Model(PLAN),
            public_verification=None,
        ), ()


def plugin(mode):
    value = NativeClarification.__new__(NativeClarification)
    value.artifacts = SimpleNamespace(exhausted=Exhausted, stopped=Stopped)
    value._service = Service
    value._types = SimpleNamespace(__package__="synthetic_native_clarification")
    value._revision = lambda **kwargs: SimpleNamespace(rendered="Synthetic original requirement")
    value._side_args = SimpleNamespace(verify_command=None)
    value._active_clarification = None
    value.public_verification_degradation = None

    async def planning_runner(context, original_task):
        return Runner(context, mode)

    async def frozen_input_options(runner, original_task):
        return {}

    value.planning_runner = planning_runner
    value.frozen_input_options = frozen_input_options
    return value


def context(admission, deadline=None):
    return ClarificationContext(None, None, "synthetic", Path("unused"), admission, deadline)


async def run(mode, admission=None, deadline=None):
    native = plugin(mode)
    admission = admission or Admission()
    payload = await native.generate_pact_input(
        context(admission, deadline), SimpleNamespace(delta=""), "Synthetic original requirement", hints=""
    )
    return native, admission, payload


@pytest.mark.asyncio
async def test_repeated_invalid_verifier_is_capped_and_degrades_to_unavailable():
    native, admission, payload = await run("invalid")
    assert len(admission.admitted) == PUBLIC_VERIFICATION_ATTEMPTS == 3
    verification = payload["public_verification"]
    assert verification["status"] == "unavailable" and verification["checks"] == []
    assert verification["degradation"]["reason"] == "repair_cap_reached"
    assert native.public_verification_degradation["capped_stages"] == [
        "requirement-clarification-public-verification:ac1:attempt-4"
    ]
    # The validated pair proceeds to PACT; no check is fabricated or registered.
    pair = validate_pair(payload)
    assert pair["goal_contract"]["acceptance_criteria"][0]["id"] == "ac1"
    assert pair["initial_plan"]["missions"][0]["target_ac_ids"] == ["ac1"]


@pytest.mark.asyncio
async def test_valid_verifier_is_kept_unchanged():
    native, admission, payload = await run("valid")
    assert payload["public_verification"]["status"] == "ready"
    assert native.public_verification_degradation is None
    assert validate_pair(payload)["public_verification"]["checks"][0]["target_ac_ids"] == ["ac1"]


@pytest.mark.asyncio
async def test_trial_budget_refusal_and_invalid_native_artifact_degrade_with_reason():
    _, _, refused = await run("invalid", admission=Admission(refuse_at=1))
    assert refused["public_verification"]["degradation"]["reason"] == "trial_budget_refused"
    _, _, invalid = await run("invalid_native")
    assert invalid["public_verification"]["degradation"]["reason"] == "invalid_public_verification_artifact"


@pytest.mark.asyncio
async def test_no_time_before_planning_deadline_skips_verification():
    import time

    _, admission, payload = await run("valid", deadline=time.time() + 1)
    assert admission.admitted == []
    assert payload["public_verification"]["degradation"]["reason"] == "no_time_before_planning_deadline"


@pytest.mark.parametrize("mode,error", [("stopped", Stopped), ("integrity", NativeIntegrity)])
@pytest.mark.asyncio
async def test_stop_and_integrity_failures_still_propagate(mode, error):
    with pytest.raises(error):
        await run(mode)


@pytest.mark.asyncio
async def test_portable_plugin_keeps_validated_pair_when_verifier_is_invalid():
    import json

    from jiuwenswarm.lh.plugins.generic import GenericPlugin
    from jiuwenswarm.lh.ports import Capabilities, ExecutionBinding

    value = GenericPlugin(
        name="synthetic",
        capabilities=Capabilities(True, True, True, True),
        resolve=lambda w, a: ExecutionBinding("synthetic-v1", {}),
        adapter_factory=lambda role, **kwargs: None,
    )
    value._args = SimpleNamespace(verify_command=None, workflow="suite")
    stages = []

    async def investigate(context, prompt, *, stage):
        stages.append(stage)
        if stage == "goal-contract":
            return json.dumps(CONTRACT)
        if stage.startswith("initial-plan"):
            return json.dumps(PLAN)
        return '{"status": "ready", "checks": [{"id": "x", "command": "true", "target_ac_ids": ["ac1"]}]}'

    value.investigate = investigate
    pair = await value.generate_pact_input(None, SimpleNamespace(text=""), "Synthetic original requirement", hints="")
    assert stages == ["goal-contract", "initial-plan-1", "public-verification"]  # no plan regeneration
    assert pair["goal_contract"]["acceptance_criteria"][0]["id"] == "ac1"
    assert pair["public_verification"]["status"] == "unavailable" and pair["public_verification"]["checks"] == []
    assert value.public_verification_degradation["type"] == "RuntimeContractError"
