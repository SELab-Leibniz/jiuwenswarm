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
    reader.read_current_state()
    if json_digest(reader.read_contract()) != contract_sha256:
        raise ValueError("campaign Goal Contract differs from the public verifier")
    # WorkRuntime owns candidate/execution binding and records the command result.
    # This public verifier always checks the whole original contract; advisory
    # mission selection must not narrow acceptance or depend on retired state.
    required = {item['id'] for item in reader.read_contract()['acceptance_criteria']}
    verification.validate_coverage(required)
    return [check.command for check in verification.checks]
