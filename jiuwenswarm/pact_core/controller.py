"""Round loop orchestration: worker → checkpoint → public verification →
reviewer → gate → opaque final validation → promotion.

The controller owns every side effect around the pure gate: prompts, turns,
checkpoints, verification runs, artifacts, and termination. Only a
gate-complete outcome with satisfied final validation promotes; every other
path leaves the source workspace unmodified and keeps ``final.patch`` for humans (AGENTS.md
invariant 4).
"""

from __future__ import annotations

import math
import os
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from jiuwenswarm import pact_core
from ._execution_artifacts import (
    adapter_manifest_entry as _adapter_manifest_entry,
)
from ._execution_finalization import (
    finalize as finalize_execution,
    finalize_infrastructure_failure,
    run_final_stage as run_execution_final_stage,
)
from ._execution_operations import (
    ExecutionIO,
    operation as execution_operation,
)
from ._execution_round import ExecutionProgress, RoundContext, execute_round
from .adapters.base import AgentAdapter
from .artifacts import (
    LoopPaths,
    append_event,
    atomic_write_text,
    build_loop_id,
    build_loop_paths,
    read_json,
    write_json_atomic,
)
from .checkpoint import CheckpointStore
from .config import LoopConfig
from .goal_tracker import (
    build_initial_goal_tracker,
    immutable_goal_sha256,
    sha256_text,
)
from .schemas import (
    INITIALIZATION_SCHEMA,
    MANIFEST_SCHEMA,
    STATE_SCHEMA,
    AdapterProbe,
    FinalValidationStatus,
    InitializationCheckpoint,
    LoopStatus,
    OperationName,
    PromotionMode,
    PromotionStatus,
)

_TERMINAL_STATUSES: frozenset[str] = frozenset({"complete", "failed", "cancelled"})


class PactLoopError(RuntimeError):
    """Raised when a loop cannot start or a fail-closed invariant breaks."""


@dataclass(frozen=True, slots=True)
class LoopSummary:
    """Terminal outcome of one PACT loop."""

    loop_id: str
    loop_dir: Path
    status: LoopStatus
    stop_reason: str
    rounds: int
    final_checkpoint: str
    final_patch: Path
    round_gate_status: str | None
    final_validation_status: FinalValidationStatus | None
    promotion_status: PromotionStatus
    promotion_reason: str = ""


def _reject_unterminated_loop(paths: LoopPaths) -> None:
    """FR-10: an existing non-terminal loop is a hard error, never auto-resume."""
    if not paths.loop_dir.exists():
        return
    if paths.state.is_file():
        status = read_json(paths.state).get("status")
        if status not in _TERMINAL_STATUSES:
            raise PactLoopError(
                f"loop {paths.loop_dir.name} has unterminated status {status!r}; "
                "start a new loop or clean up manually"
            )
    raise PactLoopError(f"loop directory already exists: {paths.loop_dir}")


def _execution_io() -> ExecutionIO:
    """Bind current module write functions so fault-injection tests remain valid."""
    return ExecutionIO(
        write_json=write_json_atomic,
        write_text=atomic_write_text,
        append_event=append_event,
    )


@contextmanager
def _operation(
    paths: LoopPaths,
    state: dict[str, object],
    *,
    operation: OperationName,
    role: str | None = None,
) -> Iterator[None]:
    """Compatibility wrapper for the shared operation lifecycle boundary."""
    with execution_operation(
        _execution_io(), paths, state, operation=operation, role=role
    ):
        yield


def _probe_or_fail(adapter: AgentAdapter, role: str) -> AdapterProbe:
    probe = adapter.probe()
    if not probe.available:
        raise PactLoopError(
            f"{role} adapter {adapter.id!r} is not available: {probe.detail or 'probe failed'}"
        )
    return probe


def _reprobe_turn_if_required(adapter: AgentAdapter, role: str) -> None:
    """Close OpenCode's mutable-profile gap immediately before each model turn."""
    if adapter.id == "opencode":
        _probe_or_fail(adapter, role)


def _pact_core_revision() -> str:
    """Best-effort full git revision of the running pact-core checkout.

    A wheel installed beneath some unrelated repository must not inherit that
    repository's HEAD. Git repository-redirecting environment variables are
    ignored, and the discovered top-level must contain this exact package at
    ``pact_core/``. Any ambiguity or failure yields ``"unavailable"``.
    """
    if pact_core.__file__ is None:
        return "unavailable"
    package_dir = Path(pact_core.__file__).parent.resolve()
    git_env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    try:
        top_level = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=package_dir,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
            env=git_env,
        )
    except Exception:
        return "unavailable"
    if top_level.returncode != 0:
        return "unavailable"
    repo_root_text = top_level.stdout.strip()
    if not repo_root_text:
        return "unavailable"
    repo_root = Path(repo_root_text).resolve()
    if (repo_root / "pact_core").resolve() != package_dir:
        return "unavailable"
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD^{commit}"],
            cwd=repo_root,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
            env=git_env,
        )
    except Exception:
        return "unavailable"
    if result.returncode != 0:
        return "unavailable"
    revision = result.stdout.strip().lower()
    if len(revision) not in {40, 64} or any(char not in "0123456789abcdef" for char in revision):
        return "unavailable"
    return revision


def _run_final_stage(
    *,
    store: CheckpointStore,
    paths: LoopPaths,
    initialization: InitializationCheckpoint,
    checkpoint: str,
    command: str | None,
    timeout_seconds: float,
) -> FinalValidationStatus:
    return run_execution_final_stage(
        _execution_io(),
        store=store,
        paths=paths,
        initialization=initialization,
        checkpoint=checkpoint,
        command=command,
        timeout_seconds=timeout_seconds,
    )


def run_loop(
    config: LoopConfig,
    *,
    worker: AgentAdapter,
    reviewer: AgentAdapter,
) -> LoopSummary:
    """Run one standalone PACT loop with the shipped automatic-promotion behavior."""
    return _run_execution(
        config,
        worker=worker,
        reviewer=reviewer,
        promotion_mode="automatic",
    )


def _run_execution(
    config: LoopConfig,
    *,
    worker: AgentAdapter,
    reviewer: AgentAdapter,
    promotion_mode: PromotionMode,
    yield_on_plan_challenge: bool = False,
    initial_feedback: str = "",
) -> LoopSummary:
    """Run one bounded execution under the facade-selected promotion policy."""
    workspace = config.workspace.expanduser().resolve()
    plan_file = config.plan_file.expanduser()
    if not plan_file.is_absolute():
        plan_file = workspace / plan_file
    plan_file = plan_file.resolve()
    if not workspace.is_dir():
        raise PactLoopError(f"workspace does not exist: {workspace}")
    if not plan_file.is_file():
        raise PactLoopError(f"plan does not exist: {plan_file}")
    if config.max_rounds < 1:
        raise PactLoopError("max_rounds must be positive")
    timeout_fields = {
        "turn_timeout_seconds": config.turn_timeout_seconds,
        "verify_timeout_seconds": config.verify_timeout_seconds,
        "final_verify_timeout_seconds": config.final_verify_timeout_seconds,
    }
    for field, value in timeout_fields.items():
        if not math.isfinite(value) or value <= 0:
            raise PactLoopError(f"{field} must be a positive finite number")
    verify_command = (
        config.verify_command
        if config.verify_command is not None and config.verify_command.strip()
        else None
    )
    final_verify_command = (
        config.final_verify_command
        if config.final_verify_command is not None and config.final_verify_command.strip()
        else None
    )
    if verify_command is None and not config.allow_unverified:
        raise PactLoopError(
            "no verification command configured; pass --verify <cmd> or "
            "record the exemption explicitly with --allow-unverified"
        )

    worker_probe = _probe_or_fail(worker, "worker")
    reviewer_probe = _probe_or_fail(reviewer, "reviewer")

    loop_id = config.loop_id or build_loop_id()
    paths = build_loop_paths(workspace, loop_id)
    _reject_unterminated_loop(paths)

    plan = plan_file.read_text(encoding="utf-8")
    repo_instructions_file = workspace / "AGENTS.md"
    repo_instructions = (
        repo_instructions_file.read_text(encoding="utf-8")
        if repo_instructions_file.is_file()
        else ""
    )

    store = CheckpointStore(workspace, loop_id, worktree_root=config.worktree_root)
    initialization = store.initialize()

    goal_tracker = build_initial_goal_tracker(plan)
    expected_goal_hash = immutable_goal_sha256(goal_tracker)
    state_created_at = datetime.now(UTC).isoformat()
    state: dict[str, object] = {
        "schema": STATE_SCHEMA,
        "loop_id": loop_id,
        "status": "running",
        "stop_reason": "",
        "current_round": 0,
        "max_rounds": config.max_rounds,
        "immutable_goal_sha256": expected_goal_hash,
        "source_workspace": str(workspace),
        "baseline_commit": initialization.baseline_commit,
        "baseline_tree": initialization.baseline_tree,
        "current_checkpoint": initialization.baseline_commit,
        "current_tree": initialization.baseline_tree,
        "checkpoint_ref": initialization.checkpoint_ref,
        "worker_worktree": str(initialization.worker_worktree),
        "allow_unverified": config.allow_unverified,
        "round_gate_status": None,
        "final_validation_status": None,
        "promotion_status": "not_attempted",
        "promotion_reason": "",
        "operation": None,
        "operation_role": None,
        "operation_status": "idle",
        "operation_started_at": None,
        "operation_finished_at": None,
        "updated_at": state_created_at,
    }

    try:
        atomic_write_text(paths.plan, plan)
        atomic_write_text(paths.goal_tracker, goal_tracker)
        write_json_atomic(
            paths.initialization,
            {"schema": INITIALIZATION_SCHEMA, **asdict(initialization)},
        )
        write_json_atomic(
            paths.manifest,
            {
                "schema": MANIFEST_SCHEMA,
                "loop_id": loop_id,
                "created_at": datetime.now(UTC).isoformat(),
                "workspace": str(workspace),
                "plan_source": str(plan_file),
                "plan_sha256": sha256_text(plan),
                "worker_adapter": _adapter_manifest_entry(worker_probe),
                "reviewer_adapter": _adapter_manifest_entry(reviewer_probe),
                "max_rounds": config.max_rounds,
                "verify_command": verify_command,
                "turn_timeout_seconds": config.turn_timeout_seconds,
                "verify_timeout_seconds": config.verify_timeout_seconds,
                "final_validation": {
                    "configured": final_verify_command is not None,
                    "timeout_seconds": config.final_verify_timeout_seconds,
                    "command_sha256": (
                        sha256_text(final_verify_command) if final_verify_command else ""
                    ),
                },
                "allow_unverified": config.allow_unverified,
                "checkpoint_ref": initialization.checkpoint_ref,
                "worker_worktree": str(initialization.worker_worktree),
                "pact_core_version": pact_core.__version__,
                "pact_core_revision": _pact_core_revision(),
            },
        )
        write_json_atomic(paths.state, state)
        append_event(paths.events, "loop_initialized", {"loop_id": loop_id})
    except BaseException:
        store.cleanup()
        raise

    progress = ExecutionProgress(
        current_checkpoint=initialization.baseline_commit,
        previous_feedback=initial_feedback,
    )
    round_context = RoundContext(
        loop_id=loop_id,
        config=config,
        workspace=workspace,
        paths=paths,
        store=store,
        initialization=initialization,
        state=state,
        plan=plan,
        repo_instructions=repo_instructions,
        expected_goal_hash=expected_goal_hash,
        verify_command=verify_command,
        worker=worker,
        reviewer=reviewer,
        worker_probe=worker_probe,
        reviewer_probe=reviewer_probe,
        reprobe=_reprobe_turn_if_required,
        io=_execution_io(),
        yield_on_plan_challenge=yield_on_plan_challenge,
    )

    try:
        for round_number in range(1, config.max_rounds + 1):
            execute_round(round_context, progress, round_number=round_number)
            if progress.status != "running":
                break
    except KeyboardInterrupt:
        progress.status = "cancelled"
        progress.stop_reason = "cancelled"
    except BaseException as exc:
        progress.status, progress.stop_reason = finalize_infrastructure_failure(
            _execution_io(),
            store,
            paths,
            state,
            exc=exc,
            rounds=progress.rounds_completed,
            checkpoint=progress.current_checkpoint,
        )
        raise

    status = progress.status
    stop_reason = progress.stop_reason
    rounds_completed = progress.rounds_completed
    current_checkpoint = progress.current_checkpoint

    final_validation_status: FinalValidationStatus | None = None
    try:
        if status == "complete":
            with _operation(paths, state, operation="final_validation"):
                final_validation_status = _run_final_stage(
                    store=store,
                    paths=paths,
                    initialization=initialization,
                    checkpoint=current_checkpoint,
                    command=final_verify_command,
                    timeout_seconds=config.final_verify_timeout_seconds,
                )
            state["final_validation_status"] = final_validation_status
            if final_validation_status not in {"passed", "not_configured"}:
                status = "failed"
                stop_reason = f"final_validation_{final_validation_status}"
    except BaseException as exc:
        status, stop_reason = finalize_infrastructure_failure(
            _execution_io(),
            store,
            paths,
            state,
            exc=exc,
            rounds=rounds_completed,
            checkpoint=current_checkpoint,
        )
        raise

    promote = status == "complete" and promotion_mode == "automatic"
    promotion_status, promotion_reason = _finalize(
        store,
        paths,
        state,
        status=status,
        stop_reason=stop_reason,
        rounds=rounds_completed,
        checkpoint=current_checkpoint,
        promote=promote,
        promotion_deferred=status == "complete" and promotion_mode == "deferred",
    )
    return LoopSummary(
        loop_id=loop_id,
        loop_dir=paths.loop_dir,
        status=status,
        stop_reason=stop_reason,
        rounds=rounds_completed,
        final_checkpoint=current_checkpoint,
        final_patch=paths.final_patch,
        round_gate_status=(
            str(state["round_gate_status"]) if state["round_gate_status"] is not None else None
        ),
        final_validation_status=final_validation_status,
        promotion_status=promotion_status,
        promotion_reason=promotion_reason,
    )


def _finalize(
    store: CheckpointStore,
    paths: LoopPaths,
    state: dict[str, object],
    *,
    status: LoopStatus,
    stop_reason: str,
    rounds: int,
    checkpoint: str,
    promote: bool,
    promotion_deferred: bool = False,
) -> tuple[PromotionStatus, str]:
    """Compatibility wrapper around terminal execution finalization."""
    return finalize_execution(
        _execution_io(),
        store,
        paths,
        state,
        status=status,
        stop_reason=stop_reason,
        rounds=rounds,
        checkpoint=checkpoint,
        promote=promote,
        promotion_deferred=promotion_deferred,
    )


__all__ = ["LoopSummary", "PactLoopError", "run_loop"]
