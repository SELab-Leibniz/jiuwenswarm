# Copyright (c) 2026 Chrys. All rights reserved.

"""Portable early-suite stages on the pinned Core's modern Work authority."""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from pact_core.artifacts import read_json, write_json_atomic
from pact_core.benchmark.budget import BudgetedAdapter
from pact_core.benchmark.driver import BenchDriver
from pact_core.runtime import RuntimeContractError, RuntimeStateError
from pact_core.runtime.planning import initial_plan_payload, validate_initial_plan
from pact_core.runtime.schemas import goal_contract_criteria, validate_goal_contract
from pact_core.schemas import TurnRequest

from .evidence import contained, sha256_bytes
from .localization import localize_suite
from .ports import ClarificationContext
from .side_calls import SideCallFailure, view_identity
from .worker_evidence import worker_evidence_guidance


def parse_object(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    value = json.loads(text)
    if not isinstance(value, dict):
        raise RuntimeContractError("expected a JSON object")
    return value


def validate_pair(value: dict[str, Any]) -> dict[str, Any]:
    contract = validate_goal_contract(value["goal_contract"])
    plan = validate_initial_plan(
        value["initial_plan"], acceptance_criterion_ids=tuple(row["id"] for row in goal_contract_criteria(contract))
    )
    owners = [ac for mission in plan.missions for ac in mission.target_ac_ids]
    if len(owners) != len(set(owners)):
        raise RuntimeContractError("every acceptance criterion must have exactly one owning mission")
    result = {"goal_contract": contract, "initial_plan": initial_plan_payload(plan)}
    if "public_verification" in value:
        verification = value["public_verification"]
        if not isinstance(verification, dict) or verification.get("status") not in ("ready", "unavailable"):
            raise RuntimeContractError("invalid public verification proposal")
        checks = verification.get("checks", [])
        if not isinstance(checks, list) or len(checks) > 100:
            raise RuntimeContractError("invalid public verification checks")
        if verification["status"] == "ready":
            covered, names = set(), set()
            for check in checks:
                if not isinstance(check, dict):
                    raise RuntimeContractError("public check must be an object")
                name, command, targets = check.get("id"), check.get("command"), check.get("target_ac_ids")
                if (
                    not isinstance(name, str)
                    or not name.strip()
                    or name in names
                    or not isinstance(command, str)
                    or not command.strip()
                    or len(command) > 16000
                    or "\x00" in command
                    or command.strip() in ("true", ":", "exit 0", "TODO", "PLACEHOLDER")
                    or re.search(r"\|\|\s*(true|:)\s*;?\s*$", command, re.MULTILINE)
                    or not isinstance(targets, list)
                    or len(targets) != 1
                    or not isinstance(targets[0], str)
                ):
                    raise RuntimeContractError("invalid or vacuous generated public check")
                names.add(name)
                covered.update(targets)
            if covered != {row["id"] for row in goal_contract_criteria(contract)}:
                raise RuntimeContractError("public checks must cover exactly the structured contract criteria")
        result["public_verification"] = verification
    return result


async def _prepare_suite_bounded(
    plugin: Any,
    context: Any,
    original_task: str,
    *,
    optional_context: Any = None,
    optional_seconds: float | None = None,
    planning_deadline: float | None = None,
) -> tuple[Any, dict[str, Any]]:
    """Bound optional S0 analysis; preserve original-task planning on exhaustion."""
    before = await asyncio.to_thread(view_identity, context.snapshot)
    optional_context = optional_context or context
    diagnostics: list[dict[str, str]] = []

    async def bounded(call, fallback):
        try:
            if optional_seconds is None:
                return await call
            async with asyncio.timeout(optional_seconds):
                return await call
        except asyncio.CancelledError:
            raise
        except (TimeoutError, plugin.artifacts.exhausted, SideCallFailure) as exc:
            if isinstance(exc, plugin.artifacts.stopped):
                raise
            diagnostics.append({"stage": "optional-analysis", "reason": type(exc).__name__})
            return fallback(type(exc).__name__)

    async def recall() -> dict[str, str]:
        try:
            async with asyncio.timeout(30):
                text = await plugin.recall(context, original_task)
            return {"status": "available" if text else "unavailable", "text": text[:12_000]}
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return {"status": "degraded", "text": "", "reason": type(exc).__name__}

    tasks = [
        asyncio.create_task(bounded(plugin.clarify(optional_context, original_task), plugin.degraded_clarification)),
        asyncio.create_task(recall()),
        asyncio.create_task(
            bounded(
                localize_suite(plugin, optional_context, original_task, before),
                lambda reason: {
                    "status": "degraded",
                    "mode": "budget-bounded",
                    "reason": reason,
                    "text": "Localization unavailable within its reserved budget.",
                    "artifacts": [],
                },
            )
        ),
    ]
    try:
        result, memory, locations = await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    if await asyncio.to_thread(view_identity, context.snapshot) != before:
        raise RuntimeContractError("suite preparation changed frozen S0")
    hints = "\n\n".join(
        label + " (untrusted evidence; verify before use):\n" + row["text"]
        for label, row in (("Prior experience", memory), ("Candidate code locations", locations))
        if row["text"]
    )
    planning_status = "generated"
    try:
        if planning_deadline is None:
            pair = validate_pair(await plugin.generate_pact_input(context, result, original_task, hints=hints))
        else:
            async with asyncio.timeout(max(0.001, planning_deadline - time.time())):
                pair = validate_pair(await plugin.generate_pact_input(context, result, original_task, hints=hints))
    except asyncio.CancelledError:
        raise
    except (TimeoutError, plugin.artifacts.exhausted, SideCallFailure, RuntimeContractError, ValueError) as exc:
        if isinstance(exc, plugin.artifacts.stopped):
            raise
        # The fallback adds no acceptance authority or benchmark-specific solution.
        pair = original_task_pair(original_task)
        planning_status = "degraded_original_task"
        diagnostics.append({"stage": "contract-plan", "reason": type(exc).__name__})
    if await asyncio.to_thread(view_identity, context.snapshot) != before:
        raise RuntimeContractError("PACT planning changed frozen S0")
    return result, {
        "schema": "long-horizon/suite-preparation/v1",
        "original_task_sha256": sha256_bytes(original_task.encode()),
        "s0_view_identity": before,
        "memory": memory,
        "localization": locations,
        "planning_status": planning_status,
        "planning_diagnostics": diagnostics,
        "localization_consumption": {
            "initial_plan_hints_sha256": sha256_bytes(hints.encode()),
            "localization_text_sha256": sha256_bytes(locations["text"].encode()),
            "initial_plan_hints_include_localization": locations["text"] in hints,
        },
        **pair,
    }


def original_task_pair(original_task: str) -> dict[str, Any]:
    """A lossless authority fallback, not invented requirements or task answers."""
    return validate_pair(
        {
            "goal_contract": {
                "schema": "pact-runtime/goal-contract/v1",
                "goal": original_task,
                "acceptance_criteria": [
                    {
                        "id": "original-task-complete",
                        "text": "Fulfil every requirement of the authoritative original task.",
                    }
                ],
                "non_goals": [],
            },
            "initial_plan": {
                "schema": "pact-runtime/initial-plan/v1",
                "constraints": [],
                "missions": [
                    {
                        "id": "complete-original-task",
                        "objective": "Complete the authoritative original task.",
                        "target_ac_ids": ["original-task-complete"],
                        "dependencies": [],
                        "verification_intent": "Run supplied public checks and independently verify every original requirement.",
                    }
                ],
            },
        }
    )


def bind_preparation(controller: Any, runtime: Any, output: Path, turn_root: Path, state: Any, task: str) -> Any:
    artifact = turn_root / "suite-preparation.json"
    if artifact.is_symlink():
        raise RuntimeContractError("suite preparation must be a regular file")
    payload = read_json(artifact)
    expected = controller._stages(state)["clarification"].get("suite_preparation_sha256")
    if sha256_bytes(artifact.read_bytes()) != expected:
        raise RuntimeStateError("suite preparation changed after clarification")
    if payload.get("original_task_sha256") != sha256_bytes(task.encode()):
        raise RuntimeContractError("suite preparation belongs to another original task")
    snapshot = controller._artifacts.load_stage_snapshot(output, controller._stages(state)["s0"])
    if payload.get("s0_view_identity") != view_identity(snapshot):
        raise RuntimeStateError("suite S0 evidence changed")
    pair = validate_pair(payload)
    graph_artifacts = []
    for receipt in payload["localization"].get("artifacts", []):
        path = Path(receipt["path"])
        relative = contained(output, path)
        if path.is_symlink() or sha256_bytes(path.read_bytes()) != receipt["sha256"]:
            raise RuntimeStateError("graph localization evidence changed")
        graph_artifacts.append({"path": relative, "sha256": receipt["sha256"]})
    input_dir = turn_root / "06-pact-input"
    write_json_atomic(input_dir / "goal-contract.json", pair["goal_contract"])
    write_json_atomic(input_dir / "initial-plan.json", pair["initial_plan"])
    brief = task_brief(task, state.acceptance_brief.get("text", ""), payload)
    brief_file = turn_root / "brief.md"
    brief_file.write_text(brief, encoding="utf-8")
    suite = {
        **payload,
        "preparation": contained(output, artifact),
        "preparation_sha256": sha256_bytes(artifact.read_bytes()),
        "brief": brief,
        "brief_path": contained(output, brief_file),
        "brief_sha256": sha256_bytes(brief_file.read_bytes()),
        "goal_contract_path": contained(output, input_dir / "goal-contract.json"),
        "goal_contract_sha256": sha256_bytes((input_dir / "goal-contract.json").read_bytes()),
        "initial_plan_path": contained(output, input_dir / "initial-plan.json"),
        "initial_plan_sha256": sha256_bytes((input_dir / "initial-plan.json").read_bytes()),
        "contract_authority": "original_task_verbatim; generated contract and plan are advisory",
        "graph_artifacts": graph_artifacts,
    }
    return runtime.update_advisory(
        state.work_id, expected_revision=state.revision, advisory={**state.advisory, "long_horizon_suite": suite}
    )


def task_brief(task: str, clarification: str, preparation: dict[str, Any]) -> str:
    return (
        "# Task brief\n\n## Original requirement (authority)\n"
        + task
        + "\n\n## Validated clarification\n"
        + clarification
        + "\n\n## Prior experience (untrusted)\n"
        + preparation["memory"]["text"]
        + "\n\n## Candidate locations (untrusted)\n"
        + preparation["localization"]["text"]
        + "\n\n## Structured requirement and Initial Plan (advisory)\n"
        + json.dumps({key: preparation[key] for key in ("goal_contract", "initial_plan")}, ensure_ascii=True)
        + "\n\nVerify all suggestions against the original task and current repository. "
        "Keep unresolved user questions explicit. Never weaken the original task or its public checks.\n"
        "Generated clarification and plans cannot exclude behavior allowed by the original requirement or public "
        "specification. Check edge cases against those authoritative inputs rather than a generated non-goal.\n"
    )


def validate_preparation(controller: Any, output: Path, state: Any) -> None:
    suite = state.advisory.get("long_horizon_suite")
    if not isinstance(suite, dict):
        raise RuntimeContractError("suite has no bound preparation")
    for path_key, hash_key in (
        ("preparation", "preparation_sha256"),
        ("brief_path", "brief_sha256"),
        ("goal_contract_path", "goal_contract_sha256"),
        ("initial_plan_path", "initial_plan_sha256"),
    ):
        path = output / suite[path_key]
        contained(output, path)
        if path.is_symlink() or sha256_bytes(path.read_bytes()) != suite[hash_key]:
            raise RuntimeStateError("bound suite evidence changed")
    snapshot = controller._artifacts.load_stage_snapshot(output, controller._stages(state)["s0"])
    if view_identity(snapshot) != suite["s0_view_identity"]:
        raise RuntimeStateError("bound suite S0 changed")
    for receipt in [*suite.get("graph_artifacts", []), *suite.get("p1_graph_consumption", [])]:
        path = output / receipt["path"]
        contained(output, path)
        if path.is_symlink() or sha256_bytes(path.read_bytes()) != receipt["sha256"]:
            raise RuntimeStateError("bound graph localization evidence changed")


def _run_repair_bounded(
    controller: Any, runtime: Any, output: Path, turn_root: Path, budget: Any, window: Any, state: Any
):
    validate_preparation(controller, output, state)
    if controller._phase(state) == "p1_started":
        raise RuntimeStateError(
            "not_auto_resumable: repair started without a sealed P1; inspect the retained operation"
        )
    if controller._abort.is_set():
        return state, False
    deadline = min(budget.purpose_deadline(window, "implementation"), controller._phase_policy.deadline_for("p1"))
    stop = budget.stop_reason(deadline, purpose="implementation")
    if stop:
        controller._invocation.stop_reason = stop
        return state, False
    prompt = (
        "[SUITE_P1_REPAIR]\nRepair the saved P0 using the clarification and Initial Plan below. "
        "This is a separate implementation pass before the first PACT review. Preserve correct P0 behavior. "
        "Run relevant public checks; report PACT_REVIEW_READY on a line of its own when ready.\n\n"
        + state.advisory["long_horizon_suite"]["brief"]
        + "\n\n"
        + controller._gate.feedback(state.candidate)
        + worker_evidence_guidance()
    )
    state = controller._advance(runtime, state, "p1_started")
    handle = runtime.begin_execution(state.work_id, expected_revision=state.revision, context=prompt, draft=True)
    wrapped = BudgetedAdapter(controller._adapters[0], budget, purpose="implementation", deadline=deadline)
    try:
        result = wrapped.run_turn(
            TurnRequest(
                role="worker",
                prompt=prompt,
                workdir=handle.workdir,
                timeout_seconds=controller._args.turn_timeout_seconds,
                artifact_dir=handle.artifact_dir,
            )
        )
        if controller._adapters[0].budget_failure == "TurnBudgetExhausted":
            controller._raise_native_turn_refusal(window, budget)
        graph_consumption = []
        if wrapped.results:
            consumption_file = turn_root / "p1-graph-consumption.json"
            write_json_atomic(
                consumption_file,
                {
                    "schema": "long-horizon/graph-consumption/v1",
                    "stage": "p1-worker-adapter",
                    "prompt_sha256": sha256_bytes(prompt.encode()),
                    "localization_text_sha256": sha256_bytes(
                        state.advisory["long_horizon_suite"]["localization"]["text"].encode()
                    ),
                    "graph_artifacts": state.advisory["long_horizon_suite"]["graph_artifacts"],
                    "prompt_includes_localization": state.advisory["long_horizon_suite"]["localization"]["text"]
                    in prompt,
                    "status": result.status,
                },
            )
            graph_consumption = [
                {"path": contained(output, consumption_file), "sha256": sha256_bytes(consumption_file.read_bytes())}
            ]
        if not wrapped.results:
            controller._invocation.stop_reason = wrapped.stopped or "repair_not_started"
            return state, False
        # Retain a partial draft for inspection, but only public validation can
        # make it export-eligible. Never replay its calls or tool side effects.
        if result.status != "completed":
            controller._invocation.stop_reason = f"repair_{result.status}_publicly_checked_fallback"
        progress = dict(state.advisory["benchmark"])
        progress.update(
            worker_turns=int(progress.get("worker_turns", 0)) + 1,
            ready=bool(re.search(r"(?im)^\s*PACT_REVIEW_READY\s*$", result.final_text)),
        )
        state = runtime.save_candidate(
            handle,
            summary=result.final_text,
            advisory={
                **state.advisory,
                "benchmark": progress,
                "long_horizon_suite": {
                    **state.advisory["long_horizon_suite"],
                    "p1_graph_consumption": graph_consumption,
                },
            },
        )
    finally:
        handle.close()
    state = controller._check_candidate(runtime, state, "p1")
    receipt = runtime.export_candidate(state.candidate, output / "stages" / f"p1-{uuid4().hex}")
    record, _ = controller._artifacts.stage_record(
        output_root=output,
        turn_root=turn_root,
        stage="p1",
        receipt=receipt,
        metadata={"work_id": state.work_id, "source": "separate P1 repair after S0 clarification"},
    )
    verification = state.advisory["long_horizon_suite"].get("public_verification", {})
    if verification.get("status") == "ready":
        state = runtime.register_checks(
            state.work_id,
            expected_revision=state.revision,
            checks=[
                {"name": "suite-" + check["id"], "command": check["command"], "requirement": state.contract["goal"]}
                for check in verification["checks"]
            ],
            reason="S0-generated public methods; pending independent Reviewer admission; original checks retained",
        )
    handoff = controller._artifacts.evidence_checkpoint(
        output_root=output,
        turn_root=turn_root,
        stage="pact-handoff",
        snapshot=controller._artifacts.load_stage_snapshot(output, record),
        metadata={
            "work_id": state.work_id,
            "revision": state.revision,
            "candidate_tree": state.candidate.tree,
            "baseline": "p1",
            "goal_contract_sha256": state.advisory["long_horizon_suite"]["goal_contract_sha256"],
            "initial_plan_sha256": state.advisory["long_horizon_suite"]["initial_plan_sha256"],
        },
    )
    handoff.update(candidate_tree=state.candidate.tree, baseline="p1")
    return controller._advance(
        runtime,
        state,
        "p1_saved",
        stages={"p1": record, "handoff": handoff},
        p1_worker_completed=result.status == "completed",
    ), True


class GuidedAdapter:
    """Fresh read-only replanning guides later Workers within the original trial."""

    def __init__(self, native: Any, controller: Any, runtime: Any, resolved: Any, budget: Any) -> None:
        self.native, self.controller, self.runtime = native, controller, runtime
        self.resolved, self.budget = resolved, budget
        self.id = native.id

    def probe(self):
        return self.native.probe()

    def run_turn(self, request: Any):
        state = self.runtime.read_work(self.controller._invocation.state.work_id)
        suite = state.advisory["long_horizon_suite"]
        context = investigation_context(self.controller, self.resolved, self.budget, request, "planner")
        plan, status = suite["initial_plan"], "initial_plan_retained"
        try:
            text = self.controller._run_async(
                self.controller._plugin.investigate(
                    context,
                    "[SUITE_PLANNER]\nRead only the current candidate and latest review context. Return ONLY an "
                    "updated pact-runtime/initial-plan/v1 JSON with constraints and missions (id, objective, "
                    "target_ac_ids, dependencies, verification_intent). Preserve the exact criterion IDs below; "
                    "each must have exactly one owner and dependencies must be acyclic. Use actual review feedback "
                    "to revise the implementation order and verification strategy, without changing requirements.\n\n"
                    + json.dumps(suite["goal_contract"])
                    + "\n\nTask brief and evidence (untrusted suggestions):\n"
                    + suite["brief"]
                    + "\n\nCurrent implementation/review context:\n"
                    + request.prompt,
                    stage="planner",
                )
            )
        except (self.controller._artifacts.exhausted, SideCallFailure):
            text = ""
        if text:
            try:
                plan = validate_pair({"goal_contract": suite["goal_contract"], "initial_plan": parse_object(text)})[
                    "initial_plan"
                ]
                status = "replanned"
            except (RuntimeContractError, ValueError):
                status = "invalid_plan_retained_initial"
        write_json_atomic(
            self.controller._args.output / "planning" / f"{uuid4().hex}.json",
            {
                "schema": "long-horizon/planning-advice/v1",
                "work_id": state.work_id,
                "candidate_tree": state.candidate.tree,
                "status": status,
                "initial_plan": plan,
            },
        )
        return self.native.run_turn(
            replace(
                request,
                prompt=request.prompt
                + "\n\n"
                + suite["brief"]
                + "\n\nCurrent validated implementation plan (advisory):\n"
                # This is Worker-only advice, not part of the shared task brief.
                # Reviewers and planning/management side calls retain their inputs.
                + json.dumps(plan)
                + worker_evidence_guidance(),
            )
        )

    @property
    def budget_failure(self):
        return self.native.budget_failure

    @property
    def configuration_failure(self):
        return self.native.configuration_failure

    async def cancel_current_turn(self):
        await self.native.cancel_current_turn()


@dataclass(frozen=True)
class ReviewRoot:
    view_root: str


@dataclass(frozen=True)
class ReviewSnapshot:
    roots: tuple[ReviewRoot, ...]


def investigation_context(controller: Any, resolved: Any, budget: Any, request: Any, stage: str):
    deadline = request.deadline if request.deadline is not None else budget.snapshot()["deadline"]
    if controller._phase_policy is not None:
        deadline = min(deadline, controller._phase_policy.deadline_for(stage))
    admission = controller._artifacts.make_admission(
        budget,
        deadline=deadline,
        workdir=request.workdir,
        adapter_id="suite-" + stage + "-" + resolved.fingerprint,
        stop_requested=controller._abort.is_set,
    )
    return ClarificationContext(
        profile=resolved.model_context,
        snapshot=ReviewSnapshot((ReviewRoot(str(request.workdir)),)),
        session_id=controller._invocation.invocation_id + "-" + stage,
        session_dir=controller._args.output / stage,
        admission=admission,
        deadline=deadline,
    )


class ManagedReviewer:
    """Fresh read-only management advice precedes review; Core alone decides acceptance."""

    def __init__(self, native: Any, controller: Any, runtime: Any, resolved: Any, budget: Any) -> None:
        self.native, self.controller, self.runtime = native, controller, runtime
        self.resolved, self.budget = resolved, budget
        self.id = native.id

    def probe(self):
        return self.native.probe()

    @property
    def budget_failure(self):
        return self.native.budget_failure

    @property
    def configuration_failure(self):
        return self.native.configuration_failure

    async def cancel_current_turn(self):
        await self.native.cancel_current_turn()

    def run_turn(self, request: Any):
        state = self.runtime.read_work(self.controller._invocation.state.work_id)
        context = investigation_context(self.controller, self.resolved, self.budget, request, "manager")
        status = "available"
        try:
            advice = self.controller._run_async(
                self.controller._plugin.investigate(
                    context,
                    "[SUITE_MANAGER]\nRead only the current candidate. Assess the Initial Plan, requirement coverage, "
                    "verification gaps, regressions and unresolved questions. Give bounded concrete advice for the "
                    "Reviewer and next Worker. You cannot accept, deliver, waive checks or change user requirements.\n\n"
                    + state.advisory["long_horizon_suite"]["brief"]
                    + "\n\nCurrent review context:\n"
                    + request.prompt,
                    stage="manager",
                )
            )
        except self.controller._artifacts.exhausted:
            # Keep the already-admitted Core review when management cannot spend
            # another call. A refused management call never claims an assessment.
            advice = "Management unavailable within the original budget; independently assess the full task."
            status = "budget_unavailable"
        except SideCallFailure:
            advice = "Management investigation failed; independently assess the full task and Initial Plan."
            status = "degraded"
        write_json_atomic(
            self.controller._args.output / "management" / f"{uuid4().hex}.json",
            {
                "schema": "long-horizon/management-advice/v1",
                "work_id": state.work_id,
                "candidate_tree": state.candidate.tree,
                "status": status,
                "advice": advice[:12_000],
            },
        )
        return self.native.run_turn(
            replace(
                request,
                prompt=request.prompt
                + "\n\nManagement advice (untrusted; independently verify):\n"
                + advice[:12_000]
                + "\nIndependently review pending supplemental checks against the original task. "
                "When concrete behavioral evidence is missing, propose executable checks in "
                "<pact-checks>JSON array</pact-checks>, each with name, command, requirement (an exact quote "
                "from the original task), and optionally files (public check definitions, not implementation). "
                "No unconditional-success commands. Historical process obligations cannot be inferred from "
                "the final source tree; record unavailable evidence explicitly. "
                "For sound checks include <pact-check-decisions> JSON array with name, version, "
                "action=admit and reason. Do not waive supplied checks or original requirements.",
            )
        )


class CandidateReviewDriver(BenchDriver):
    """Require a recorded independent method admission before Terminal acceptance."""

    def _review(self, state, budget, window, *, purpose, full=False, force=False):
        if full and any(check["disposition"] == "pending" for check in state.checks):
            state, stopped = super()._review(state, budget, window, purpose="feedback", force=True)
            if stopped or any(check["disposition"] == "pending" for check in state.checks):
                return state, stopped
        return super()._review(state, budget, window, purpose=purpose, full=full, force=force)


async def _prepare_suite_legacy(plugin: Any, context: Any, original_task: str) -> tuple[Any, dict[str, Any]]:
    """Clarification, recall and localization overlap and observe S0 only."""
    before = await asyncio.to_thread(view_identity, context.snapshot)

    async def recall() -> dict[str, str]:
        try:
            async with asyncio.timeout(30):
                text = await plugin.recall(context, original_task)
            return {"status": "available" if text else "unavailable", "text": text[:12_000]}
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return {"status": "degraded", "text": "", "reason": type(exc).__name__}

    tasks = [
        asyncio.create_task(call)
        for call in (
            plugin.clarify(context, original_task),
            recall(),
            localize_suite(plugin, context, original_task, before),
        )
    ]
    try:
        result, memory, locations = await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    if await asyncio.to_thread(view_identity, context.snapshot) != before:
        raise RuntimeContractError("suite preparation changed frozen S0")
    hints = "\n\n".join(
        label + " (untrusted evidence; verify before use):\n" + row["text"]
        for label, row in (("Prior experience", memory), ("Candidate code locations", locations))
        if row["text"]
    )
    pair = validate_pair(await plugin.generate_pact_input(context, result, original_task, hints=hints))
    if await asyncio.to_thread(view_identity, context.snapshot) != before:
        raise RuntimeContractError("PACT planning changed frozen S0")
    return result, {
        "schema": "long-horizon/suite-preparation/v1",
        "original_task_sha256": sha256_bytes(original_task.encode()),
        "s0_view_identity": before,
        "memory": memory,
        "localization": locations,
        "localization_consumption": {
            "initial_plan_hints_sha256": sha256_bytes(hints.encode()),
            "localization_text_sha256": sha256_bytes(locations["text"].encode()),
            "initial_plan_hints_include_localization": locations["text"] in hints,
        },
        **pair,
    }


def _run_repair_legacy(
    controller: Any, runtime: Any, output: Path, turn_root: Path, budget: Any, window: Any, state: Any
):
    validate_preparation(controller, output, state)
    if controller._phase(state) == "p1_started":
        raise RuntimeStateError(
            "not_auto_resumable: repair started without a sealed P1; inspect the retained operation"
        )
    if controller._abort.is_set():
        return state, False
    deadline = budget.purpose_deadline(window, "implementation")
    stop = budget.stop_reason(deadline, purpose="implementation")
    if stop:
        controller._invocation.stop_reason = stop
        return state, False
    prompt = (
        "[SUITE_P1_REPAIR]\nRepair the saved P0 using the clarification and Initial Plan below. "
        "This is a separate implementation pass before the first PACT review. Preserve correct P0 behavior. "
        "Run relevant public checks; report PACT_REVIEW_READY on a line of its own when ready.\n\n"
        + state.advisory["long_horizon_suite"]["brief"]
        + worker_evidence_guidance()
    )
    state = controller._advance(runtime, state, "p1_started")
    handle = runtime.begin_execution(state.work_id, expected_revision=state.revision, context=prompt, draft=True)
    wrapped = BudgetedAdapter(controller._adapters[0], budget, purpose="implementation", deadline=deadline)
    try:
        result = wrapped.run_turn(
            TurnRequest(
                role="worker",
                prompt=prompt,
                workdir=handle.workdir,
                timeout_seconds=controller._args.turn_timeout_seconds,
                artifact_dir=handle.artifact_dir,
            )
        )
        if controller._adapters[0].budget_failure == "TurnBudgetExhausted":
            controller._raise_native_turn_refusal(window, budget)
        graph_consumption = []
        if wrapped.results:
            consumption_file = turn_root / "p1-graph-consumption.json"
            write_json_atomic(
                consumption_file,
                {
                    "schema": "long-horizon/graph-consumption/v1",
                    "stage": "p1-worker-adapter",
                    "prompt_sha256": sha256_bytes(prompt.encode()),
                    "localization_text_sha256": sha256_bytes(
                        state.advisory["long_horizon_suite"]["localization"]["text"].encode()
                    ),
                    "graph_artifacts": state.advisory["long_horizon_suite"]["graph_artifacts"],
                    "prompt_includes_localization": state.advisory["long_horizon_suite"]["localization"]["text"]
                    in prompt,
                    "status": result.status,
                },
            )
            graph_consumption = [
                {"path": contained(output, consumption_file), "sha256": sha256_bytes(consumption_file.read_bytes())}
            ]
        if not wrapped.results or result.status != "completed":
            # No save means Core retains P0 as its canonical candidate. The unfinished
            # operation remains inspectable; neither a retry nor a resume replays P0.
            controller._invocation.stop_reason = wrapped.stopped or f"repair_{result.status}_retained_p0"
            return state, bool(wrapped.results)
        progress = dict(state.advisory["benchmark"])
        progress.update(
            worker_turns=int(progress.get("worker_turns", 0)) + 1,
            ready=bool(re.search(r"(?im)^\s*PACT_REVIEW_READY\s*$", result.final_text)),
        )
        state = runtime.save_candidate(
            handle,
            summary=result.final_text,
            advisory={
                **state.advisory,
                "benchmark": progress,
                "long_horizon_suite": {
                    **state.advisory["long_horizon_suite"],
                    "p1_graph_consumption": graph_consumption,
                },
            },
        )
    finally:
        handle.close()
    receipt = runtime.export_candidate(state.candidate, output / "stages" / f"p1-{uuid4().hex}")
    record, _ = controller._artifacts.stage_record(
        output_root=output,
        turn_root=turn_root,
        stage="p1",
        receipt=receipt,
        metadata={"work_id": state.work_id, "source": "separate P1 repair after S0 clarification"},
    )
    verification = state.advisory["long_horizon_suite"].get("public_verification", {})
    if verification.get("status") == "ready":
        state = runtime.register_checks(
            state.work_id,
            expected_revision=state.revision,
            checks=[
                {"name": "suite-" + check["id"], "command": check["command"], "requirement": state.contract["goal"]}
                for check in verification["checks"]
            ],
            reason="S0-generated public methods; pending independent Reviewer admission; original checks retained",
        )
    handoff = controller._artifacts.evidence_checkpoint(
        output_root=output,
        turn_root=turn_root,
        stage="pact-handoff",
        snapshot=controller._artifacts.load_stage_snapshot(output, record),
        metadata={
            "work_id": state.work_id,
            "revision": state.revision,
            "candidate_tree": state.candidate.tree,
            "baseline": "p1",
            "goal_contract_sha256": state.advisory["long_horizon_suite"]["goal_contract_sha256"],
            "initial_plan_sha256": state.advisory["long_horizon_suite"]["initial_plan_sha256"],
        },
    )
    handoff.update(candidate_tree=state.candidate.tree, baseline="p1")
    return controller._advance(runtime, state, "p1_saved", stages={"p1": record, "handoff": handoff}), True


async def prepare_suite(plugin: Any, context: Any, original_task: str, **bounded: Any):
    if not bounded:
        return await _prepare_suite_legacy(plugin, context, original_task)
    return await _prepare_suite_bounded(plugin, context, original_task, **bounded)


def run_repair(controller: Any, runtime: Any, output: Path, turn_root: Path, budget: Any, window: Any, state: Any):
    implementation = _run_repair_bounded if controller._phase_policy is not None else _run_repair_legacy
    return implementation(controller, runtime, output, turn_root, budget, window, state)
