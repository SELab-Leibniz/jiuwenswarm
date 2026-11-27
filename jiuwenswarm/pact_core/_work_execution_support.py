"""Shared checkpoint and evidence support for current Work Kernel operations."""
from __future__ import annotations

import hashlib
import math
import re
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

from jiuwenswarm import pact_core
from ._execution_artifacts import adapter_manifest_entry
from ._execution_round import RoundContext
from .adapters.base import AgentAdapter
from .artifacts import (LoopPaths, atomic_write_text, build_loop_id,
    build_loop_paths, read_json, round_paths, write_json_atomic)
from .checkpoint import CheckpointStore
from .config import LoopConfig
from .controller import (PactLoopError, _execution_io, _pact_core_revision,
    _probe_or_fail, _reject_unterminated_loop, _reprobe_turn_if_required)
from .goal_tracker import build_initial_goal_tracker, immutable_goal_sha256, sha256_text
from .schemas import (INITIALIZATION_SCHEMA, MANIFEST_SCHEMA, STATE_SCHEMA,
    AdapterProbe, InitializationCheckpoint, TurnRequest, TurnResult)


@dataclass(frozen=True, slots=True)
class _LoadedDraft:
    artifact: Path
    paths: LoopPaths
    initialization: InitializationCheckpoint
    checkpoint: str
    checkpoint_tree: str
    checkpoint_parent: str
    worker_output_missing: bool
    namespace_violations: tuple[str, ...]


class _AbsentRole:
    """Explicitly unavailable role; accidental dispatch is a hard failure."""

    id = "not-invoked"

    def probe(self) -> AdapterProbe:
        return AdapterProbe(adapter_id=self.id, available=False, detail="role not invoked")

    def run_turn(self, request: TurnRequest) -> TurnResult:
        raise AssertionError(f"{request.role} must not be invoked in this execution mode")


def _reject(message: str) -> NoReturn:
    from .kernel import KernelContractError

    raise KernelContractError(message)


def _command(command: str | None) -> str | None:
    return command if command is not None and command.strip() else None


def _plan(config: LoopConfig, *, require_verification: bool = True) -> str:
    workspace = config.workspace.expanduser().resolve()
    path = config.plan_file.expanduser()
    if not path.is_absolute():
        path = workspace / path
    if not workspace.is_dir() or not path.is_file():
        raise PactLoopError("draft workspace and plan must exist")
    if config.max_rounds < 1:
        raise PactLoopError("max_rounds must be positive")
    for name in ("turn_timeout_seconds", "verify_timeout_seconds", "final_verify_timeout_seconds"):
        value = getattr(config, name)
        if not math.isfinite(value) or value <= 0:
            raise PactLoopError(f"{name} must be a positive finite number")
    if require_verification and _command(config.verify_command) is None and not config.allow_unverified:
        raise PactLoopError("draft requires verification or explicit allow_unverified")
    return path.read_text(encoding="utf-8")


def _bindings(config: LoopConfig, plan: str) -> dict[str, object]:
    """Bind validation policy without disclosing opaque commands to either role."""
    return {
        "plan_sha256": sha256_text(plan),
        "verify_command_sha256": sha256_text(_command(config.verify_command) or ""),
        "verify_timeout_seconds": config.verify_timeout_seconds,
        "allow_unverified": config.allow_unverified,
        "final_verify_command_sha256": sha256_text(_command(config.final_verify_command) or ""),
        "final_verify_timeout_seconds": config.final_verify_timeout_seconds,
    }


def _basis(initialization: InitializationCheckpoint) -> dict[str, object]:
    return {
        "head_commit": initialization.head_commit,
        "branch": initialization.branch,
        "index_tree": initialization.index_tree,
        "workspace_tree": initialization.workspace_tree,
    }


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _evidence_paths(paths: LoopPaths) -> dict[str, Path]:
    rpaths = round_paths(paths.loop_dir, 1)
    return {
        "initialization": paths.initialization, "plan": paths.plan,
        "goal_tracker": paths.goal_tracker, "summary": rpaths.summary,
        "checkpoint": rpaths.checkpoint, "worker_result": rpaths.worker_result,
        "verification": rpaths.verification, "patch": rpaths.patch,
        "delta_patch": rpaths.delta_patch, "worker_prompt": rpaths.worker_prompt,
        "manifest": paths.manifest, "state": paths.state,
        "context": paths.loop_dir / "context.md",
    }


def _start(
    config: LoopConfig, *, kind: str, worker: AgentAdapter | None,
    reviewer: AgentAdapter | None, previous: _LoadedDraft | None, feedback: str,
    review_context: str = "",
    require_verification: bool = True,
) -> RoundContext:
    plan = _plan(config, require_verification=require_verification)
    workspace = config.workspace.expanduser().resolve()
    worker_probe = _probe_or_fail(worker, "worker") if worker else _AbsentRole().probe()
    reviewer_probe = _probe_or_fail(reviewer, "reviewer") if reviewer else _AbsentRole().probe()
    # Never reuse a caller's deterministic id after an orphaned/crashed attempt.
    loop_id = f"{kind}-{build_loop_id()}"
    paths = build_loop_paths(workspace, loop_id)
    _reject_unterminated_loop(paths)
    store = CheckpointStore(workspace, loop_id, worktree_root=config.worktree_root)
    init = (
        store.initialize_from_checkpoint(
            previous.initialization, previous.checkpoint,
            expected_tree=previous.checkpoint_tree, expected_parent=previous.checkpoint_parent,
        )
        if previous else store.initialize()
    )
    try:
        tracker = build_initial_goal_tracker(plan)
        checkpoint = store.retained_checkpoint()
        now = datetime.now(UTC).isoformat()
        state: dict[str, object] = {
            "schema": STATE_SCHEMA, "loop_id": loop_id, "status": "running", "stop_reason": "",
            "current_round": 0, "max_rounds": 1,
            "immutable_goal_sha256": immutable_goal_sha256(tracker),
            "source_workspace": str(workspace), "baseline_commit": init.baseline_commit,
            "baseline_tree": init.baseline_tree, "current_checkpoint": checkpoint.commit,
            "current_tree": checkpoint.tree, "checkpoint_ref": init.checkpoint_ref,
            "worker_worktree": str(init.worker_worktree), "allow_unverified": config.allow_unverified,
            "round_gate_status": None, "final_validation_status": None,
            "promotion_status": "not_attempted", "promotion_reason": "",
            "operation": None, "operation_role": None, "operation_status": "idle",
            "operation_started_at": None, "operation_finished_at": None, "updated_at": now,
        }
        atomic_write_text(paths.plan, plan)
        atomic_write_text(paths.goal_tracker, tracker)
        atomic_write_text(paths.loop_dir / "context.md", feedback)
        write_json_atomic(paths.initialization, {"schema": INITIALIZATION_SCHEMA, **asdict(init)})
        write_json_atomic(paths.manifest, {
            "schema": MANIFEST_SCHEMA, "loop_id": loop_id, "created_at": now,
            "workspace": str(workspace), "plan_source": str(config.plan_file),
            "plan_sha256": sha256_text(plan), "worker_adapter": adapter_manifest_entry(worker_probe),
            "reviewer_adapter": adapter_manifest_entry(reviewer_probe), "max_rounds": 1,
            "verify_command": _command(config.verify_command),
            "turn_timeout_seconds": config.turn_timeout_seconds,
            "verify_timeout_seconds": config.verify_timeout_seconds,
            "final_validation": {
                "configured": _command(config.final_verify_command) is not None,
                "timeout_seconds": config.final_verify_timeout_seconds,
                "command_sha256": sha256_text(_command(config.final_verify_command) or "")
                if _command(config.final_verify_command) else "",
            },
            "allow_unverified": config.allow_unverified, "checkpoint_ref": init.checkpoint_ref,
            "worker_worktree": str(init.worker_worktree), "pact_core_version": pact_core.__version__,
            "pact_core_revision": _pact_core_revision(),
        })
        write_json_atomic(paths.state, state)
        io = _execution_io()
        io.append_event(paths.events, "loop_initialized", {"loop_id": loop_id})
        instructions = workspace / "AGENTS.md"
        return RoundContext(
            loop_id=loop_id, config=replace(config, max_rounds=1), workspace=workspace,
            paths=paths, store=store, initialization=init, state=state, plan=plan,
            repo_instructions=instructions.read_text(encoding="utf-8") if instructions.is_file() else "",
            expected_goal_hash=immutable_goal_sha256(tracker), verify_command=_command(config.verify_command),
            worker=worker or _AbsentRole(), reviewer=reviewer or _AbsentRole(),
            worker_probe=worker_probe, reviewer_probe=reviewer_probe,
            reprobe=_reprobe_turn_if_required, io=io, yield_on_plan_challenge=True,
            review_context=review_context,
        )
    except BaseException:
        store.cleanup()
        raise


def _continuation_path(config: LoopConfig, identity: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", identity):
        _reject("invalid Runtime operation identity")
    path = config.workspace.expanduser().resolve() / ".pact" / "continuations" / f"{identity}.json"
    if path != path.resolve():
        _reject("Runtime continuation path cannot be aliased")
    return path


def _continuation(config: LoopConfig, identity: str | None, kind: str, previous: Path | None, review_context: str = "", initial_feedback: str = "", *, require_verification: bool = True) -> dict[str, object] | None:
    if identity is None:
        return None
    from .kernel import workspace_identity
    path = _continuation_path(config, identity)
    binding: dict[str, object] = {
        "schema": "pact-core/runtime-continuation/v1", "operation_id": identity,
        "kind": kind, "source_workspace": str(config.workspace.expanduser().resolve()),
        "source_basis": workspace_identity(config.workspace), "bindings": _bindings(config, _plan(config, require_verification=require_verification)),
        "previous": str(previous) if previous else None,
        "previous_sha256": _digest(previous) if previous else None,
        "review_context_sha256": sha256_text(review_context),
        "initial_feedback_sha256": sha256_text(initial_feedback),
    }
    if not path.exists():
        return dict(binding, stage="new", loop_id=None, prior_loops=[])
    saved = read_json(path)
    if any(saved.get(key) != value for key, value in binding.items()):
        _reject("Runtime continuation request/source binding mismatch")
    return saved


def _save_continuation(config: LoopConfig, saved: dict[str, object] | None, **updates: object) -> None:
    if saved is not None:
        old_loop, new_loop = saved.get("loop_id"), updates.get("loop_id")
        if isinstance(old_loop, str) and new_loop is not None and old_loop != new_loop:
            history = saved.get("prior_loops")
            if not isinstance(history, list):
                _reject("invalid continuation history")
            saved["prior_loops"] = [*history, old_loop]
        saved.update(updates)
        write_json_atomic(_continuation_path(config, str(saved["operation_id"])), saved)


def _candidate_hashes(paths: LoopPaths) -> dict[str, str]:
    rpaths = round_paths(paths.loop_dir, 1)
    candidates = [*_evidence_paths(paths).values(), paths.promotion, paths.final_patch,
        paths.final_validation, paths.loop_dir / "candidate.json", rpaths.review,
        rpaths.reviewer_prompt, rpaths.reviewer_result, rpaths.review_decision,
        rpaths.reviewer_mutation, rpaths.gate]
    return {path.name: _digest(path) for path in candidates if path.is_file()}


def workspace_identity(workspace: Path) -> dict[str, object]:
    """Stable source identity excluding controller/adapter artifact namespaces."""
    snapshot = CheckpointStore(workspace, "identity").workspace_identity()
    return {key: value for key, value in asdict(snapshot).items() if key != "status"}
