"""One bounded worker/checkpoint/verify/review/gate Execution round."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
import math
from pathlib import Path
from time import time

from ._execution_artifacts import (
    feedback_text,
    role_result_payload,
    round_key,
    skipped_reviewer_review,
    synthesized_review,
    synthesized_worker_summary,
)
from ._execution_operations import ExecutionIO, operation
from .adapters.base import AgentAdapter
from .artifacts import LoopPaths, round_paths
from .checkpoint import CheckpointStore
from .config import LoopConfig
from .gate import GateInputs, evaluate_gate
from .goal_tracker import (
    immutable_goal_sha256,
    record_round_progress,
    sha256_text,
)
from .prompts import (
    build_review_decision_repair_prompt,
    build_reviewer_prompt,
    build_worker_prompt,
)
from .review_decision import (
    needs_decision_repair,
    resolve_review_decision,
)
from .schemas import (
    CHECKPOINT_SCHEMA,
    GATE_DECISION_SCHEMA,
    REVIEW_DECISION_SCHEMA,
    VERIFICATION_SCHEMA,
    AdapterProbe,
    InitializationCheckpoint,
    LoopStatus,
    RoundCheckpoint,
    TurnRequest,
    TurnResult,
    VerificationResult,
)
from .verification import run_verification


def remaining_timeout(timeout_seconds: float, deadline: float | None) -> float:
    """Bound a stage by one absolute UTC deadline; zero never starts a process."""
    if deadline is None:
        return timeout_seconds
    if not math.isfinite(deadline):
        raise ValueError("deadline must be finite UTC epoch seconds")
    return max(0.0, min(timeout_seconds, deadline - time()))


def _bounded_review(context: RoundContext, request: TurnRequest, deadline: float | None) -> TurnResult:
    timeout = remaining_timeout(request.timeout_seconds, deadline)
    if timeout == 0:
        return TurnResult(status="timeout", final_text="", output_source="controller", exit_code=None,
                          stderr_tail="budget_exhausted: reviewer deadline reached")
    return context.reviewer.run_turn(replace(request, timeout_seconds=timeout, deadline=deadline))


@dataclass(frozen=True, slots=True)
class RoundContext:
    loop_id: str
    config: LoopConfig
    workspace: Path
    paths: LoopPaths
    store: CheckpointStore
    initialization: InitializationCheckpoint
    state: dict[str, object]
    plan: str
    repo_instructions: str
    expected_goal_hash: str
    verify_command: str | None
    worker: AgentAdapter
    reviewer: AgentAdapter
    worker_probe: AdapterProbe
    reviewer_probe: AdapterProbe
    reprobe: Callable[[AgentAdapter, str], None]
    io: ExecutionIO
    yield_on_plan_challenge: bool = False
    review_context: str = ""


@dataclass(slots=True)
class ExecutionProgress:
    current_checkpoint: str
    status: LoopStatus = "running"
    stop_reason: str = ""
    rounds_completed: int = 0
    previous_feedback: str = ""


@dataclass(frozen=True, slots=True)
class PreparedRound:
    """Worker evidence, or its exact retained equivalent, before acceptance."""

    checkpoint: RoundCheckpoint
    worker_turn: TurnResult | None
    worker_summary: str
    worker_output_missing: bool
    input_checkpoint: str
    round_key_value: str
    goal_tracker: str


def execute_round(
    context: RoundContext,
    progress: ExecutionProgress,
    *,
    round_number: int,
) -> None:
    """Keep the standalone Worker → verification → reviewer → gate ordering."""
    prepared = execute_worker_checkpoint(context, progress, round_number=round_number)
    validate_prepared_round(context, progress, round_number=round_number, prepared=prepared)


def execute_worker_checkpoint(
    context: RoundContext,
    progress: ExecutionProgress,
    *,
    round_number: int,
) -> PreparedRound:
    """Execute one Worker and durably capture its final filesystem."""
    config = context.config
    paths = context.paths
    state = context.state
    store = context.store
    io = context.io

    progress.rounds_completed = round_number
    state["current_round"] = round_number
    store.assert_round_start()
    rpaths = round_paths(paths.loop_dir, round_number)
    input_checkpoint = progress.current_checkpoint
    io.append_event(
        paths.events, "round_start", {"round": round_number, "input": input_checkpoint}
    )

    goal_tracker = paths.goal_tracker.read_text(encoding="utf-8")
    worker_prompt = build_worker_prompt(
        round_number=round_number,
        input_checkpoint=input_checkpoint,
        plan=context.plan,
        goal_tracker=goal_tracker,
        repo_instructions=context.repo_instructions,
        previous_feedback=progress.previous_feedback,
    )
    io.write_text(rpaths.worker_prompt, worker_prompt)
    round_key_value = round_key(
        context.loop_id, round_number, input_checkpoint, worker_prompt
    )

    with operation(io, paths, state, operation="worker_turn", role="worker"):
        context.reprobe(context.worker, "worker")
        worker_turn = context.worker.run_turn(
            TurnRequest(
                role="worker",
                prompt=worker_prompt,
                workdir=context.initialization.worker_worktree,
                timeout_seconds=config.turn_timeout_seconds,
                artifact_dir=rpaths.worker_adapter_artifacts,
            )
        )
    with operation(io, paths, state, operation="checkpoint"):
        checkpoint = store.checkpoint_worker(round_number=round_number)
    progress.current_checkpoint = checkpoint.commit
    worker_output_missing = (
        worker_turn.status != "completed" or not worker_turn.final_text.strip()
    )
    worker_summary = (
        worker_turn.final_text
        if not worker_output_missing
        else synthesized_worker_summary(
            turn=worker_turn,
            checkpoint_commit=checkpoint.commit,
            changed_paths=checkpoint.changed_paths,
        )
    )
    io.write_text(rpaths.summary, worker_summary)
    io.write_text(rpaths.delta_patch, checkpoint.round_diff)
    io.write_text(rpaths.patch, checkpoint.cumulative_diff)
    io.write_json(
        rpaths.checkpoint,
        {
            "schema": CHECKPOINT_SCHEMA,
            "round": round_number,
            "parent_commit": checkpoint.parent_commit,
            "commit": checkpoint.commit,
            "tree": checkpoint.tree,
            "status": checkpoint.status,
            "changed_paths": checkpoint.changed_paths,
            "namespace_violations": checkpoint.namespace_violations,
            "round_patch_sha256": sha256_text(checkpoint.round_diff),
            "cumulative_patch_sha256": sha256_text(checkpoint.cumulative_diff),
        },
    )
    io.write_json(
        rpaths.worker_result,
        role_result_payload(
            role="worker",
            round_number=round_number,
            probe=context.worker_probe,
            turn=worker_turn,
            prompt=worker_prompt,
            round_key_value=round_key_value,
            checkpoint_commit=checkpoint.commit,
            artifact_root=paths.loop_dir,
        ),
    )
    io.append_event(
        paths.events,
        "worker_done",
        {
            "round": round_number,
            "status": worker_turn.status,
            "checkpoint": checkpoint.commit,
        },
    )

    return PreparedRound(
        checkpoint=checkpoint, worker_turn=worker_turn, worker_summary=worker_summary,
        worker_output_missing=worker_output_missing, input_checkpoint=input_checkpoint,
        round_key_value=round_key_value, goal_tracker=goal_tracker,
    )


def verify_prepared_round(
    context: RoundContext, prepared: PreparedRound, *, round_number: int,
    deadline: float | None = None,
) -> VerificationResult:
    """Verify one retained filesystem checkpoint without invoking either role."""
    config, paths, state, store, io = (
        context.config, context.paths, context.state, context.store, context.io,
    )
    checkpoint = prepared.checkpoint
    rpaths = round_paths(paths.loop_dir, round_number)
    with (
        operation(io, paths, state, operation="public_verification"),
        store.verification_worktree(
            round_number=round_number, checkpoint=checkpoint.commit
        ) as verification_dir,
    ):
        timeout = remaining_timeout(config.verify_timeout_seconds, deadline)
        verification: VerificationResult = VerificationResult(
            status="timeout", command=context.verify_command or "",
            stderr_tail="budget_exhausted: deadline reached before public verification",
        ) if timeout == 0 else run_verification(
            workspace=context.workspace,
            workdir=verification_dir,
            checkpoint_commit=checkpoint.commit,
            baseline_commit=context.initialization.baseline_commit,
            namespace_violations=checkpoint.namespace_violations,
            required_artifacts=[rpaths.summary, rpaths.patch],
            command=context.verify_command,
            timeout_seconds=timeout,
            deadline=deadline,
            loop_dir=paths.loop_dir,
            round_number=round_number,
            patch_path=rpaths.patch,
        )
    io.write_json(
        rpaths.verification,
        {"schema": VERIFICATION_SCHEMA, "round": round_number, **asdict(verification)},
    )
    io.write_text(
        rpaths.verification_log,
        f"--- stdout (tail) ---\n{verification.stdout_tail}\n"
        f"--- stderr (tail) ---\n{verification.stderr_tail}\n",
    )
    io.append_event(
        paths.events,
        "verification",
        {"round": round_number, "status": verification.status},
    )

    return verification


def validate_prepared_round(
    context: RoundContext,
    progress: ExecutionProgress,
    *,
    round_number: int,
    prepared: PreparedRound,
    verification: VerificationResult | None = None,
    reviewer_prompt_builder: Callable[[str], str] | None = None,
    feedback_only: bool = False,
    deadline: float | None = None,
) -> None:
    """Apply the shared verification/reviewer/gate path to an exact checkpoint."""
    config, paths, state, store, io = (
        context.config, context.paths, context.state, context.store, context.io,
    )
    progress.rounds_completed = round_number
    checkpoint = prepared.checkpoint
    worker_turn = prepared.worker_turn
    worker_summary = prepared.worker_summary
    worker_output_missing = prepared.worker_output_missing
    input_checkpoint = prepared.input_checkpoint
    round_key_value = prepared.round_key_value
    goal_tracker = prepared.goal_tracker
    rpaths = round_paths(paths.loop_dir, round_number)
    if verification is None:
        verification = verify_prepared_round(context, prepared, round_number=round_number, deadline=deadline)

    if remaining_timeout(config.turn_timeout_seconds, deadline) == 0:
        io.write_text(rpaths.review, "budget_exhausted: deadline reached before reviewer execution.\n")
        progress.status, progress.stop_reason = "failed", "budget_exhausted"
        return

    reviewer_turn = None
    repair_turn = None
    skip_reviewer = worker_output_missing and not checkpoint.changed_paths and worker_turn is not None
    if skip_reviewer:
        assert worker_turn is not None
        skip_reason = "worker_no_usable_output_and_no_round_delta"
        review_text = skipped_reviewer_review(
            turn=worker_turn, checkpoint_commit=checkpoint.commit
        )
        io.write_text(rpaths.review, review_text)
        io.write_json(
            rpaths.reviewer_result,
            role_result_payload(
                role="reviewer",
                round_number=round_number,
                probe=context.reviewer_probe,
                turn=None,
                prompt=None,
                round_key_value=round_key_value,
                checkpoint_commit=checkpoint.commit,
                artifact_root=paths.loop_dir,
                skip_reason=skip_reason,
            ),
        )
        reviewer_mutated = False
        io.append_event(
            paths.events,
            "reviewer_skipped",
            {"round": round_number, "reason": skip_reason},
        )
    else:
        with store.reviewer_scratch(round_number=round_number) as scratch_dir:
            reviewer_prompt = reviewer_prompt_builder(str(scratch_dir)) if reviewer_prompt_builder else build_reviewer_prompt(
                round_number=round_number,
                baseline_checkpoint=context.initialization.baseline_commit,
                input_checkpoint=input_checkpoint,
                output_checkpoint=checkpoint.commit,
                plan=context.plan,
                goal_tracker=goal_tracker,
                repo_instructions=context.repo_instructions,
                worker_summary=worker_summary,
                verification=verification,
                reviewer_scratch=str(scratch_dir),
                runtime_evidence=context.review_context,
            )
            io.write_text(rpaths.reviewer_prompt, reviewer_prompt)
            with store.reviewer_worktree(
                round_number=round_number, checkpoint=checkpoint.commit
            ) as reviewer_dir:
                try:
                    with operation(
                        io, paths, state, operation="reviewer_turn", role="reviewer"
                    ):
                        context.reprobe(context.reviewer, "reviewer")
                        reviewer_turn = _bounded_review(context,
                            TurnRequest(
                                role="reviewer",
                                prompt=reviewer_prompt,
                                workdir=reviewer_dir,
                                timeout_seconds=remaining_timeout(config.turn_timeout_seconds, deadline),
                                artifact_dir=rpaths.reviewer_adapter_artifacts,
                                review_mode="feedback" if feedback_only else "acceptance",
                            ), deadline,
                        )
                    if (not feedback_only and needs_decision_repair(reviewer_turn)
                            and remaining_timeout(config.turn_timeout_seconds, deadline) > 0):
                        repair_prompt = build_review_decision_repair_prompt(
                            review_text=reviewer_turn.final_text,
                            raw_decision=reviewer_turn.review_decision.raw_text,
                        )
                        io.write_text(rpaths.reviewer_repair_prompt, repair_prompt)
                        io.append_event(
                            paths.events,
                            "review_decision_repair_started",
                            {
                                "round": round_number,
                                "initial_capture_status": (
                                    reviewer_turn.review_decision.verdict_status
                                ),
                            },
                        )
                        with operation(
                            io, paths, state, operation="reviewer_turn", role="reviewer"
                        ):
                            context.reprobe(context.reviewer, "reviewer")
                            repair_turn = _bounded_review(context,
                                TurnRequest(
                                    role="reviewer",
                                    prompt=repair_prompt,
                                    workdir=reviewer_dir,
                                    timeout_seconds=remaining_timeout(config.turn_timeout_seconds, deadline),
                                    artifact_dir=rpaths.reviewer_repair_adapter_artifacts,
                                ), deadline,
                            )
                        io.write_json(
                            rpaths.reviewer_repair_result,
                            role_result_payload(
                                role="reviewer",
                                round_number=round_number,
                                probe=context.reviewer_probe,
                                turn=repair_turn,
                                prompt=repair_prompt,
                                round_key_value=round_key(
                                    context.loop_id,
                                    round_number,
                                    checkpoint.commit,
                                    repair_prompt,
                                ),
                                checkpoint_commit=checkpoint.commit,
                                artifact_root=paths.loop_dir,
                            ),
                        )
                        io.append_event(
                            paths.events,
                            "review_decision_repair_done",
                            {
                                "round": round_number,
                                "turn_status": repair_turn.status,
                                "capture_status": (
                                    repair_turn.review_decision.verdict_status
                                ),
                            },
                        )
                finally:
                    mutation = store.capture_reviewer_mutation(
                        reviewer_dir, checkpoint.commit
                    )
                    io.write_text(rpaths.reviewer_mutation, mutation.diff)

        reviewer_output_missing = (
            reviewer_turn.status != "completed" or not reviewer_turn.final_text.strip()
        )
        review_text = (
            reviewer_turn.final_text
            if not reviewer_output_missing
            else synthesized_review(turn=reviewer_turn)
        )
        io.write_text(rpaths.review, review_text)
        io.write_json(
            rpaths.reviewer_result,
            role_result_payload(
                role="reviewer",
                round_number=round_number,
                probe=context.reviewer_probe,
                turn=reviewer_turn,
                prompt=reviewer_prompt,
                round_key_value=round_key_value,
                checkpoint_commit=checkpoint.commit,
                artifact_root=paths.loop_dir,
            ),
        )
        reviewer_mutated = mutation.changed
        io.append_event(
            paths.events,
            "reviewer_done",
            {
                "round": round_number,
                "status": reviewer_turn.status,
                "mutated": reviewer_mutated,
            },
        )

    budget_exhausted = remaining_timeout(config.turn_timeout_seconds, deadline) == 0 or (deadline is not None and any(
        turn is not None and turn.status == "timeout" and turn.stderr_tail.startswith("budget_exhausted")
        for turn in (reviewer_turn, repair_turn)
    ))
    if feedback_only:
        store.assert_control_ownership(checkpoint.commit)
        if immutable_goal_sha256(paths.goal_tracker.read_text(encoding="utf-8")) != context.expected_goal_hash:
            raise RuntimeError("immutable Goal changed during feedback review")
        progress.status = "failed"
        progress.stop_reason = (
            "reviewer_mutation" if reviewer_mutated else
            "budget_exhausted" if budget_exhausted else
            "reviewer_output_missing" if reviewer_turn is None or reviewer_turn.status != "completed" or not reviewer_turn.final_text.strip() else
            "feedback_recorded"
        )
        state.update(current_round=round_number, current_checkpoint=checkpoint.commit,
                     current_tree=checkpoint.tree, round_gate_status=None)
        io.write_json(paths.state, state)
        return

    if budget_exhausted:
        store.assert_control_ownership(checkpoint.commit)
        progress.status, progress.stop_reason = "failed", "budget_exhausted"
        return

    review_resolution = resolve_review_decision(
        turn=reviewer_turn,
        review_text=review_text,
        repair_turn=repair_turn,
    )
    io.write_json(
        rpaths.review_decision,
        {
            "schema": REVIEW_DECISION_SCHEMA,
            "round": round_number,
            "checkpoint": checkpoint.commit,
            "review_sha256": sha256_text(review_text),
            **asdict(review_resolution),
        },
    )
    io.append_event(
        paths.events,
        "review_decision",
        {
            "round": round_number,
            "decision_status": review_resolution.verdict_status,
            "verdict": review_resolution.verdict,
            "source": review_resolution.verdict_source,
            "repair_attempted": review_resolution.repair_attempted,
        },
    )

    store.assert_control_ownership(checkpoint.commit)

    tracker_text = paths.goal_tracker.read_text(encoding="utf-8")
    try:
        current_goal_hash = immutable_goal_sha256(tracker_text)
    except ValueError:
        current_goal_hash = "immutable-markers-missing"
    with operation(io, paths, state, operation="gate"):
        decision = evaluate_gate(
            GateInputs(
                review_text=review_text,
                worker_summary=worker_summary,
                worker_output_missing=worker_output_missing,
                verification_status=verification.status,
                allow_unverified=config.allow_unverified,
                reviewer_mutated=reviewer_mutated,
                namespace_violations=tuple(checkpoint.namespace_violations),
                immutable_goal_sha256_expected=context.expected_goal_hash,
                immutable_goal_sha256_current=current_goal_hash,
                normalized_review_marker=review_resolution.verdict or "continue",
                normalized_terminal_line=review_resolution.terminal_line,
                review_protocol_error=(
                    review_resolution.verdict_status == "protocol_error"
                ),
            )
        )
    io.write_json(
        rpaths.gate,
        {
            "schema": GATE_DECISION_SCHEMA,
            "round": round_number,
            "verification_status": verification.status,
            **asdict(decision),
        },
    )
    io.append_event(
        paths.events,
        "gate",
        {
            "round": round_number,
            "status": decision.status,
            "blocked_by": decision.blocked_by,
        },
    )

    tracker_text = record_round_progress(
        tracker_text,
        round_number=round_number,
        checkpoint=checkpoint.commit,
        gate_status=decision.status,
        summary_ref=rpaths.summary.name,
        review_ref=rpaths.review.name,
    )
    io.write_text(paths.goal_tracker, tracker_text)
    state.update(
        {
            "current_round": round_number,
            "current_checkpoint": checkpoint.commit,
            "current_tree": checkpoint.tree,
            "round_gate_status": decision.status,
        }
    )
    io.write_json(paths.state, state)

    compatible_handoff_blockers = all(
        blocker == "reviewer_marker_not_complete"
        or blocker.startswith("verification_")
        for blocker in decision.blocked_by
    )
    runtime_handoff = (
        context.yield_on_plan_challenge
        and review_resolution.verdict_status == "valid"
        and review_resolution.verdict == "continue"
        and review_resolution.plan_challenge_status == "valid"
        and review_resolution.plan_challenge is not None
        and compatible_handoff_blockers
    )
    if runtime_handoff:
        progress.status = "failed"
        progress.stop_reason = "runtime_plan_challenge"
    elif decision.status == "complete":
        progress.status = "complete"
        progress.stop_reason = "gate_complete"
    elif decision.status == "failed":
        progress.status = "failed"
        progress.stop_reason = (
            "reviewer_protocol_error"
            if "reviewer_protocol_error" in decision.blocked_by
            else "reviewer_failed"
        )
    else:
        progress.previous_feedback = feedback_text(review_text, decision.reasons)
        if round_number == config.max_rounds:
            progress.status = "failed"
            progress.stop_reason = "max_rounds"
