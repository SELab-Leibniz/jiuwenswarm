"""Inject any coding-agent turn adapter without importing Chrys or Jiuwen.

The host supplies a factory with real isolation/cancellation guarantees. Suite
mode uses three independent investigations/proposals and a selector; explicit
benchmark mode retains the original labelled single-call protocol.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from pact_core.artifacts import write_json_atomic
from pact_core.runtime import RuntimeContractError
from pact_core.schemas import TurnRequest

from ..file_artifacts import Clarification, ClarificationBudgetExhausted, ClarificationStopped, FileArtifacts
from ..planning_checkpoint import PlanningCheckpoint, PlanningCheckpointError
from ..ports import Capabilities, ClarificationContext, ExecutionBinding, PluginSpec
from ..reviewer_execution import NonDegradableIntegrityError
from ..side_calls import (
    SideCallFailure,
    SideCallIntegrityError,
    SideCallRecovered,
    SideCalls,
    view_identity,
)
from ..suite import original_task_pair, parse_object, validate_pair

MAX_GUIDANCE_POINTS = 6
MAX_GUIDANCE_EVIDENCE = 4
MAX_GUIDANCE_STATEMENT = 4000


class ProposalValidationError(RuntimeContractError):
    """Host-authored validation summaries containing no model/provider payload."""


def proposal_failure_summary(result: BaseException) -> str:
    if isinstance(result, ProposalValidationError):
        return str(result)[:1000]
    if isinstance(result, json.JSONDecodeError):
        return f"proposal JSON failed at line {result.lineno}, column {result.colno}"
    return "proposal stage failed; inspect its own retained runtime evidence"


def proposal_response_schema() -> dict[str, Any]:
    """Expose the same structural contract the host actually validates."""
    nonempty = {"type": "string", "minLength": 1, "pattern": r"\S"}
    return {
        "type": "object",
        "required": ["guidance_points"],
        "properties": {
            "guidance_points": {
                "type": "array",
                "maxItems": MAX_GUIDANCE_POINTS,
                "items": {
                    "type": "object",
                    "required": ["statement", "confidence", "evidence", "decision_impact"],
                    "properties": {
                        "statement": {**nonempty, "maxLength": MAX_GUIDANCE_STATEMENT},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "evidence": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": MAX_GUIDANCE_EVIDENCE,
                            "items": {
                                **nonempty,
                                "description": "An existing relative S0 repository path (optional line suffix), "
                                "or the exact reference 'original task'.",
                            },
                        },
                        "decision_impact": nonempty,
                    },
                },
            }
        },
    }


def record_proposal_failure(
    context: ClarificationContext,
    checkpoints: PlanningCheckpoint,
    original_task: str,
    results: list[Any],
    primary: BaseException,
) -> None:
    """Retain safe rejection evidence without replacing the actual stage failure."""
    try:
        diagnostic_root = context.session_dir / "validation-attempts" / uuid4().hex
        for path in (diagnostic_root, *diagnostic_root.parents):
            if path.is_symlink():
                raise PlanningCheckpointError("proposal diagnostic path is a symlink")
        diagnostic_root.mkdir(parents=True, mode=0o700)
        diagnostic_path = diagnostic_root / "clarification-diagnostics.json"
        write_json_atomic(
            diagnostic_path,
            {
                "schema": "long-horizon/proposal-validation/v1",
                "work_scope_sha256": hashlib.sha256(str(context.session_dir).encode()).hexdigest(),
                "checkpoint_binding_sha256": checkpoints.binding,
                "original_task_sha256": hashlib.sha256(original_task.encode()).hexdigest(),
                "response_schema": proposal_response_schema(),
                "proposals": [
                    {
                        "sample_index": index,
                        "status": "validated" if isinstance(result, dict) else "rejected",
                        "error_type": type(result).__name__ if isinstance(result, BaseException) else None,
                        "detail": proposal_failure_summary(result) if isinstance(result, BaseException) else None,
                    }
                    for index, result in enumerate(results, 1)
                ],
            },
        )
        diagnostic_path.chmod(0o600)
    except Exception as error:
        primary.add_note(f"proposal diagnostic write failed: {type(error).__name__}")
        logging.getLogger(__name__).warning("proposal diagnostic write failed: %s", type(error).__name__)


class GenericPlugin(SideCalls):
    def __init__(self, *, name: str, capabilities: Capabilities, resolve: Callable, adapter_factory: Callable) -> None:
        super().__init__()
        self.spec = PluginSpec.for_agent(name)
        self.capabilities = capabilities
        self.artifacts = FileArtifacts()
        self._resolve, self._factory = resolve, adapter_factory
        self._binding: ExecutionBinding | None = None
        self._args: Any = None
        self._context: dict[str, Any] = {}
        self._active_clarifier: Any = None
        self.public_verification_degradation: dict[str, Any] | None = None

    def configuration(self, profile: str) -> dict[str, object]:
        return {
            "plugin": self.spec.id,
            "profile": profile,
            "workflow": "suite" if self._args is not None and self._args.workflow == "suite" else "benchmark",
            "clarification_protocol": "three-proposal-selector/v1"
            if self._args is not None and self._args.workflow == "suite"
            else "single-readonly/v1",
        }

    def resolve_execution(self, workspace: Path, args: Any) -> ExecutionBinding:
        self._binding = self._resolve(workspace, args)
        self._args = args
        return self._binding

    def make_adapter(self, role: str, binding: ExecutionBinding, args: Any, **context: Any) -> Any:
        self._context = context
        self._remember_execution(binding, args, context)
        return self._factory(role=role, binding=binding, args=args, **context)

    async def clarify(self, context: ClarificationContext, original_task: str) -> Clarification:
        if self._args.workflow == "suite":
            return await self._clarify_suite(context, original_task)
        return await self._clarify_single(context, original_task)

    async def _clarify_single(self, context: ClarificationContext, original_task: str) -> Clarification:
        assert self._binding is not None
        adapter = self.make_adapter("reviewer", self._binding, self._args, **self._context)
        ticket = context.admission.admit("single-readonly")
        self._active_clarifier = adapter
        status = "error"
        detail = ""
        try:
            request = TurnRequest(
                role="reviewer",
                workdir=Path(context.snapshot.roots[0].view_root),
                timeout_seconds=ticket.timeout_seconds,
                prompt="Read the frozen initial repository and clarify the original task. "
                "Your analysis is advisory evidence. Keep original requirements and unresolved questions intact.\n\n"
                + original_task,
            )
            response = await asyncio.to_thread(adapter.run_turn, request)
            self.artifacts.validate(context.snapshot)
            if self._context["abort"].is_set():
                detail = "invocation_stopped"
                raise ClarificationStopped("invocation stopped during clarification")
            if response.status != "completed":
                status = "timeout" if response.status == "timeout" else "error"
                detail = f"role_status={response.status}"
                raise ClarificationBudgetExhausted(f"clarification role ended with {response.status}")
            if not response.final_text.strip() or len(response.final_text) > 150_000:
                raise RuntimeContractError("clarification must be nonempty and bounded")
            status = "completed"
            return Clarification(response.final_text)
        except asyncio.CancelledError:
            status = "error"
            detail = "cancelled"
            await adapter.cancel_current_turn()
            raise
        finally:
            context.admission.finish(ticket, status=status, detail=detail)
            self._active_clarifier = None

    async def cancel(self) -> None:
        await self._cancel_side_calls()
        if self._active_clarifier is not None:
            await self._active_clarifier.cancel_current_turn()

    async def _investigate_recovering(self, context: Any, prompt: str, *, stage: str) -> str:
        try:
            return await self.investigate(context, prompt, stage=stage)
        except SideCallIntegrityError as exc:
            raise SideCallRecovered(self.recover_integrity(exc, stage=stage)) from exc

    def _integrity_fallback_pair(self, original_task: str, recovery: dict[str, Any], *, stage: str) -> dict[str, Any]:
        self.public_verification_degradation = {
            "reason": "integrity_recovered",
            "stage": stage,
            "integrity_recovery": recovery,
        }
        pair = original_task_pair(original_task)
        pair["public_verification"] = {
            "status": "unavailable",
            "reason": "Planning was generated from the authoritative original task after a recovered "
            "side-call integrity failure; no public check was generated or claimed.",
            "checks": [],
            "degradation": self.public_verification_degradation,
        }
        return validate_pair(pair)

    async def _clarify_suite(self, context: ClarificationContext, original_task: str) -> Clarification:
        assert self._binding is not None
        checkpoints = PlanningCheckpoint(
            context.session_dir,
            {
                "protocol": "three-proposal-selector/v1",
                "implementation_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "plugin": self.spec.id,
                "execution": self._binding.fingerprint,
                "configuration": self.configuration(self._args.profile_name),
                "evidence_configuration": self.evidence_configuration(),
                "original_task_sha256": hashlib.sha256(original_task.encode()).hexdigest(),
                "s0_identity": await asyncio.to_thread(view_identity, context.snapshot),
            },
        )
        focuses = {
            1: "primary implementation and direct callers",
            2: "public tests and boundary cases",
            3: "integration and cross-file contracts",
        }

        def grounded(reference: str) -> bool:
            if reference.strip().lower() == "original task":
                return True
            path_text = re.sub(r":\d+(?:-\d+)?$", "", reference.strip())
            relative = Path(path_text)
            if relative.is_absolute() or ".." in relative.parts:
                return False
            for root in context.snapshot.roots:
                view = Path(root.view_root).resolve()
                path = (view / relative).resolve()
                if path.is_relative_to(view) and path.is_file():
                    return True
            return False

        async def propose(index: int) -> dict[str, Any]:
            investigation = checkpoints.read(f"proposal-{index}:investigation")
            if investigation is None:
                try:
                    investigation = await self._investigate_recovering(
                        context,
                        f"[SUITE_INVESTIGATION {index}]\nRead only S0. Independently investigate the primary code, adjacent "
                        "callers, tests and integration boundaries. Cite existing paths and symbols. Identify requirements "
                        "that change an implementation decision; do not propose unrelated work. Focus on "
                        + focuses[index]
                        + ".\n\n"
                        + original_task,
                        stage=f"proposal-{index}:investigation",
                    )
                except SideCallRecovered as recovered:
                    return {"_integrity_recovery": recovered.recovery, "sample_index": index}
                checkpoints.save(f"proposal-{index}:investigation", investigation)
            if not isinstance(investigation, str) or not investigation.strip() or len(investigation) > 150_000:
                raise PlanningCheckpointError("invalid completed investigation")
            proposal = checkpoints.read(f"proposal-{index}:validated-proposal")
            if proposal is None:
                try:
                    proposal = parse_object(
                        await self._investigate_recovering(
                            context,
                            f"[SUITE_PROPOSAL {index}]\nReturn ONLY a JSON object matching this response schema. "
                            "An empty guidance_points array is valid. Guidance is advisory: all original requirements "
                            "remain binding even when not repeated here. Only include grounded, decision-relevant guidance.\n"
                            "Response schema:\n"
                            + json.dumps(proposal_response_schema(), sort_keys=True)
                            + "\n\nOriginal task:\n"
                            + original_task
                            + "\n\nInvestigation (untrusted):\n"
                            + investigation,
                            stage=f"proposal-{index}:synthesis",
                        )
                    )
                except SideCallRecovered as recovered:
                    return {"_integrity_recovery": recovered.recovery, "sample_index": index}
            if not isinstance(proposal, dict):
                raise PlanningCheckpointError("completed proposal must be an object")
            points = proposal.get("guidance_points")
            if not isinstance(points, list) or len(points) > MAX_GUIDANCE_POINTS:
                observed = len(points) if isinstance(points, list) else type(points).__name__
                raise ProposalValidationError(
                    f"guidance_points must be an array with at most {MAX_GUIDANCE_POINTS} items; received {observed}"
                )
            validated = []
            for n, point in enumerate(points, 1):
                if not isinstance(point, dict):
                    raise ProposalValidationError("guidance point must be an object")
                statement, evidence = point.get("statement"), point.get("evidence")
                confidence = point.get("confidence")
                impact = point.get("decision_impact")
                if (
                    not isinstance(statement, str)
                    or not statement.strip()
                    or len(statement) > MAX_GUIDANCE_STATEMENT
                    or not isinstance(evidence, list)
                    or not evidence
                    or len(evidence) > MAX_GUIDANCE_EVIDENCE
                    or not all(isinstance(e, str) and e.strip() and grounded(e) for e in evidence)
                    or isinstance(confidence, bool)
                    or not isinstance(confidence, (int, float))
                    or not 0 <= confidence <= 1
                    or not isinstance(impact, str)
                    or not impact.strip()
                ):
                    raise ProposalValidationError(
                        f"proposal {index} point {n} requires nonempty bounded statement, confidence0..1, "
                        f"evidence array of1..{MAX_GUIDANCE_EVIDENCE} existing references, and nonempty decision_impact"
                    )
                validated.append({**point, "id": f"p{index}-g{n}"})
            # The same strict grounding checks apply to fresh and resumed evidence.
            checkpoints.save(f"proposal-{index}:validated-proposal", proposal)
            return {
                "sample_index": index,
                "focus": focuses[index],
                "investigation": investigation[:12_000],
                "guidance_points": validated,
            }

        tasks = [asyncio.create_task(propose(index)) for index in (1, 2, 3)]
        try:
            results = await asyncio.gather(*tasks, return_exceptions=True)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        for result in results:
            if isinstance(result, (PlanningCheckpointError, SideCallIntegrityError, NonDegradableIntegrityError)):
                record_proposal_failure(context, checkpoints, original_task, results, result)
                raise result
        for result in results:
            if isinstance(result, ClarificationBudgetExhausted):
                record_proposal_failure(context, checkpoints, original_task, results, result)
                raise result
        valid = [
            result
            for result in results
            if isinstance(result, dict) and "_integrity_recovery" not in result
        ]
        integrity_recoveries = [
            result["_integrity_recovery"]
            for result in results
            if isinstance(result, dict) and "_integrity_recovery" in result
        ]
        if len(valid) < 2:
            failure = RuntimeContractError("fewer than two valid independent clarification proposals")
            record_proposal_failure(context, checkpoints, original_task, results, failure)
            raise failure
        candidates = {point["id"]: point for proposal in valid for point in proposal["guidance_points"]}
        diagnostics = {
            "schema": "long-horizon/clarification-proposals/v1",
            "protocol": "three-proposal-selector/v1",
            "proposals": valid,
            "failed_proposals": [type(result).__name__ for result in results if isinstance(result, BaseException)],
            "integrity_recoveries": integrity_recoveries,
            "resumed_completed_stages": checkpoints.reused,
        }
        if not candidates:
            return Clarification("No additional guidance; fulfil the complete original task.", diagnostics=diagnostics)
        try:
            selected = parse_object(
                await self._investigate_recovering(
                    context,
                    "[SUITE_SELECTOR]\nReturn JSON with reviews, an array with one object for EVERY candidate id. "
                    "Each object has id, decision (select or reject), and rationale. Select only necessary evidence-based "
                    "guidance. Keep contradictory or unsupported suggestions out; preserve unanswered user questions.\n\n"
                    + str(valid),
                    stage="selector",
                )
            )
        except SideCallRecovered as recovered:
            return Clarification(
                "No additional guidance; fulfil the complete original task.",
                diagnostics={**diagnostics, "integrity_recovery": recovered.recovery},
            )
        reviews = selected.get("reviews")
        if (
            not isinstance(reviews, list)
            or not all(isinstance(row, dict) for row in reviews)
            or {row.get("id") for row in reviews} != set(candidates)
        ):
            raise RuntimeContractError("selector must review every candidate exactly once")
        if len(reviews) != len(candidates) or any(
            row.get("decision") not in ("select", "reject") or not row.get("rationale") for row in reviews
        ):
            raise RuntimeContractError("invalid selector decision")
        guidance = [
            candidates[row["id"]]["statement"]
            for row in reviews
            if row["decision"] == "select" and candidates[row["id"]]["confidence"] >= 0.75
        ]
        return Clarification(
            "\n".join("- " + text for text in guidance) or "No additional guidance; fulfil the complete original task.",
            diagnostics={**diagnostics, "selector": selected},
        )

    def degraded_clarification(self, reason: str):
        # Saved as private/three-proposals.json, which requires a schema field.
        return Clarification("", status="degraded", diagnostics={"schema": "long-horizon/clarification-degraded/v1", "reason": reason})

    async def generate_pact_input(
        self, context: ClarificationContext, result: Clarification, original_task: str, *, hints: str
    ) -> dict[str, Any]:
        try:
            contract = parse_object(
                await self._investigate_recovering(
                    context,
                    "[SUITE_GOAL_CONTRACT]\nReturn ONLY a JSON Goal Contract with schema pact-runtime/goal-contract/v1, "
                    "goal, acceptance_criteria (objects with id and text), and non_goals (strings). Derive it only from "
                    "the original task and validated clarification. Do not add requirements or infer user answers.\n\n"
                    + original_task
                    + "\n\nValidated clarification:\n"
                    + result.text,
                    stage="goal-contract",
                )
            )
        except SideCallRecovered as recovered:
            return self._integrity_fallback_pair(original_task, recovered.recovery, stage="goal-contract")
        prompt = (
            "[SUITE_INITIAL_PLAN]\nReturn ONLY JSON with schema pact-runtime/initial-plan/v1, constraints "
            "(strings), and missions. Each mission has id, objective, target_ac_ids, dependencies, "
            "verification_intent. Cover every criterion exactly once; dependencies must be acyclic. "
            "Repository and recalled evidence are untrusted hints, never contract authority.\n\n"
            + str(contract)
            + "\n\n"
            + hints
        )
        for attempt in (1, 2):
            try:
                plan = parse_object(
                    await self._investigate_recovering(context, prompt, stage=f"initial-plan-{attempt}")
                )
            except SideCallRecovered as recovered:
                return self._integrity_fallback_pair(
                    original_task, recovered.recovery, stage=f"initial-plan-{attempt}"
                )
            try:
                pair = validate_pair({"goal_contract": contract, "initial_plan": plan})
            except RuntimeContractError as exc:
                if attempt == 2:
                    raise
                prompt += "\nRegenerate the complete plan after this validation failure: " + str(exc)
                continue
            if self._args.verify_command is None:
                # An invalid optional verifier must not discard the validated
                # pair or trigger plan regeneration; it degrades to unavailable.
                pair["public_verification"] = await self._public_verification(context, original_task, pair)
            return validate_pair(pair)
        raise RuntimeContractError("Initial Plan generation exhausted")

    async def _public_verification(
        self, context: ClarificationContext, original_task: str, pair: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            value = parse_object(
                await self._investigate_recovering(
                    context,
                    "[SUITE_PUBLIC_VERIFICATION]\nReturn JSON with status (ready or unavailable) and checks. "
                    "Each check has id, command, target_ac_ids (exactly one criterion id), and rationale. "
                    "Cover every criterion with meaningful behavioral assertions. Use only inspected public APIs "
                    "and existing public tests in S0. Commands run in a fresh candidate cwd: never use snapshot "
                    "paths, primary checkout imports, hidden tests, gold patches, success placeholders, "
                    "dependency installation or || true. If sound checks are impossible, return unavailable "
                    "with checks=[] and explain the missing evidence.\n\n" + original_task + "\n\n" + str(pair),
                    stage="public-verification",
                )
            )
            validate_pair({**pair, "public_verification": value})
            return value
        except asyncio.CancelledError:
            raise
        except SideCallRecovered as exc:
            reason = "integrity_recovered"
            self.public_verification_degradation = {
                "reason": "integrity_recovered",
                "stage": "public-verification",
                "integrity_recovery": exc.recovery,
            }
        except (SideCallIntegrityError, NonDegradableIntegrityError):
            raise
        except (RuntimeContractError, ValueError, SideCallFailure, self.artifacts.exhausted) as exc:
            if isinstance(exc, self.artifacts.stopped):
                raise
            reason = type(exc).__name__
        if reason != "integrity_recovered":
            self.public_verification_degradation = {
                "reason": "invalid_or_unavailable_public_verification",
                "type": reason,
            }
        return {
            "status": "unavailable",
            "reason": "Generated public verification unavailable (" + reason + "); continuing with the validated "
            "Goal Contract and Initial Plan. No public check was generated or claimed.",
            "checks": [],
            "degradation": self.public_verification_degradation,
        }
