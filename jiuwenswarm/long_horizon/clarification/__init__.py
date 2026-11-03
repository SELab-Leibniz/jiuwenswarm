"""Pinned Chrys clarification policy ported onto native JiuwenSwarm execution."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .artifacts import ClarificationArtifactStore
from .budget import SideCallBudgetExhausted, SideCallInterrupted, SideCallIntegrityError
from .compat import atomic_write_owner_only_text, ensure_owner_only_directory
from .event_evidence import snapshot_workspace
from .model import JiuwenClarificationModel
from .service import ClarificationService, validate_pact_runtime_input
from .snapshot import WorkspaceSnapshot, WorkspaceSnapshotter, capture_workspace
from .types import ClarificationResult, PactRuntimeInput, RequirementRevision
from .verification import VERIFICATION_FILE, json_digest

__all__ = [
    "ClarificationArtifactStore", "ClarificationResult", "ClarificationService", "JiuwenClarificationModel",
    "PactRuntimeInput", "RequirementRevision", "SideCallBudgetExhausted", "SideCallInterrupted", "SideCallIntegrityError", "WorkspaceSnapshot",
    "WorkspaceSnapshotter", "capture_workspace", "generate_pact_inputs", "run_clarification",
    "save_pact_inputs", "snapshot_workspace", "validate_pact_runtime_input",
]


async def run_clarification(worker: Any, snapshot: WorkspaceSnapshot, requirement: str,
                            output_dir: Path, timeout_seconds: float = 7200.0) -> ClarificationResult:
    """Clarify only S0; PACT generation is separate so localization can finish first."""
    revision = RequirementRevision(number=1, messages=(requirement,))
    artifacts = ClarificationArtifactStore(Path(output_dir), turn_number=1)
    artifacts.save_requirement_input(revision=revision.number, messages=revision.messages)
    model = JiuwenClarificationModel(worker=worker, snapshot=snapshot,
        output_dir=Path(output_dir) / "native-calls", timeout_seconds=timeout_seconds)
    result = await ClarificationService(model).clarify(revision=revision, background="", snapshot=snapshot)
    artifacts.save_result(result, requirement_messages=revision.messages)
    return result


async def generate_pact_inputs(worker: Any, snapshot: WorkspaceSnapshot, requirement: str,
                               result: ClarificationResult, output_dir: Path,
                               localization_hints: str = "", timeout_seconds: float = 7200.0,
                               runtime_evidence: str = "", checkpoint_identity: dict | None = None,
                               checkpoint_root: Path | None = None) -> PactRuntimeInput:
    model = JiuwenClarificationModel(worker=worker, snapshot=snapshot,
        output_dir=Path(output_dir) / "native-calls", timeout_seconds=timeout_seconds,
        runtime_evidence=runtime_evidence, checkpoint_identity=checkpoint_identity, checkpoint_root=checkpoint_root)
    failure: BaseException | None = None
    try:
        pact_input, _usage = await ClarificationService(model).generate_pact_input(
            result=result, revision=RequirementRevision(number=1, messages=(requirement,)),
            background="", snapshot=snapshot, localization_hints=localization_hints,
            generate_public_verification=True,
        )
    except BaseException as exc:
        failure = exc
        raise
    finally:
        # Completed and rejected calls both consume budget, including a terminal
        # failure after earlier criteria were already generated and reviewed.
        def save_usage() -> None:
            ensure_owner_only_directory(Path(output_dir))
            atomic_write_owner_only_text(Path(output_dir) / "generation-usage.json",
                json.dumps({"schema": "jiuwenswarm/pact-input-usage/v1", "calls": model.usage_details}, sort_keys=True, indent=2) + "\n")
        if failure is None:
            save_usage()
        else:
            model._preserve_failure(failure, save_usage)
    return pact_input


def save_pact_inputs(pact_input: PactRuntimeInput, output_dir: Path) -> dict[str, Path | None]:
    """Archive the accepted pair and byte-stable bound public-check proposal."""
    output_dir = Path(output_dir)
    ensure_owner_only_directory(output_dir)
    validate_pact_runtime_input(pact_input.goal_contract, pact_input.initial_plan)
    goal = pact_input.goal_contract.model_dump(mode="json", by_alias=True)
    plan = pact_input.initial_plan.model_dump(mode="json", by_alias=True)
    contract_path = output_dir / "goal-contract.json"
    plan_path = output_dir / "initial-plan.json"
    verification_path = output_dir / VERIFICATION_FILE if pact_input.public_verification is not None else None
    for path in (contract_path, plan_path, verification_path):
        if path is not None and (path.exists() or path.is_symlink()):
            raise FileExistsError(f"refusing to replace accepted PACT inputs: {path}")
    verification = pact_input.public_verification
    if verification is not None:
        verification.validate_coverage({item.id for item in pact_input.goal_contract.acceptance_criteria})
    for path, payload in ((contract_path, goal), (plan_path, plan)):
        atomic_write_owner_only_text(path, json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n")
    if verification is not None and verification_path is not None:
        atomic_write_owner_only_text(verification_path, json.dumps({
            "schema": "jiuwenswarm/pact-bound-public-verification/v1",
            "goal_contract_sha256": json_digest(goal), "initial_plan_sha256": json_digest(plan),
            "verification": verification.model_dump(mode="json", by_alias=True),
        }, ensure_ascii=True, indent=2, sort_keys=True) + "\n")
    return {"contract_path": contract_path, "plan_path": plan_path, "public_verification_path": verification_path}
