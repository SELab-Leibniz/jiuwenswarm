# Copyright (c) 2026 Chrys. All rights reserved.

"""Portable early-suite stages on the pinned Core's modern Work authority."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from jiuwenswarm.pact_core.artifacts import read_json, write_json_atomic
from jiuwenswarm.pact_core.runtime import RuntimeContractError, RuntimeStateError
from jiuwenswarm.pact_core.runtime.planning import initial_plan_payload, validate_initial_plan
from jiuwenswarm.pact_core.runtime.schemas import goal_contract_criteria, validate_goal_contract
from jiuwenswarm.pact_core.schemas import TurnRequest, TurnResult

from .budget_boundary import BudgetedAdapter
from .budget_origin import validate_cutoff
from .capacity import model_content_limit
from .confirmed_behavior import CONFIRMED_BEHAVIOR_CHECK
from .evidence import contained, sha256_bytes
from .localization import localize_suite
from .planning_checkpoint import PlanningCheckpointError
from .ports import ClarificationContext
from .public_review import (
    PublicReviewProgress,
    capacity_os_error,
    clear_public_review,
    public_review_failure,
    recoverable_public_review_error,
    started_public_review,
    storage_capacity_progress,
    with_reason,
)
from .review_limits import ReviewLimitedDriver
from .reviewer_execution import NonDegradableIntegrityError, release_recovered_views
from .side_calls import (
    MAX_SIDE_CALL_INTEGRITY_RECOVERIES as _MAX_SIDE_CALL_INTEGRITY_RECOVERIES,
)
from .side_calls import (
    SideCallFailure,
    SideCallIntegrityError,
    recover_side_call_integrity,
    view_identity,
)
from .transport_failure import transport_failure_evidence
from .worker_evidence import (
    reviewer_delivery_hygiene_guidance,
    reviewer_ui_state_evidence_guidance,
    worker_evidence_guidance,
)

MAX_SIDE_CALL_INTEGRITY_RECOVERIES = _MAX_SIDE_CALL_INTEGRITY_RECOVERIES


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


def _full_public_review_incomplete(value: dict[str, Any]) -> str | None:
    """A parallel native review is all-or-nothing before binding and P1."""
    verification = value.get("public_verification")
    if not isinstance(verification, dict) or verification.get("requires_complete_review") is not True:
        return None
    if verification.get("status") == "ready":
        return None
    degradation = verification.get("degradation")
    reason = degradation.get("reason") if isinstance(degradation, dict) else "incomplete"
    return str(reason or "incomplete")


def _incomplete_public_review(value: dict[str, Any]) -> PublicReviewProgress | None:
    reason = _full_public_review_incomplete(value)
    if reason is None:
        return None
    verification = value.get("public_verification")
    degradation = verification.get("degradation") if isinstance(verification, dict) else None
    detail = degradation.get("detail") if isinstance(degradation, dict) else ""
    return PublicReviewProgress(
        started=True,
        reason=reason,
        detail=str(detail or "")[:1000],
        degradation=degradation if isinstance(degradation, dict) else None,
    )


def incomplete_public_review(value: Any) -> PublicReviewProgress | None:
    """Return a recoverable reason for incomplete full-public data, if any."""
    return _incomplete_public_review(value) if isinstance(value, dict) else None


def _raise_incomplete_public_review(plugin: Any, value: dict[str, Any]) -> None:
    progress = _incomplete_public_review(value)
    if progress is not None:
        raise recoverable_public_review_error(plugin.artifacts.exhausted, progress)


def _public_review_failure(exc: BaseException) -> PublicReviewProgress | None:
    """Classify an outer failure only if the public-review stage was entered."""
    progress = started_public_review()
    if progress is None:
        return None
    reason = (
        "timeout_before_planning_deadline"
        if isinstance(exc, TimeoutError)
        else "invalid_public_verification_artifact"
    )
    return with_reason(progress, progress.reason or reason, type(exc).__name__)


def _capacity_public_review(
    plugin: Any, exc: BaseException, *, deadline_unix: float | None = None
) -> PublicReviewProgress | None:
    capacity = capacity_os_error(exc)
    if capacity is None:
        return None
    progress = started_public_review()
    if progress is not None:
        return with_reason(progress, "storage_capacity", f"{type(exc).__name__}: {exc}")
    args = getattr(plugin, "_side_args", None)
    return storage_capacity_progress(
        capacity,
        deadline_unix=deadline_unix,
        total_seconds=getattr(args, "budget_seconds", None),
    )


async def _prepare_suite_bounded(
    plugin: Any,
    context: Any,
    original_task: str,
    *,
    optional_context: Any = None,
    optional_seconds: float | None = None,
    planning_deadline: float | None = None,
    cutoff_at_unix: float | None = None,
    clock: Callable[[], float] = time.time,
) -> tuple[Any, dict[str, Any]]:
    """Bound optional S0 analysis; preserve original-task planning on exhaustion."""
    clear_public_review()
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
        except NonDegradableIntegrityError:
            raise
        except (TimeoutError, plugin.artifacts.exhausted, SideCallFailure) as exc:
            if isinstance(exc, plugin.artifacts.stopped):
                raise
            diagnostics.append({"stage": "optional-analysis", "reason": type(exc).__name__})
            return fallback(type(exc).__name__)

    async def recall() -> dict[str, str]:
        cutoff = cutoff_at_unix
        try:
            if cutoff is not None and cutoff <= clock():
                raise TimeoutError("suite recall cutoff elapsed")
            async with asyncio.timeout(30 if cutoff is None else max(0, cutoff - clock())):
                text = await plugin.recall(context, original_task)
            return {"status": "available" if text else "unavailable", "text": text[:model_content_limit(12_000)]}
        except asyncio.CancelledError:
            raise
        except NonDegradableIntegrityError:
            raise
        except Exception as exc:
            if cutoff is not None:
                raise
            return {"status": "degraded", "text": "", "reason": type(exc).__name__}

    tasks = [
        asyncio.create_task(bounded(plugin.clarify(optional_context, original_task), plugin.degraded_clarification)),
        asyncio.create_task(recall()),
        asyncio.create_task(
            bounded(
                localize_suite(
                    plugin, optional_context, original_task, before, cutoff_at_unix=cutoff_at_unix, clock=clock
                ),
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
    except BaseException as exc:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        owner = asyncio.current_task()
        if owner is not None and owner.cancelling() > 0:
            raise asyncio.CancelledError() from exc
        progress = _capacity_public_review(plugin, exc, deadline_unix=planning_deadline)
        if progress is not None:
            raise recoverable_public_review_error(plugin.artifacts.exhausted, progress) from exc
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
            async with asyncio.timeout(max(0.001, planning_deadline - clock())):
                pair = validate_pair(await plugin.generate_pact_input(context, result, original_task, hints=hints))
    except (asyncio.CancelledError, PlanningCheckpointError, SideCallIntegrityError):
        raise
    except NonDegradableIntegrityError:
        raise
    except (TimeoutError, plugin.artifacts.exhausted, SideCallFailure, RuntimeContractError, ValueError) as exc:
        if isinstance(exc, plugin.artifacts.stopped) or public_review_failure(exc) is not None:
            raise
        # An outer planning timeout that fires after the public-review stage
        # began is a public-review timeout, not a contract/plan degradation.
        review_failure = _public_review_failure(exc)
        if review_failure is not None:
            raise recoverable_public_review_error(plugin.artifacts.exhausted, review_failure) from exc
        # The fallback adds no acceptance authority or benchmark-specific solution.
        pair = original_task_pair(original_task)
        planning_status = "degraded_original_task"
        diagnostics.append({"stage": "contract-plan", "reason": type(exc).__name__})
    except OSError as exc:
        progress = _capacity_public_review(plugin, exc, deadline_unix=planning_deadline)
        if progress is None:
            raise
        raise recoverable_public_review_error(plugin.artifacts.exhausted, progress) from exc
    _raise_incomplete_public_review(plugin, pair)
    _note_public_degradation(pair, diagnostics)
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
            "initial_plan_hints_include_localization": bool(locations["text"]) and locations["text"] in hints,
        },
        **pair,
    }


def _note_public_degradation(pair: dict[str, Any], diagnostics: list[dict[str, str]]) -> None:
    """Surface an explicitly degraded (unavailable) generated public verification."""
    verification = pair.get("public_verification")
    if isinstance(verification, dict) and isinstance(verification.get("degradation"), dict):
        diagnostics.append(
            {"stage": "public-verification", "reason": str(verification["degradation"].get("reason", "unavailable"))}
        )


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
    _raise_incomplete_public_review(controller._plugin, payload)
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
    if getattr(state, "delivery", None) is None:
        # A bound preparation written by an earlier version may still carry
        # an incomplete full review. Refuse P1 with the controller's
        # recoverable-exhaustion path, not a runtime-level contract error.
        _raise_incomplete_public_review(controller._plugin, suite)
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


_SUITE_CHECK_REASON = "S0-generated public methods; pending independent Reviewer admission; original checks retained"
_BASH_SHEBANG = re.compile(r"#![ \t]*(?:(/bin/bash|/usr/bin/bash)(?:[ \t]+(-[eEufx]+))?|/usr/bin/env[ \t]+bash)[ \t]*")
_PIPEFAIL_PROLOGUE = re.compile(r"set[ \t]+(?:-[a-zA-Z]+[ \t]+)*-[a-zA-Z]*o[ \t]+pipefail(?=$|[ \t;&|])")


def _suite_shell_command(command: str) -> str:
    """Honor Bash shebangs and strict pipeline prologues in PACT shell strings."""
    if os.name == "posix":
        lines = command.split("\n")
        shebang = _BASH_SHEBANG.fullmatch(lines[0])
        if shebang:
            interpreter, options = shebang.groups()
            args = ["exec", interpreter or "/bin/bash"]
            if options:
                args.append(options)
            return shlex.join([*args, "-c", command])
        for line in lines:
            line = line.lstrip(" \t")
            if not line or line.startswith("#") and not line.startswith("#!"):
                continue
            if _PIPEFAIL_PROLOGUE.match(line):
                # shlex passes the reviewed source to Bash as one literal argv element.
                return shlex.join(["exec", "/bin/bash", "-c", command])
            break
    return command


def register_suite_checks(runtime: Any, state: Any, *, upgrade_registered: bool = False) -> Any:
    """Register S0 methods or upgrade exact active definitions left by a previous controller."""
    verification = state.advisory.get("long_horizon_suite", {}).get("public_verification", {})
    if verification.get("status") != "ready":
        return state
    registered = {row["name"]: row for row in state.checks}
    proposals = []
    for check in verification["checks"]:
        name = "suite-" + check["id"]
        original = check["command"]
        command = _suite_shell_command(original)
        row = registered.get(name)
        if row is None:
            if upgrade_registered:
                continue
        else:
            # A P1 retry can revisit registration after a crash. Preserve reviewed
            # or revised methods and upgrade only the original active definition.
            if (
                command == original
                or row["command"] != original
                or row["source"] != "derived"
                or row["reason"] != _SUITE_CHECK_REASON
                or row["requirement"] != state.contract["goal"]
                or row["files"]
                or row["disposition"] == "suspended"
            ):
                continue
        proposals.append({"name": name, "command": command, "requirement": state.contract["goal"]})
    if not proposals:
        return state
    if upgrade_registered and state.status == "paused":
        state = runtime.resume(state.work_id, expected_revision=state.revision)
    return runtime.register_checks(
        state.work_id,
        expected_revision=state.revision,
        checks=proposals,
        reason=(
            "Select Bash for the bound S0 script; review the revised method" if upgrade_registered else _SUITE_CHECK_REASON
        ),
    )


MIN_RECOVERY_TURN_SECONDS = 60.0


def _model_window_closed(controller: Any) -> bool:
    predicate = getattr(controller, "_model_window_closed", None)
    if callable(predicate):
        return bool(predicate())
    abort = getattr(controller, "_abort", None)
    return bool(abort is not None and abort.is_set())


def _model_window_turn_result() -> TurnResult:
    return TurnResult(
        status="timeout",
        final_text="",
        output_source="budget",
        exit_code=None,
        stderr_tail="budget_exhausted: soft deadline closed model admission",
    )
_RECOVERY_GUIDANCE = (
    "\n\n[TRANSPORT_RECOVERY]\n"
    "The preceding Worker turn ended with a recoverable transport or provider infrastructure failure. "
    "This is a fresh Worker turn from the saved candidate checkpoint, not a replay of the failed native session. "
    "Inspect the existing candidate, preserve correct work, and continue the P1 repair. "
    "Run relevant public checks; report PACT_REVIEW_READY on a line of its own when ready."
)


def _recovery_minimum_seconds(controller: Any) -> float:
    return min(MIN_RECOVERY_TURN_SECONDS, max(1.0, float(controller._args.turn_timeout_seconds)))


def _recovery_remaining_seconds(controller: Any, budget: Any, deadline: float) -> float:
    """Reuse the admitted absolute deadline, including the PR #13 soft clock."""
    boundary = float(deadline)
    soft = getattr(controller, "_soft_deadline", None)
    if soft is not None:
        boundary = min(boundary, float(soft))
    policy = getattr(controller, "_phase_policy", None)
    if policy is not None:
        boundary = min(boundary, policy.deadline_for("p1"))
    return boundary - budget.clock()


def _repair_evidence_dirs(controller: Any) -> tuple[Path, ...]:
    """The native adapter's just-finished turn directory, when it exposes one.

    The Worker adapter may be wrapped more than once (deadline guard, recovered
    view handoff, role boundary), so follow the whole wrapper chain instead of
    assuming a fixed depth.
    """
    adapter = controller._adapters[0]
    seen: set[int] = set()
    output_dir = sequence = role = None
    while adapter is not None and id(adapter) not in seen:
        seen.add(id(adapter))
        if output_dir is None:
            output_dir = getattr(adapter, "output_dir", None)
        if sequence is None:
            sequence = getattr(adapter, "sequence", None)
        if role is None:
            role = getattr(adapter, "semantic_role", None)
        child = getattr(adapter, "native", None)
        if child is None or child is adapter:
            break
        adapter = child
    role = role or "worker"
    directories: list[Path] = []
    if output_dir is not None and isinstance(sequence, int) and not isinstance(sequence, bool):
        for name in (str(role), "worker"):
            directory = Path(output_dir) / name / f"{sequence:04d}"
            if directory not in directories:
                directories.append(directory)
    return tuple(directories)


def _repair_recovery_record(
    *,
    attempts: int,
    max_attempts: int,
    minimum_seconds: float,
    outcome: str,
    incomplete: bool,
    attempt_records: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema": "long-horizon/p1-transport-recovery/v1",
        "policy": "fresh-worker-from-saved-candidate/v1;bounded-attempts;original-deadlines",
        "max_attempts": max_attempts,
        "recovery_attempts": attempts,
        "min_turn_seconds": minimum_seconds,
        "incomplete": incomplete,
        "outcome": outcome,
        "attempts": attempt_records,
    }


def _run_repair_bounded(
    controller: Any, runtime: Any, output: Path, turn_root: Path, budget: Any, window: Any, state: Any
):
    validate_preparation(controller, output, state)
    if controller._phase(state) == "p1_started":
        raise RuntimeStateError(
            "not_auto_resumable: repair started without a sealed P1; inspect the retained operation"
        )
    if controller._abort.is_set() or _model_window_closed(controller):
        if _model_window_closed(controller):
            controller._invocation.stop_reason = "window_budget_exhausted"
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
        + (
            "\n\nRecorded external user decisions:\n" + json.dumps(list(state.user_decisions), ensure_ascii=False)
            if getattr(controller._args, "external_live_workdir", None) is not None and state.user_decisions
            else ""
        )
        + "\n\n"
        + controller._gate.feedback(state.candidate)
        + worker_evidence_guidance()
    )
    max_attempts = int(getattr(controller._args, "transport_recovery_attempts", 3))
    minimum_seconds = _recovery_minimum_seconds(controller)
    recovery_attempts = 0
    attempt_records: list[dict[str, Any]] = []
    transport_seen = False
    recovery: dict[str, Any] | None = None
    result = None
    wrapped = BudgetedAdapter(controller._adapters[0], budget, purpose="implementation", deadline=deadline)
    while True:
        if _model_window_closed(controller):
            controller._invocation.stop_reason = "window_budget_exhausted"
            break
        attempt_prompt = prompt if recovery_attempts == 0 else prompt + _RECOVERY_GUIDANCE
        state = controller._advance(runtime, state, "p1_started")
        release_recovered_views((runtime.workspace,))
        handle = runtime.begin_execution(
            state.work_id, expected_revision=state.revision, context=attempt_prompt, draft=True
        )
        artifact_dir = handle.artifact_dir
        before = len(wrapped.results)
        try:
            result = wrapped.run_turn(
                TurnRequest(
                    role="worker",
                    prompt=attempt_prompt,
                    workdir=handle.workdir,
                    timeout_seconds=controller._args.turn_timeout_seconds,
                    artifact_dir=artifact_dir,
                )
            )
            if controller._adapters[0].budget_failure == "TurnBudgetExhausted":
                controller._raise_native_turn_refusal(window, budget)
            if len(wrapped.results) == before:
                if recovery_attempts == 0:
                    controller._invocation.stop_reason = wrapped.stopped or "repair_not_started"
                    return state, False
                if transport_seen:
                    recovery = _repair_recovery_record(
                        attempts=recovery_attempts,
                        max_attempts=max_attempts,
                        minimum_seconds=minimum_seconds,
                        outcome=wrapped.stopped or "recovery_not_admitted",
                        incomplete=True,
                        attempt_records=attempt_records,
                    )
                break
            graph_consumption = []
            consumption_file = turn_root / "p1-graph-consumption.json"
            write_json_atomic(
                consumption_file,
                {
                    "schema": "long-horizon/graph-consumption/v1",
                    "stage": "p1-worker-adapter",
                    "prompt_sha256": sha256_bytes(attempt_prompt.encode()),
                    "localization_text_sha256": sha256_bytes(
                        state.advisory["long_horizon_suite"]["localization"]["text"].encode()
                    ),
                    "graph_artifacts": state.advisory["long_horizon_suite"]["graph_artifacts"],
                    "prompt_includes_localization": state.advisory["long_horizon_suite"]["localization"]["text"]
                    in attempt_prompt,
                    "status": result.status,
                },
            )
            graph_consumption = [
                {"path": contained(output, consumption_file), "sha256": sha256_bytes(consumption_file.read_bytes())}
            ]
            # Retain a partial draft for inspection, but only public validation can
            # make it export-eligible. Never replay its calls or tool side effects.
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
        evidence = transport_failure_evidence(
            result,
            artifact_dirs=(artifact_dir,),
            native_dirs=_repair_evidence_dirs(controller),
        )
        if evidence is not None:
            transport_seen = True
        attempt_records.append(
            {
                "attempt": recovery_attempts + 1,
                "status": result.status,
                "candidate_tree": state.candidate.tree,
                "transport": evidence,
            }
        )
        if result.status == "completed":
            break
        if evidence is None:
            break
        if recovery_attempts >= max_attempts:
            recovery = _repair_recovery_record(
                attempts=recovery_attempts,
                max_attempts=max_attempts,
                minimum_seconds=minimum_seconds,
                outcome="recovery_exhausted",
                incomplete=True,
                attempt_records=attempt_records,
            )
            break
        remaining = _recovery_remaining_seconds(controller, budget, deadline)
        turn_limit = getattr(controller._args, "max_turns", None)
        used_turns = sum(call.get("purpose") == "implementation" for call in budget.snapshot()["calls"])
        if controller._abort.is_set():
            recovery = _repair_recovery_record(
                attempts=recovery_attempts,
                max_attempts=max_attempts,
                minimum_seconds=minimum_seconds,
                outcome="aborted",
                incomplete=True,
                attempt_records=attempt_records,
            )
            break
        if turn_limit is not None and used_turns >= turn_limit:
            recovery = _repair_recovery_record(
                attempts=recovery_attempts,
                max_attempts=max_attempts,
                minimum_seconds=minimum_seconds,
                outcome="turn_limit",
                incomplete=True,
                attempt_records=attempt_records,
            )
            break
        if remaining < minimum_seconds:
            recovery = _repair_recovery_record(
                attempts=recovery_attempts,
                max_attempts=max_attempts,
                minimum_seconds=minimum_seconds,
                outcome="budget_insufficient",
                incomplete=True,
                attempt_records=attempt_records,
            )
            break
        recovery_attempts += 1
    if transport_seen:
        if result is not None and result.status == "completed":
            recovery = _repair_recovery_record(
                attempts=recovery_attempts,
                max_attempts=max_attempts,
                minimum_seconds=minimum_seconds,
                outcome="recovered",
                incomplete=False,
                attempt_records=attempt_records,
            )
        elif recovery is None:
            recovery = _repair_recovery_record(
                attempts=recovery_attempts,
                max_attempts=max_attempts,
                minimum_seconds=minimum_seconds,
                outcome="non_transport_failure_after_recovery",
                incomplete=True,
                attempt_records=attempt_records,
            )
        path = turn_root / "p1-transport-recovery.json"
        recovery["path"] = contained(output, path)
        write_json_atomic(path, recovery)
    if result is not None and result.status != "completed":
        controller._invocation.stop_reason = f"repair_{result.status}_publicly_checked_fallback"
    # Seal the completed/partial Worker source before an infrastructure error
    # in public checking can interrupt the next stage. This grants no eligibility.
    seal_started = budget.clock()
    receipt = runtime.export_candidate(state.candidate, output / "stages" / f"p1-{uuid4().hex}")
    record, _ = controller._artifacts.stage_record(
        output_root=output,
        turn_root=turn_root,
        stage="p1",
        receipt=receipt,
        metadata={"work_id": state.work_id, "source": "separate P1 repair after S0 clarification"},
    )
    controller._pending_seal_seconds = max(0.0, budget.clock() - seal_started)
    state = register_suite_checks(runtime, state)
    # Registering methods invalidates Core's previous verification. Check the
    # current complete set once, retaining the supplied command unchanged.
    state = controller._check_candidate(runtime, state, "p1")
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
    state = controller._advance(
        runtime,
        state,
        "p1_saved",
        stages={"p1": record, "handoff": handoff},
        p1_worker_completed=bool(result is not None and result.status == "completed"),
        **({"p1_transport_recovery": recovery} if recovery is not None else {}),
    )
    if recovery is not None:
        state = runtime.update_advisory(
            state.work_id,
            expected_revision=state.revision,
            advisory={
                **state.advisory,
                "long_horizon_suite": {
                    **state.advisory["long_horizon_suite"],
                    "p1_transport_recovery": recovery,
                },
            },
        )
        controller._invocation.state = state
    return state, True


class GuidedAdapter:
    """Fresh read-only replanning guides later Workers within the original trial."""

    def __init__(self, native: Any, controller: Any, runtime: Any, resolved: Any, budget: Any) -> None:
        self.native, self.controller, self.runtime = native, controller, runtime
        self.resolved, self.budget = resolved, budget
        self.id = native.id

    def probe(self):
        return self.native.probe()

    def run_turn(self, request: Any):
        if _model_window_closed(self.controller):
            return _model_window_turn_result()
        state = self.runtime.read_work(self.controller._invocation.state.work_id)
        suite = state.advisory["long_horizon_suite"]
        context = investigation_context(
            self.controller,
            self.resolved,
            self.budget,
            request,
            "planner",
            checkpoint=_side_call_checkpoint(self.runtime, state),
        )
        plan, status = suite["initial_plan"], "initial_plan_retained"
        integrity_recovery: dict[str, Any] | None = None
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
        except SideCallIntegrityError as exc:
            integrity_recovery = _recover_side_call_integrity(self.controller, exc, stage="planner")
            if integrity_recovery is None:
                raise
            text = ""
            status = "planner_integrity_recovered"
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
                **({"integrity_recovery": integrity_recovery} if integrity_recovery is not None else {}),
            },
        )
        if _model_window_closed(self.controller):
            # The planner itself may have consumed the last admitted seconds.
            # Recheck before the Worker call instead of spending an expired turn.
            self.controller._invocation.stop_reason = "window_budget_exhausted"
            return _model_window_turn_result()
        # The planning side call is over; the view is now handed to the Worker.
        # Validate the recovered guard one final time, then let the Worker write.
        if request.workdir is not None:
            release_recovered_views((request.workdir,))
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


def investigation_context(
    controller: Any,
    resolved: Any,
    budget: Any,
    request: Any,
    stage: str,
    *,
    checkpoint: tuple[Path, str] | None = None,
):
    deadline = request.deadline if request.deadline is not None else budget.snapshot()["deadline"]
    if controller._phase_policy is not None:
        deadline = min(deadline, controller._phase_policy.deadline_for(stage))
    admission = controller._artifacts.make_admission(
        budget,
        deadline=deadline,
        workdir=request.workdir,
        adapter_id="suite-" + stage + "-" + resolved.fingerprint,
        stop_requested=controller._model_stop_requested,
    )
    return ClarificationContext(
        profile=resolved.model_context,
        snapshot=ReviewSnapshot((ReviewRoot(str(request.workdir)),)),
        session_id=controller._invocation.invocation_id + "-" + stage,
        session_dir=controller._args.output / stage,
        admission=admission,
        deadline=deadline,
        checkpoint=checkpoint,
    )


def _side_call_checkpoint(runtime: Any, state: Any) -> tuple[Path, str] | None:
    workspace = getattr(runtime, "workspace", None)
    candidate = getattr(state, "candidate", None)
    commit = getattr(candidate, "checkpoint", None)
    if workspace is None or not commit:
        return None
    return Path(workspace), str(commit)


def _recover_side_call_integrity(
    controller: Any, exc: SideCallIntegrityError, *, stage: str
) -> dict[str, Any] | None:
    """Suite callers share the bounded recovery decision with every other side call."""
    return recover_side_call_integrity(
        exc,
        stage=stage,
        controller=controller,
        plugin=getattr(controller, "_plugin", None),
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

    @property
    def candidate_untrusted(self):
        return getattr(self.native, "candidate_untrusted", False)

    @property
    def integrity_failures(self):
        return getattr(self.native, "integrity_failures", ())

    async def cancel_current_turn(self):
        await self.native.cancel_current_turn()

    def run_turn(self, request: Any):
        if _model_window_closed(self.controller):
            return _model_window_turn_result()
        state = self.runtime.read_work(self.controller._invocation.state.work_id)
        context = investigation_context(
            self.controller,
            self.resolved,
            self.budget,
            request,
            "manager",
            checkpoint=_side_call_checkpoint(self.runtime, state),
        )
        status = "available"
        integrity_recovery: dict[str, Any] | None = None
        try:
            advice = self.controller._run_async(
                self.controller._plugin.investigate(
                    context,
                    "[SUITE_MANAGER]\nRead only the current candidate. Assess the Initial Plan, requirement coverage, "
                    "verification gaps, regressions and unresolved questions. Give bounded concrete advice for the "
                    "Reviewer and next Worker. You cannot accept, deliver, waive checks or change user requirements.\n\n"
                    + state.advisory["long_horizon_suite"]["brief"]
                    + "\n\nCurrent review context:\n"
                    + request.prompt
                    + "\n\nScratch-only execution: build and write only in the assigned scratch candidate directory. "
                    "Before building, cd to that exact absolute path; never use find over /workspace, /tmp or other "
                    "global roots to locate the project. The side-call wrapper supplies the absolute candidate path.",
                    stage="manager",
                )
            )
        except SideCallIntegrityError as exc:
            integrity_recovery = _recover_side_call_integrity(self.controller, exc, stage="manager")
            if integrity_recovery is None:
                raise
            advice = (
                "Management advice discarded after a protected-view integrity check; independently assess "
                "the full task and Initial Plan."
            )
            status = "manager_integrity_recovered"
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
                "advice": advice[:model_content_limit(12_000)],
                **({"integrity_recovery": integrity_recovery} if integrity_recovery is not None else {}),
            },
        )
        if _model_window_closed(self.controller):
            # Management advice may cross the soft boundary; do not start the
            # Reviewer/diagnosis turn on an expired model admission.
            self.controller._invocation.stop_reason = "window_budget_exhausted"
            return _model_window_turn_result()
        confirmed_decisions = [
            decision for decision in getattr(state, "user_decisions", ()) if decision.get("authority") == "user"
        ]
        return self.native.run_turn(
            replace(
                request,
                prompt=request.prompt
                + "\n\nManagement advice (untrusted; independently verify):\n"
                + advice[:model_content_limit(12_000)]
                + "\nIndependently review pending supplemental checks against the original task. "
                "When concrete behavioral evidence is missing, propose executable checks in "
                "<pact-checks>JSON array</pact-checks>, each with name, command, requirement (an exact quote "
                "from the original task), and optionally files. Core stops before execution when a listed "
                "definition file changes. List only files that define the check itself, such as a test script "
                "it invokes. Read candidate inputs in the command; omit files for a self-contained inline check. "
                "Before proposing or admitting a method, exercise the command as written in a fresh subprocess "
                "with the check runner's inherited PATH. Use a non-login shell unless login behavior is required. "
                "Create any private temporary directory the command selects before launching tools that use it. "
                "Distinguish setup failure from a failed behavioral assertion. "
                "For a UI hit test, locate the intended element from the public interface and compare the hit "
                "element with that DOM object (or an intended interactive descendant). Use a specific ID as a "
                "requirement only if the contract specifies it. Check that a direct hit passes and a genuinely "
                "covering element fails even when its identifier resembles the target's. "
                "No unconditional-success commands. Historical process obligations cannot be inferred from "
                "the final source tree; record unavailable evidence explicitly. "
                "For sound checks include <pact-check-decisions> JSON array with name, version, "
                "action=admit and reason. Do not waive supplied checks or original requirements."
                + CONFIRMED_BEHAVIOR_CHECK
                + (
                    "\nRecorded user decisions for this Work:\n" + json.dumps(confirmed_decisions, ensure_ascii=False)
                    if confirmed_decisions else ""
                )
                + reviewer_ui_state_evidence_guidance()
                + reviewer_delivery_hygiene_guidance(),
            )
        )


class CandidateReviewDriver(ReviewLimitedDriver):
    """Require a recorded independent method admission before Terminal acceptance."""

    def _review(self, state, budget, window, *, purpose, full=False, force=False):
        if full and any(check["disposition"] == "pending" for check in state.checks):
            state, stopped = super()._review(state, budget, window, purpose="feedback", force=True)
            if stopped or any(check["disposition"] == "pending" for check in state.checks):
                return state, stopped
        return super()._review(state, budget, window, purpose=purpose, full=full, force=force)


async def _prepare_suite_legacy(
    plugin: Any,
    context: Any,
    original_task: str,
    *,
    cutoff_at_unix: float | None = None,
    clock: Callable[[], float] = time.time,
) -> tuple[Any, dict[str, Any]]:
    """Clarification, recall and localization overlap and observe S0 only."""
    clear_public_review()
    before = await asyncio.to_thread(view_identity, context.snapshot)

    async def recall() -> dict[str, str]:
        cutoff = cutoff_at_unix
        try:
            if cutoff is not None and cutoff <= clock():
                raise TimeoutError("suite recall cutoff elapsed")
            async with asyncio.timeout(30 if cutoff is None else max(0, cutoff - clock())):
                text = await plugin.recall(context, original_task)
            return {"status": "available" if text else "unavailable", "text": text[:model_content_limit(12_000)]}
        except asyncio.CancelledError:
            raise
        except NonDegradableIntegrityError:
            raise
        except Exception as exc:
            if cutoff is not None:
                raise
            return {"status": "degraded", "text": "", "reason": type(exc).__name__}

    # An explicit cutoff disables graph localization. Ordinary
    # suite callers retain main's graph localization and cancellation semantics.
    graph_enabled = cutoff_at_unix is None
    diagnostics: list[dict[str, str]] = []

    async def clarify() -> Any:
        try:
            return await plugin.clarify(context, original_task)
        except (PlanningCheckpointError, SideCallIntegrityError, NonDegradableIntegrityError):
            raise
        except (RuntimeContractError, ValueError) as exc:
            # The fallback adds no acceptance authority or benchmark-specific solution.
            diagnostics.append({"stage": "clarification", "reason": type(exc).__name__})
            return plugin.degraded_clarification(type(exc).__name__)

    tasks = [asyncio.create_task(clarify()), asyncio.create_task(recall())]
    if graph_enabled:
        tasks.append(
            asyncio.create_task(
                localize_suite(plugin, context, original_task, before, cutoff_at_unix=cutoff_at_unix, clock=clock)
            )
        )
    locations = {"status": "disabled_by_human", "text": "", "configured_enabled": False}
    try:
        completed = await asyncio.gather(*tasks)
        result, memory = completed[:2]
        if graph_enabled:
            locations = completed[2]
    except BaseException as exc:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        owner = asyncio.current_task()
        if owner is not None and owner.cancelling() > 0:
            raise asyncio.CancelledError() from exc
        progress = _capacity_public_review(plugin, exc, deadline_unix=getattr(context, "deadline", None))
        if progress is not None:
            raise recoverable_public_review_error(plugin.artifacts.exhausted, progress) from exc
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
        pair = validate_pair(await plugin.generate_pact_input(context, result, original_task, hints=hints))
    except (PlanningCheckpointError, SideCallIntegrityError, NonDegradableIntegrityError):
        raise
    except (RuntimeContractError, ValueError) as exc:
        if public_review_failure(exc) is not None:
            raise
        review_failure = _public_review_failure(exc)
        if review_failure is not None:
            raise recoverable_public_review_error(plugin.artifacts.exhausted, review_failure) from exc
        # The fallback adds no acceptance authority or benchmark-specific solution.
        pair = original_task_pair(original_task)
        planning_status = "degraded_original_task"
        diagnostics.append({"stage": "contract-plan", "reason": type(exc).__name__})
    except OSError as exc:
        progress = _capacity_public_review(plugin, exc, deadline_unix=getattr(context, "deadline", None))
        if progress is None:
            raise
        raise recoverable_public_review_error(plugin.artifacts.exhausted, progress) from exc
    _raise_incomplete_public_review(plugin, pair)
    _note_public_degradation(pair, diagnostics)
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
            "initial_plan_hints_include_localization": bool(locations["text"]) and locations["text"] in hints,
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
    if controller._abort.is_set() or _model_window_closed(controller):
        if _model_window_closed(controller):
            controller._invocation.stop_reason = "window_budget_exhausted"
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
        + (
            "\n\nRecorded external user decisions:\n" + json.dumps(list(state.user_decisions), ensure_ascii=False)
            if getattr(controller._args, "external_live_workdir", None) is not None and state.user_decisions
            else ""
        )
        + worker_evidence_guidance()
    )
    state = controller._advance(runtime, state, "p1_started")
    release_recovered_views((runtime.workspace,))
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
    seal_started = budget.clock()
    receipt = runtime.export_candidate(state.candidate, output / "stages" / f"p1-{uuid4().hex}")
    record, _ = controller._artifacts.stage_record(
        output_root=output,
        turn_root=turn_root,
        stage="p1",
        receipt=receipt,
        metadata={"work_id": state.work_id, "source": "separate P1 repair after S0 clarification"},
    )
    controller._pending_seal_seconds = max(0.0, budget.clock() - seal_started)
    state = register_suite_checks(runtime, state)
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


async def prepare_suite(
    plugin: Any,
    context: Any,
    original_task: str,
    *,
    cutoff_at_unix: float | None = None,
    clock: Callable[[], float] = time.time,
    **bounded: Any,
):
    validate_cutoff(cutoff_at_unix)
    if cutoff_at_unix is not None:
        # Legacy means no reserved phase fractions; every admission still uses
        # the one global deadline. Do not select bounded fallback preparation.
        return await _prepare_suite_legacy(plugin, context, original_task, cutoff_at_unix=cutoff_at_unix, clock=clock)
    if not bounded:
        return await _prepare_suite_legacy(plugin, context, original_task, cutoff_at_unix=cutoff_at_unix, clock=clock)
    return await _prepare_suite_bounded(
        plugin, context, original_task, cutoff_at_unix=cutoff_at_unix, clock=clock, **bounded
    )


def run_repair(controller: Any, runtime: Any, output: Path, turn_root: Path, budget: Any, window: Any, state: Any):
    implementation = _run_repair_bounded if controller._phase_policy is not None else _run_repair_legacy
    return implementation(controller, runtime, output, turn_root, budget, window, state)
