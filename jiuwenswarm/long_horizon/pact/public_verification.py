# Copyright (c) 2026 Chrys. All rights reserved.

"""Bind public-only checks to a campaign and pin their bytes for every check.

Migrated from Chrys ``6fcfb3321892735631b49f33236b1e2a988ded2d``:
``src/chrys/pact/public_verification.py`` and the closed public-verification
and PACT-input schemas from ``service/requirement_clarification``. The suite
uses its local clarification schemas without a runtime dependency on Chrys.
Public checks are development evidence, never the independent grading oracle.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

from pact_core.artifacts import build_loop_paths
from pact_core.runtime.schemas import validate_goal_contract
from pact_core.runtime.store import CampaignReader

from jiuwenswarm.long_horizon.clarification.types import PactGoalContract, PactInitialPlan
from jiuwenswarm.long_horizon.clarification.verification import (
    VERIFICATION_FILE,
    PublicVerification,
    json_digest,
)


MAX_VERIFICATION_BYTES = 256 * 1024
_BOUND_KEYS = {"schema", "goal_contract_sha256", "initial_plan_sha256", "verification"}


def _read(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ValueError("public verifier must be a regular non-symlink file")
    with path.open("rb") as handle:
        raw = handle.read(MAX_VERIFICATION_BYTES + 1)
    if len(raw) > MAX_VERIFICATION_BYTES:
        raise ValueError("public verifier is too large")
    return raw


def _parse(raw: bytes) -> tuple[dict, PublicVerification]:
    payload = json.loads(raw)
    if (
        not isinstance(payload, dict)
        or set(payload) != _BOUND_KEYS
        or payload["schema"] != "jiuwenswarm/pact-bound-public-verification/v1"
    ):
        raise ValueError("invalid bound public verifier")
    verification = PublicVerification.model_validate(payload["verification"])
    if verification.status != "ready" or not verification.checks:
        raise ValueError("public verification is unavailable")
    return payload, verification


def bind_public_verification(
    goal_contract: dict, initial_plan: dict, verification: PublicVerification | dict
) -> dict:
    """Produce the closed binding after validating public input shapes and coverage.

    JSON input digests bind semantic Goal Contract/Initial Plan contents. Campaign
    admission additionally pins the exact serialized verifier bytes, so later file
    replacement (including formatting-only replacement) fails verification.
    Generation must use public repository evidence only; this helper does not read
    task gold, hidden tests, or an independent benchmark verifier.
    """
    contract = PactGoalContract.model_validate(goal_contract)
    PactInitialPlan.model_validate(initial_plan)
    validate_goal_contract(goal_contract)
    checks = PublicVerification.model_validate(verification)
    checks.validate_coverage({criterion.id for criterion in contract.acceptance_criteria})
    return {
        "schema": "jiuwenswarm/pact-bound-public-verification/v1",
        "goal_contract_sha256": json_digest(goal_contract),
        "initial_plan_sha256": json_digest(initial_plan),
        "verification": checks.model_dump(by_alias=True),
    }


def command_from_clarification(contract_file: Path, plan_file: Path, *, workspace: Path, campaign_id: str) -> str:
    """Validate the staged sibling and capture its digest before starting any PACT roles."""
    path = contract_file.parent / VERIFICATION_FILE
    raw = _read(path)
    payload, verification = _parse(raw)
    contract_data = json.loads(contract_file.read_text(encoding="utf-8"))
    plan_data = json.loads(plan_file.read_text(encoding="utf-8"))
    contract = PactGoalContract.model_validate(contract_data)
    PactInitialPlan.model_validate(plan_data)
    if payload["goal_contract_sha256"] != json_digest(contract_data) or payload["initial_plan_sha256"] != json_digest(
        plan_data
    ):
        raise ValueError("public verifier is bound to different PACT inputs")
    verification.validate_coverage({criterion.id for criterion in contract.acceptance_criteria})
    args = [
        "--public-spec",
        str(path.resolve()),
        "--sha256",
        hashlib.sha256(raw).hexdigest(),
        "--campaign-workspace",
        str(workspace.resolve()),
        "--campaign-id",
        campaign_id,
        "--contract-sha256",
        json_digest(validate_goal_contract(contract_data)),
    ]
    return isolated_verify_command(args)


def isolated_verify_command(arguments: list[str]) -> str:
    """Load this installed Jiuwen suite, never packages or PYTHONPATH from a candidate checkout."""
    source_root = str(Path(__file__).resolve().parents[3])
    bootstrap = (
        f"import runpy,sys; sys.path.insert(0, {source_root!r}); "
        "runpy.run_module('jiuwenswarm.long_horizon.pact.verify_shim', run_name='__main__')"
    )
    args = [sys.executable, "-I", "-c", bootstrap, *arguments]
    return subprocess.list2cmdline(args) if os.name == "nt" else shlex.join(args)


def pinned_commands(
    path: Path, expected_sha256: str, *, workspace: Path, campaign_id: str, contract_sha256: str
) -> list[str]:
    """Read exactly the bytes the campaign accepted; mutation cannot replace its checks."""
    raw = _read(path)
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("public verifier changed after campaign admission")
    _, verification = _parse(raw)
    reader = CampaignReader(workspace, campaign_id)
    state = reader.read_current_state()
    if json_digest(reader.read_contract()) != contract_sha256:
        raise ValueError("campaign Goal Contract differs from the public verifier")
    execution = state.get("execution")
    if not isinstance(execution, dict) or not isinstance(execution.get("loop_id"), str):
        raise ValueError("campaign has no current execution")
    expected_loop = build_loop_paths(workspace, execution["loop_id"]).loop_dir.resolve()
    loop_dir = os.environ.get("PACT_LOOP_DIR")
    if not loop_dir or Path(loop_dir).resolve() != expected_loop:
        raise ValueError("verification call does not belong to the current campaign execution")
    plan = reader.read_plan_revision()
    frontier = state.get("frontier")
    selected = frontier.get("selected") if isinstance(frontier, dict) else None
    missions = plan.get("missions")
    if not isinstance(missions, list) or not all(isinstance(item, dict) for item in missions):
        raise ValueError("campaign plan has no valid missions")
    mission = next((item for item in missions if item.get("id") == selected), None)
    if not isinstance(mission, dict) or not mission.get("target_ac_ids"):
        raise ValueError("campaign has no selected mission with acceptance criteria")
    required = set(mission["target_ac_ids"])
    acceptance = state.get("acceptance")
    if not isinstance(acceptance, dict) or not isinstance(acceptance.get("criteria"), list):
        raise ValueError("campaign acceptance state is missing")
    required.update(item["id"] for item in acceptance["criteria"] if item.get("status") == "satisfied")
    checks = [check for check in verification.checks if set(check.target_ac_ids) <= required]
    if not checks or {ac for check in checks for ac in check.target_ac_ids} != required:
        raise ValueError("public verifier does not cover the current mission and prior accepted criteria")
    return [check.command for check in checks]
