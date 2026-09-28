"""Clarification services backed exclusively by the native Jiuwen worker.

The schemas/prompts are ported from Chrys 6fcfb332. This adapter does not import
its Agent, model client, session host, or kernel. Every proposal, selector, PACT
input, and public-check review is a real Jiuwen worker call on a frozen view.
"""

from __future__ import annotations

import json
import hashlib
from pathlib import Path
from typing import Any, Callable, TypeVar

from pydantic import BaseModel

from ..structured import decode_structured_object, parse_unique_json_object

from .budget import SideCallBudgetExhausted, SideCallInterrupted, SideCallIntegrityError
from .compat import atomic_write_owner_only_text, ensure_owner_only_directory
from .event_evidence import investigation_tool_calls, snapshot_workspace, verified_evidence, validate_model_view
from .model_protocol import ClarificationModelRunner
from .prompts import (
    legacy_v1_proposal_instructions, legacy_v1_selector_instructions,
    pact_goal_contract_instructions, pact_initial_plan_instructions,
    proposal_focus, proposal_instructions, proposal_required_coverage, selector_instructions,
)
from .snapshot import WorkspaceSnapshot
from .types import (
    ClarificationProposal, ClarificationSelection, ClarificationSelectorDecision,
    LegacyV1ClarificationProposal, PactAcceptanceCriterion, PactGoalContract, PactInitialPlan,
    ProposalInvestigation, ProposalModelResult,
)
from .validation import _investigation_errors, _proposal_errors, _successful_read_paths, _successful_search_queries
from .verification import (
    PUBLIC_VERIFICATION_INSTRUCTIONS, PUBLIC_VERIFICATION_REVIEW_INSTRUCTIONS,
    PublicVerification, referenced_public_tests,
)

ResponseT = TypeVar("ResponseT", bound=BaseModel)


def _strict_object(text: str) -> dict:
    return parse_unique_json_object(text)


def _decoded_value(text: str) -> tuple[dict, dict]:
    decoded = decode_structured_object(text)
    return decoded.value, {
        "format": decoded.format, "object_start": decoded.start, "object_end": decoded.end,
        "native_final_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "object_sha256": hashlib.sha256(decoded.object_text.encode()).hexdigest(),
        "payload_rewritten": False,
    }


def _sum_usage(items: list[dict]) -> dict[str, object]:
    result: dict[str, object] = {}
    for item in items:
        for key, value in item.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                result[key] = result.get(key, 0) + value
    return result


class JiuwenClarificationModel(ClarificationModelRunner):
    """Fresh role sessions and native evidence; no fabricated continuation."""

    def __init__(self, *, worker: Any, snapshot: WorkspaceSnapshot, output_dir: Path,
                 timeout_seconds: float = 7200.0) -> None:
        self.worker = worker
        self.snapshot = snapshot
        self.workspace = snapshot_workspace(snapshot)
        self.output_dir = Path(output_dir)
        self.timeout_seconds = timeout_seconds
        self._sequence = 0
        ensure_owner_only_directory(self.output_dir)

    async def _call(self, stage: str, prompt: str, *, schema: dict | None = None,
                    session_id: str | None = None) -> Any:
        self._sequence += 1
        directory = self.output_dir / f"{self._sequence:04d}-{stage}"
        validate_model_view(self.snapshot)
        try:
            result = await self.worker.run(
                stage=stage, prompt=prompt, workspace=self.workspace, output_dir=directory,
                read_only=True, schema=schema, timeout_seconds=self.timeout_seconds, session_id=session_id,
            )
        except Exception as exc:
            # The worker owns bounded process-group cleanup and saved native state.
            # Do not retry an exhausted turn with a new session or claim it failed evidence.
            if str(getattr(exc, "code", "")).upper() in {"INTERRUPTED", "CANCELLED", "CANCELED"} or getattr(getattr(exc, "result", None), "status", "") in {"cancelled", "canceled", "interrupted"}:
                raise SideCallInterrupted(f"{stage} interrupted", result=getattr(exc, "result", None),
                    output_dir=getattr(exc, "output_dir", directory)) from exc
            if type(exc).__name__ in {"WorkerBudgetExhaustedError", "WorkerTimeoutError"} or str(getattr(exc, "code", "")).lower() in {
                "budget_exhausted", "timeout", "output_budget_exhausted",
            }:
                raise SideCallBudgetExhausted(
                    f"{stage} suspended: {type(exc).__name__}",
                    result=getattr(exc, "result", None), output_dir=getattr(exc, "output_dir", directory),
                ) from exc
            raise
        validate_model_view(self.snapshot)
        if result.status in {"cancelled", "canceled", "interrupted"} or result.exit_code in {130, 137, 143}:
            raise SideCallInterrupted(f"{stage} interrupted", result=result, output_dir=directory)
        if result.status in {"budget_exhausted", "timeout", "output_budget_exhausted"}:
            raise SideCallBudgetExhausted(f"{stage} suspended: {result.status}", result=result, output_dir=directory)
        if result.status != "completed" or result.exit_code != 0:
            raise RuntimeError(f"Jiuwen {stage} did not complete: {result.status}, exit={result.exit_code}")
        if not isinstance(result.final_text, str) or not result.final_text.strip():
            raise ValueError(f"Jiuwen {stage} returned no native final text")
        if not result.session_id:
            raise ValueError(f"Jiuwen {stage} omitted native session identity")
        return result

    def _record(self, name: str, payload: dict) -> None:
        atomic_write_owner_only_text(self.output_dir / name,
            json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n")

    async def _structured(self, stage: str, prompt: str, *, response_format: type[ResponseT],
                          instructions: str, require_investigation: bool = False,
                          validate: Callable[[ResponseT], None] | None = None) -> tuple[ResponseT, dict[str, object]]:
        prompt = instructions + "\n\n" + prompt
        if require_investigation:
            prompt += ("\n\nBefore replying, search the frozen repository and read at least two distinct relevant "
                       "public files with native grep and read_file tools. Only these two tools are available in "
                       "this read-only transport; use grep to discover paths, not glob, list_files, or shell tools. "
                       "Cite only the files actually inspected. "
                       "Never write files or consult any live workspace, hidden benchmark tests, or gold patch.")
        prompt += "\n\nReply with exactly one JSON object, no Markdown or prose."
        schema = response_format.model_json_schema()
        errors: list[str] = []
        # This isolated stage adapter does not implement verified same-session continuation.
        # Keep the completed malformed turn as evidence rather than launching a new
        # session and mislabelling it a continuation.
        result = await self._call(stage, prompt, schema=schema)
        try:
            decoded, transport = _decoded_value(result.final_text)
            value = response_format.model_validate(decoded)
            if require_investigation:
                calls = investigation_tool_calls(result.events, self.snapshot)
                errors = _investigation_errors(calls)
                paths = _successful_read_paths(calls)
                if len(paths) < 2:
                    errors.append("public verification requires two distinct inspected public files")
                if isinstance(value, PublicVerification):
                    for check in value.checks:
                        for path in referenced_public_tests(check.command):
                            if path not in paths:
                                errors.append(f"public test was not inspected: {path}")
                if errors:
                    raise ValueError("; ".join(errors))
            if validate is not None:
                validate(value)
            self._record(f"accepted-{stage}-{self._sequence:04d}.json", {
                "schema": "jiuwenswarm/accepted-side-call/v1", "stage": stage,
                "session_id": result.session_id, "structured_output": transport,
                "usage": result.usage,
            })
            return value, _sum_usage([dict(result.usage)])
        except (ValueError, TypeError) as exc:
            self._record(f"rejected-{stage}-{self._sequence:04d}.json", {
                "schema": "jiuwenswarm/rejected-side-call/v1", "stage": stage,
                "session_id": result.session_id, "attempt": 1, "errors": [str(exc)[:6000]],
                "final_text": result.final_text, "usage": result.usage,
            })
            raise

    async def propose(self, prompt: str, *, sample_index: int) -> ProposalModelResult:
        stage = f"clarification-proposal-{sample_index}"
        instructions = proposal_instructions()
        messages = instructions + "\n\n" + prompt + (
            "\n\nUse native grep and read_file tools on the frozen snapshot before answering. "
            "Only these two tools are available in this read-only transport; use grep to discover paths, "
            "not glob, list_files, or shell tools. "
            "At least one successful targeted search and one successful source read are required. "
            "Every current_repo anchor must be an exact repository-relative path, optionally :start-end, "
            "whose cited lines appeared in an actual read tool result. Investigate both primary and adjacent coverage. "
            "Return exactly one JSON object matching the supplied schema."
        )
        session_id = None
        events: list[dict] = []
        usages: list[dict] = []
        calls = []
        errors: list[str] = []
        proposal = None
        references = []
        transport = None
        attempt = 0
        for attempt in (1,):
            try:
                result = await self._call(stage, messages, schema=ClarificationProposal.model_json_schema(), session_id=session_id)
                session_id = result.session_id
                usages.append(dict(result.usage))
                events.extend(result.events)
                calls = investigation_tool_calls(events, self.snapshot)
                decoded, transport = _decoded_value(result.final_text)
                proposal = ClarificationProposal.model_validate(decoded)
                references, reference_errors = verified_evidence(proposal, calls)
                errors = [*_investigation_errors(calls), *_proposal_errors(proposal, calls), *reference_errors]
                if not errors:
                    break
            except (SideCallBudgetExhausted, SideCallInterrupted, SideCallIntegrityError):
                raise
            except Exception as exc:
                errors = [f"{type(exc).__name__}: {str(exc)[:3000]}"]
            proposal = None
        totals = _sum_usage(usages)
        investigation = ProposalInvestigation(
            sample_index=sample_index, focus=proposal_focus(sample_index),
            status="completed" if proposal is not None and not errors else "failed",
            investigation_attempts=max(1, attempt), synthesis_attempts=attempt,
            coverage_status="sufficient" if proposal is not None and not errors else "insufficient",
            required_coverage=list(proposal_required_coverage(sample_index)),
            search_queries=list(_successful_search_queries(calls))[:40],
            inspected_paths=list(_successful_read_paths(calls))[:40],
            coverage_errors=errors[:20], validation_errors=errors[:20], tool_calls=calls,
            verified_evidence=references if proposal is not None else [],
            input_token_count=int(totals.get("input_tokens", 0)),
            output_token_count=int(totals.get("output_tokens", 0)),
            total_token_count=int(totals.get("total_tokens", 0)),
        )
        self._record(f"proposal-{sample_index}.json", {
            "schema": "jiuwenswarm/clarification-native-evidence/v1", "stage": stage,
            "session_id": session_id, "snapshot_id": self.snapshot.snapshot_id,
            "structured_output": transport,
            "proposal": proposal.model_dump(mode="json") if proposal is not None else None,
            "investigation": investigation.model_dump(mode="json"), "usage": usages,
        })
        return ProposalModelResult(proposal=proposal, investigation=investigation, usage_details=tuple(usages), error="; ".join(errors))

    async def select(self, prompt: str) -> tuple[ClarificationSelectorDecision, dict[str, object]]:
        return await self._structured("clarification-selector", prompt, response_format=ClarificationSelectorDecision, instructions=selector_instructions())

    async def propose_legacy_v1(self, prompt: str, *, sample_index: int) -> tuple[LegacyV1ClarificationProposal, dict[str, object]]:
        return await self._structured(f"clarification-legacy-proposal-{sample_index}", prompt,
            response_format=LegacyV1ClarificationProposal, instructions=legacy_v1_proposal_instructions())

    async def select_legacy_v1(self, prompt: str) -> tuple[ClarificationSelection, dict[str, object]]:
        return await self._structured("clarification-legacy-selector", prompt,
            response_format=ClarificationSelection, instructions=legacy_v1_selector_instructions())

    async def generate_pact_goal_contract(self, prompt: str) -> tuple[PactGoalContract, dict[str, object]]:
        return await self._structured("pact-goal-contract", prompt, response_format=PactGoalContract, instructions=pact_goal_contract_instructions())

    async def generate_pact_initial_plan(self, prompt: str) -> tuple[PactInitialPlan, dict[str, object]]:
        return await self._structured("pact-initial-plan", prompt, response_format=PactInitialPlan, instructions=pact_initial_plan_instructions())

    async def generate_public_verification(self, prompt: str, *, acceptance_criteria: tuple[PactAcceptanceCriterion, ...]) -> tuple[PublicVerification, dict[str, object]]:
        if not acceptance_criteria or len({item.id for item in acceptance_criteria}) != len(acceptance_criteria):
            raise ValueError("public verification requires nonempty unique acceptance criteria")
        checks = []
        usages = []
        for index, criterion in enumerate(acceptance_criteria, 1):
            criterion_prompt = (prompt + "\n\nGenerate checks only for this acceptance criterion: "
                                + criterion.model_dump_json())
            validate = lambda value, ac_id=criterion.id: value.validate_coverage({ac_id})
            proposal, usage = await self._structured(f"public-verification-ac{index}", criterion_prompt,
                response_format=PublicVerification, instructions=PUBLIC_VERIFICATION_INSTRUCTIONS,
                require_investigation=True, validate=validate)
            usages.append(usage)
            reviewed, usage = await self._structured(f"public-verification-review-ac{index}", criterion_prompt
                + "\n\nUntrusted proposed public checks to audit against frozen source:\n"
                + proposal.model_dump_json(by_alias=True), response_format=PublicVerification,
                instructions=PUBLIC_VERIFICATION_REVIEW_INSTRUCTIONS, require_investigation=True, validate=validate)
            usages.append(usage)
            checks.extend(check.model_copy(update={"id": f"ac{index}-check{check_index}"})
                          for check_index, check in enumerate(reviewed.checks, 1))
        verification = PublicVerification(schema="jiuwenswarm/pact-public-verification/v1", status="ready",
            reason="Each criterion was investigated and independently reviewed by native JiuwenSwarm sessions on frozen public source.", checks=checks)
        verification.validate_coverage({item.id for item in acceptance_criteria})
        return verification, _sum_usage(usages)
