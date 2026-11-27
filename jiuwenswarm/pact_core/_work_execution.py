"""Kernel operations for callers that own their execution workflow.

These operations preserve actual files and run the existing verification and
delivery machinery. They never dispatch a Worker or manufacture a Worker turn.
The Runtime owns authorization, operation locking, and canonical Work State.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ._work_execution_support import (
    _LoadedDraft, _basis, _candidate_hashes, _command, _continuation, _continuation_path, _digest,
    _reject, _save_continuation, _start,
)
from ._execution_artifacts import round_key
from ._execution_finalization import finalize, finalize_infrastructure_failure, run_final_stage
from ._execution_round import (
    ExecutionProgress, PreparedRound, RoundContext, validate_prepared_round,
    verify_prepared_round, remaining_timeout,
)
from ._runtime_serialization import decode, encode
from .adapters.base import AgentAdapter
from .artifacts import (
    atomic_write_text, build_loop_paths, read_json, round_paths, write_json_atomic,
)
from .checkpoint import CheckpointStore
from .config import LoopConfig
from .goal_tracker import sha256_text
from .prompts import build_work_reviewer_prompt
from .schemas import CHECKPOINT_SCHEMA, INITIALIZATION_SCHEMA, VERIFICATION_SCHEMA, RoundCheckpoint, VerificationResult

if TYPE_CHECKING:
    from .kernel import ExecutionOutcome

WORK_EXECUTION_SCHEMA = "pact-core/work-execution/v1"
WORK_CANDIDATE_SCHEMA = "pact-core/work-candidate/v1"


@dataclass(frozen=True, slots=True)
class WorkExecutionHandle:
    artifact: Path
    workdir: Path
    loop_id: str
    source_workspace: Path
    input_checkpoint: str
    source_basis: dict[str, object]


@dataclass(frozen=True, slots=True)
class WorkCandidate:
    artifact: Path
    checkpoint: str
    tree: str
    parent: str
    source_workspace: Path
    source_basis: dict[str, object]
    summary: Path
    initialization: Path
    patch: Path
    namespace_violations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WorkVerification:
    artifact: Path
    status: str
    candidate: Path
    checkpoint: str
    verification: Path


def _exact(path: Path) -> Path:
    supplied = path.expanduser().absolute()
    if supplied != supplied.resolve() or not supplied.is_file():
        _reject("work artifact must be an exact existing canonical path")
    return supplied


def _check_evidence(payload: dict[str, object], paths: list[Path]) -> None:
    expected = {str(path): _digest(_exact(path)) for path in paths}
    if payload.get("evidence") != expected:
        _reject("work artifact evidence was changed")


def _candidate(path: Path, *, promotion_outcome: ExecutionOutcome | None = None) -> tuple[WorkCandidate, _LoadedDraft]:
    from .kernel import _read_initialization, recover_promotion

    payload = read_json(_exact(path))
    if payload.get("schema") != WORK_CANDIDATE_SCHEMA:
        _reject("unsupported work candidate schema")
    try:
        candidate = decode(WorkCandidate, payload.get("candidate"))
    except ValueError as exc:
        _reject(f"invalid work candidate: {exc}")
    if candidate.artifact != path or candidate.source_workspace != candidate.source_workspace.resolve():
        _reject("work candidate identity mismatch")
    loop_id = path.parent.name
    paths = build_loop_paths(candidate.source_workspace, loop_id)
    if path != paths.loop_dir / "work-candidate.json":
        _reject("work candidate is outside its canonical loop")
    rpaths = round_paths(paths.loop_dir, 1)
    evidence = [paths.initialization, paths.loop_dir / "work-execution.json",
                paths.loop_dir / "context.md", rpaths.summary, rpaths.checkpoint,
                rpaths.patch, rpaths.delta_patch]
    _check_evidence(payload, evidence)
    initialization = _read_initialization(paths)
    cp = read_json(rpaths.checkpoint)
    if (candidate.initialization != paths.initialization or candidate.summary != rpaths.summary
            or candidate.patch != rpaths.patch or candidate.source_basis != _basis(initialization)
            or cp.get("schema") != CHECKPOINT_SCHEMA
            or cp.get("commit") != candidate.checkpoint or cp.get("tree") != candidate.tree
            or cp.get("parent_commit") != candidate.parent
            or cp.get("namespace_violations") != list(candidate.namespace_violations)
            or cp.get("round_patch_sha256") != _digest(rpaths.delta_patch)
            or cp.get("cumulative_patch_sha256") != _digest(rpaths.patch)):
        _reject("work candidate checkpoint binding mismatch")
    store = CheckpointStore(candidate.source_workspace, loop_id)
    store.restore_promotion_candidate(initialization, candidate.checkpoint)
    if promotion_outcome is None:
        store.assert_source_unchanged()
    else:
        if (promotion_outcome.source_workspace != candidate.source_workspace
                or promotion_outcome.candidate_checkpoint != candidate.checkpoint
                or promotion_outcome.source_head_commit != candidate.source_basis['head_commit']
                or promotion_outcome.source_index_tree != candidate.source_basis['index_tree']
                or promotion_outcome.source_workspace_tree != candidate.source_basis['workspace_tree']):
            _reject("promotion outcome does not match the sealed candidate")
        promoted_input = _read_initialization(build_loop_paths(
            promotion_outcome.source_workspace, promotion_outcome.loop_id))
        if (_basis(promoted_input) != candidate.source_basis
                or promoted_input.baseline_commit != initialization.baseline_commit):
            _reject("promotion input does not match the candidate source branch and baseline")
        # The same validator used by crash recovery checks the gate, authorized
        # patch and current HEAD/index/tree. Acceptance alone is not delivery.
        receipt = recover_promotion(promotion_outcome)
        if receipt is None or receipt.status not in {'applied', 'no_changes'}:
            _reject("candidate requires a completed successful promotion receipt")
    if (store._output(["rev-parse", f"{candidate.checkpoint}^{{tree}}"] ) != candidate.tree
            or store._output(["rev-list", "--parents", "-n", "1", candidate.checkpoint]).split()
            != [candidate.checkpoint, candidate.parent]):
        _reject("work candidate Git object differs from its checkpoint receipt")
    return candidate, _LoadedDraft(path, paths, initialization, candidate.checkpoint,
        candidate.tree, candidate.parent, False, candidate.namespace_violations)


def read_work_candidate(path: Path, *, promotion_outcome: ExecutionOutcome | None = None) -> WorkCandidate:
    """Read a sealed candidate against its original or proven delivered source.

    A completed promotion receipt is required for the delivered-source variant;
    ordinary callers retain the original immutable source-basis check.
    """
    return _candidate(path, promotion_outcome=promotion_outcome)[0]


def _write_operation_input(context: RoundContext, saved: dict[str, object]) -> None:
    write_json_atomic(context.paths.loop_dir / "candidate.json", {
        "schema": "pact-core/work-operation-input/v1",
        **{key: saved[key] for key in (
            "operation_id", "kind", "previous", "previous_sha256", "source_basis",
            "bindings", "review_context_sha256", "initial_feedback_sha256",
        )},
    })


def _check_operation_input(saved: dict[str, object], workspace: Path, loop_id: str) -> WorkCandidate:
    from .kernel import _read_initialization

    paths = build_loop_paths(workspace, loop_id)
    supplied = read_json(_exact(paths.loop_dir / "candidate.json"))
    expected = {"schema": "pact-core/work-operation-input/v1", **{
        key: saved.get(key) for key in (
            "operation_id", "kind", "previous", "previous_sha256", "source_basis",
            "bindings", "review_context_sha256", "initial_feedback_sha256",
        )}}
    if supplied != expected:
        _reject("completed work operation input binding mismatch")
    original = saved.get("previous")
    if not isinstance(original, str) or _digest(_exact(Path(original))) != saved.get("previous_sha256"):
        _reject("completed work operation candidate binding mismatch")
    candidate, loaded = _candidate(Path(original))
    initialization = _read_initialization(paths)
    if (candidate.source_workspace != workspace or candidate.source_basis != _basis(initialization)
            or saved.get("source_basis") != candidate.source_basis
            or loaded.initialization.baseline_commit != initialization.baseline_commit):
        _reject("completed work operation source binding mismatch")
    manifest = read_json(_exact(paths.manifest))
    final = manifest.get("final_validation")
    if not isinstance(final, dict):
        _reject("completed work operation final policy is missing")
    plan_hash = _digest(_exact(paths.plan))
    policy = {
        "plan_sha256": plan_hash,
        "verify_command_sha256": sha256_text(str(manifest.get("verify_command") or "")),
        "verify_timeout_seconds": manifest.get("verify_timeout_seconds"),
        "allow_unverified": manifest.get("allow_unverified"),
        "final_verify_command_sha256": final.get("command_sha256") or sha256_text(""),
        "final_verify_timeout_seconds": final.get("timeout_seconds"),
    }
    if saved.get("bindings") != policy or manifest.get("plan_sha256") != plan_hash:
        _reject("completed work operation policy binding mismatch")
    store = CheckpointStore(workspace, loop_id)
    store.restore_promotion_candidate(initialization, candidate.checkpoint)
    store.assert_source_unchanged()
    checkpoint = read_json(round_paths(paths.loop_dir, 1).checkpoint)
    if (checkpoint.get("commit") != candidate.checkpoint or checkpoint.get("tree") != candidate.tree
            or checkpoint.get("parent_commit") != candidate.parent):
        _reject("completed work operation used a different candidate")
    return candidate


def read_work_operation(workspace: Path, operation_id: str) -> WorkVerification | ExecutionOutcome | None:
    """Recover a completed operation result without repeating external work.

    This validates original input bindings, source and recorded evidence, not
    delivery authorization. Runtime must match the current revision before commit.
    An absent or unfinished result is never permission to replay the operation.
    """
    from .kernel import ExecutionOutcome, _validate_evidence_paths

    workspace = workspace.expanduser().resolve()
    path = _continuation_path(LoopConfig(workspace, workspace / "PLAN.md"), operation_id)
    if not path.exists():
        return None
    saved = read_json(_exact(path))
    if (saved.get("schema") != "pact-core/runtime-continuation/v1"
            or saved.get("operation_id") != operation_id
            or saved.get("source_workspace") != str(workspace)):
        _reject("invalid work operation identity")
    if saved.get("stage") != "completed":
        return None
    loop_id = saved.get("loop_id")
    if not isinstance(loop_id, str):
        _reject("completed work operation is missing its loop identity")
    paths = build_loop_paths(workspace, loop_id)
    candidate = _check_operation_input(saved, workspace, loop_id)
    kind = saved.get("kind")
    if kind == "work-verification":
        result = decode(WorkVerification, saved.get("outcome"))
        verification = round_paths(paths.loop_dir, 1).verification
        if (result.artifact != verification or result.verification != verification
                or str(result.candidate) != saved.get("previous")
                or saved.get("verification_sha256") != _digest(_exact(verification))
                or saved.get("outcome_hashes") != _candidate_hashes(paths)):
            _reject("completed work verification evidence mismatch")
        payload = read_json(verification)
        if payload.get("status") != result.status or result.checkpoint != candidate.checkpoint:
            _reject("completed work verification result mismatch")
        return result
    if kind in {"work-feedback", "work-acceptance"}:
        outcome = decode(ExecutionOutcome, saved.get("outcome"))
        if outcome.loop_id != loop_id or outcome.source_workspace != workspace:
            _reject("completed work acceptance identity mismatch")
        _validate_evidence_paths(outcome, paths)
        if saved.get("outcome_hashes") != _candidate_hashes(paths):
            _reject("completed work acceptance evidence mismatch")
        state = read_json(paths.state)
        expected_ready = (kind == "work-acceptance" and state.get("status") == "complete"
            and state.get("round_gate_status") == "complete"
            and state.get("final_validation_status") in {"passed", "not_configured"}
            and state.get("promotion_status") == "not_attempted")
        if (outcome.candidate_checkpoint != candidate.checkpoint
                or outcome.source_head_commit != candidate.source_basis["head_commit"]
                or outcome.source_index_tree != candidate.source_basis["index_tree"]
                or outcome.source_workspace_tree != candidate.source_basis["workspace_tree"]
                or outcome.status != state.get("status") or outcome.stop_reason != state.get("stop_reason")
                or outcome.rounds != state.get("current_round")
                or outcome.round_gate_status != state.get("round_gate_status")
                or outcome.final_validation_status != state.get("final_validation_status")
                or outcome.promotion_status != state.get("promotion_status")
                or outcome.promotion_reason != state.get("promotion_reason")
                or outcome.promotion_mode != "deferred" or outcome.promotion_ready != expected_ready):
            _reject("completed work acceptance result differs from recorded state")
        return outcome
    _reject("unsupported work operation kind")


def prepare_work_execution(
    workspace: Path, *, operation_id: str, previous_candidate: Path | None = None,
    worktree_root: Path | None = None, context: str = "",
) -> WorkExecutionHandle:
    """Allocate an isolated checkout; the caller may use any execution strategy."""
    import re

    if not re.fullmatch(r"[0-9a-f]{32}", operation_id):
        _reject("work execution requires a 32-character hexadecimal operation identity")
    workspace = workspace.expanduser().resolve()
    loop_id = f"work-{operation_id}"
    paths = build_loop_paths(workspace, loop_id)
    if paths.loop_dir != paths.loop_dir.resolve():
        _reject("work execution directory cannot be aliased")
    artifact = paths.loop_dir / "work-execution.json"
    binding = {
        "previous": str(previous_candidate) if previous_candidate else None,
        "previous_sha256": _digest(_exact(previous_candidate)) if previous_candidate else None,
        "context_sha256": sha256_text(context),
        "worktree_root": str(worktree_root.expanduser().resolve()) if worktree_root else None,
    }
    if artifact.exists():
        saved = read_json(_exact(artifact))
        if saved.get("schema") != WORK_EXECUTION_SCHEMA or saved.get("binding") != binding:
            _reject("work execution request changed")
        handle = _load_handle(artifact)
        if (paths.loop_dir / "work-candidate.json").exists() or not handle.workdir.is_dir():
            _reject("work execution already ended; use its retained candidate")
        from .kernel import _read_initialization
        initialization = _read_initialization(paths)
        if previous_candidate is not None:
            previous, _ = _candidate(previous_candidate)
            if previous.checkpoint != handle.input_checkpoint or previous.source_basis != handle.source_basis:
                _reject("work execution input no longer matches its previous candidate")
        elif handle.input_checkpoint != initialization.baseline_commit:
            _reject("work execution original input checkpoint changed")
        store = CheckpointStore(workspace, loop_id, worktree_root=worktree_root)
        store.restore_promotion_candidate(initialization, handle.input_checkpoint)
        store.assert_source_unchanged()
        return handle
    if paths.loop_dir.exists():
        _reject("unfinished work initialization requires explicit reconciliation")
    store = CheckpointStore(workspace, loop_id, worktree_root=worktree_root)
    if previous_candidate is None:
        initialization = store.initialize()
    else:
        candidate, loaded = _candidate(previous_candidate)
        if candidate.source_workspace != workspace or candidate.namespace_violations:
            _reject("work candidate source mismatch or namespace violation")
        initialization = store.initialize_from_checkpoint(loaded.initialization,
            loaded.checkpoint, expected_tree=loaded.checkpoint_tree,
            expected_parent=loaded.checkpoint_parent)
    handle = WorkExecutionHandle(artifact, initialization.worker_worktree, loop_id,
        workspace, store.current_commit, _basis(initialization))
    # No cleanup on failure: unknown external initialization is never silently
    # repurposed. Runtime reset/adopt can reconcile the persisted ownership.
    atomic_write_text(paths.loop_dir / "context.md", context)
    write_json_atomic(paths.initialization, {"schema": INITIALIZATION_SCHEMA, **asdict(initialization)})
    write_json_atomic(artifact, {
        "schema": WORK_EXECUTION_SCHEMA, "binding": binding, "handle": encode(handle),
        "evidence": {str(path): _digest(path) for path in
                     (paths.initialization, paths.loop_dir / "context.md")},
    })
    return handle


def _load_handle(path: Path) -> WorkExecutionHandle:
    from .kernel import _read_initialization

    payload = read_json(_exact(path))
    if payload.get("schema") != WORK_EXECUTION_SCHEMA:
        _reject("unsupported work execution schema")
    try:
        handle = decode(WorkExecutionHandle, payload.get("handle"))
    except ValueError as exc:
        _reject(f"invalid work execution handle: {exc}")
    paths = build_loop_paths(handle.source_workspace, handle.loop_id)
    if path != paths.loop_dir / "work-execution.json" or handle.artifact != path:
        _reject("work execution path binding mismatch")
    _check_evidence(payload, [paths.initialization, paths.loop_dir / "context.md"])
    init = _read_initialization(paths)
    if init.worker_worktree != handle.workdir or _basis(init) != handle.source_basis:
        _reject("work execution initialization binding mismatch")
    return handle


def read_work_execution(path: Path) -> WorkExecutionHandle:
    """Read an original execution handle, including one whose checkout closed."""
    return _load_handle(path)


def _write_checkpoint(context: RoundContext, cp: RoundCheckpoint, summary: str) -> PreparedRound:
    paths = context.paths
    rpaths = round_paths(paths.loop_dir, 1)
    atomic_write_text(rpaths.summary, summary)
    atomic_write_text(rpaths.patch, cp.cumulative_diff)
    atomic_write_text(rpaths.delta_patch, cp.round_diff)
    write_json_atomic(rpaths.checkpoint, {
        "schema": CHECKPOINT_SCHEMA, "round": 1, "parent_commit": cp.parent_commit,
        "commit": cp.commit, "tree": cp.tree, "status": cp.status,
        "changed_paths": cp.changed_paths, "namespace_violations": cp.namespace_violations,
        "round_patch_sha256": sha256_text(cp.round_diff),
        "cumulative_patch_sha256": sha256_text(cp.cumulative_diff),
    })
    return PreparedRound(cp, None, summary, not summary.strip(), cp.parent_commit,
        round_key(context.loop_id, 1, cp.commit, summary),
        paths.goal_tracker.read_text(encoding="utf-8"))


def capture_work_candidate(handle: WorkExecutionHandle, *, summary: str = "") -> WorkCandidate:
    """Capture the actual filesystem, ignoring external Git commits and staging."""
    from .kernel import _read_initialization

    if _load_handle(handle.artifact) != handle:
        _reject("caller changed work execution handle")
    paths = build_loop_paths(handle.source_workspace, handle.loop_id)
    artifact = paths.loop_dir / "work-candidate.json"
    if artifact.exists():
        candidate, loaded = _candidate(artifact)
        if candidate.summary.read_text(encoding="utf-8") != summary:
            _reject("repeated capture has different handoff information")
        store = CheckpointStore(handle.source_workspace, handle.loop_id,
            worktree_root=handle.workdir.parents[2])
        store.restore_promotion_candidate(loaded.initialization, candidate.checkpoint)
        store.cleanup()
        return candidate
    initialization = _read_initialization(paths)
    store = CheckpointStore(handle.source_workspace, handle.loop_id,
        worktree_root=handle.workdir.parents[2])
    store.restore_promotion_candidate(initialization, handle.input_checkpoint)
    store.assert_source_unchanged()
    cp = store.checkpoint_worker(round_number=1)
    rpaths = round_paths(paths.loop_dir, 1)
    atomic_write_text(rpaths.summary, summary)
    atomic_write_text(rpaths.patch, cp.cumulative_diff)
    atomic_write_text(rpaths.delta_patch, cp.round_diff)
    write_json_atomic(rpaths.checkpoint, {
        "schema": CHECKPOINT_SCHEMA, "round": 1, "parent_commit": cp.parent_commit,
        "commit": cp.commit, "tree": cp.tree, "status": cp.status,
        "changed_paths": cp.changed_paths, "namespace_violations": cp.namespace_violations,
        "round_patch_sha256": sha256_text(cp.round_diff),
        "cumulative_patch_sha256": sha256_text(cp.cumulative_diff),
    })
    candidate = WorkCandidate(artifact, cp.commit, cp.tree, cp.parent_commit,
        handle.source_workspace, handle.source_basis, rpaths.summary,
        paths.initialization, rpaths.patch, tuple(cp.namespace_violations))
    evidence = [paths.initialization, handle.artifact, paths.loop_dir / "context.md",
                rpaths.summary, rpaths.checkpoint, rpaths.patch, rpaths.delta_patch]
    write_json_atomic(artifact, {"schema": WORK_CANDIDATE_SCHEMA,
        "candidate": encode(candidate), "evidence": {str(path): _digest(path) for path in evidence}})
    store.cleanup()
    return candidate


def verify_work_candidate(config: LoopConfig, candidate: Path, *, operation_id: str,
                          deadline: float | None = None) -> WorkVerification:
    """Run a public check separately; its receipt belongs to this exact policy."""
    remaining_timeout(config.verify_timeout_seconds, deadline)
    saved = _continuation(config, operation_id, "work-verification", candidate, require_verification=False)
    assert saved is not None
    if saved.get("stage") == "completed":
        recovered = read_work_operation(config.workspace, operation_id)
        if not isinstance(recovered, WorkVerification):
            _reject("unexpected work verification receipt")
        result = recovered
        _candidate(candidate)
        if saved.get("verification_sha256") != _digest(_exact(result.verification)):
            _reject("saved verification was changed")
        return result
    if saved.get("stage") != "new":
        _reject("unfinished verification requires explicit recovery or reset")
    current, loaded = _candidate(candidate)
    if current.source_workspace != config.workspace.expanduser().resolve():
        _reject("verification candidate belongs to another source")
    context = _start(config, kind="work-verify", worker=None, reviewer=None,
        previous=loaded, feedback="Public verification of retained candidate.", require_verification=False)
    _save_continuation(config, saved, stage="started", loop_id=context.loop_id)
    try:
        cp = context.store.retained_checkpoint(namespace_violations=current.namespace_violations)
        prepared = _write_checkpoint(context, cp, current.summary.read_text(encoding="utf-8"))
        _write_operation_input(context, saved)
        verification = verify_prepared_round(context, prepared, round_number=1, deadline=deadline)
        context.store.assert_source_unchanged()
        finalize(context.io, context.store, context.paths, context.state, status="failed",
            stop_reason="verification_recorded", rounds=1, checkpoint=cp.commit, promote=False)
        result = WorkVerification(round_paths(context.paths.loop_dir, 1).verification,
            verification.status, candidate, cp.commit, round_paths(context.paths.loop_dir, 1).verification)
        _save_continuation(config, saved, stage="completed", outcome=encode(result),
            verification_sha256=_digest(result.verification), outcome_hashes=_candidate_hashes(context.paths))
        return result
    except BaseException as exc:
        finalize_infrastructure_failure(context.io, context.store, context.paths,
            context.state, exc=exc, rounds=0, checkpoint=context.store.current_commit)
        raise


def accept_work_candidate(
    config: LoopConfig, candidate: Path, reviewer: AgentAdapter, *, operation_id: str,
    review_context: str = "", feedback_only: bool = False,
    deadline: float | None = None,
    verification_evidence: Path | None = None,
) -> ExecutionOutcome:
    """Inspect retained files; only final acceptance evaluates the delivery gate.

    Feedback admits failed or unavailable checks and an initial empty handoff.
    It retains isolation and integrity checks without interpreting a verdict.
    The absolute deadline bounds every external stage without resetting budget.
    """
    from .kernel import ExecutionEvidenceRefs, ExecutionOutcome

    recorded: VerificationResult | None = None
    if verification_evidence is not None:
        if not feedback_only:
            _reject("saved verification is diagnostic only; acceptance must verify again")
        evidence_path = _exact(verification_evidence)
        if not evidence_path.is_relative_to(config.workspace.expanduser().resolve() / '.pact'):
            _reject("saved diagnostic verification is outside PACT storage")
        original_input = read_json(_exact(evidence_path.parent / 'candidate.json'))
        original_id = original_input.get('operation_id')
        if not isinstance(original_id, str) or original_input.get('previous') != str(candidate):
            _reject("saved diagnostic verification belongs to a different candidate")
        original_result = read_work_operation(config.workspace, original_id)
        original_path = (original_result.verification if isinstance(original_result, WorkVerification)
            else original_result.evidence.verification if original_result is not None else None)
        if original_path != evidence_path:
            _reject("saved diagnostic verification is not a completed operation result")
        payload = read_json(evidence_path)
        if payload.get('schema') != VERIFICATION_SCHEMA:
            _reject("unsupported saved diagnostic verification schema")
        recorded = decode(VerificationResult, {key: value for key, value in payload.items()
            if key not in {'schema', 'round'}})
        # The Runtime validates current candidate/definition ownership. Bind the
        # exact immutable input into the Kernel continuation as diagnostic context.
        review_context += (f'\nPreviously recorded verification evidence (not rerun): '
            f'{evidence_path}; sha256={_digest(evidence_path)}. '
            'This feedback cannot establish a fresh passing result or authorize delivery.')
    remaining_timeout(config.turn_timeout_seconds, deadline)
    saved = _continuation(config, operation_id,
        "work-feedback" if feedback_only else "work-acceptance", candidate, review_context,
        require_verification=False)
    assert saved is not None
    if saved.get("stage") == "completed":
        recovered = read_work_operation(config.workspace, operation_id)
        if not isinstance(recovered, ExecutionOutcome):
            _reject("unexpected work acceptance receipt")
        return recovered
    if saved.get("stage") != "new":
        _reject("unfinished reviewer ownership is unknown; explicit reset is required")
    current, loaded = _candidate(candidate)
    if current.source_workspace != config.workspace.expanduser().resolve():
        _reject("acceptance candidate belongs to another source")
    context = _start(config, kind="work-review" if feedback_only else "work-accept",
        worker=None, reviewer=reviewer, previous=loaded,
        feedback="Candidate produced by an external execution; no Worker role was invoked.",
        review_context=review_context, require_verification=False)
    paths, store = context.paths, context.store
    _save_continuation(config, saved, stage="started", loop_id=context.loop_id)
    progress = ExecutionProgress(current_checkpoint=store.current_commit)
    try:
        cp = store.retained_checkpoint(namespace_violations=current.namespace_violations)
        summary = current.summary.read_text(encoding="utf-8")
        prepared = _write_checkpoint(context, cp, summary)
        _write_operation_input(context, saved)
        if recorded is None:
            verification = verify_prepared_round(context, prepared, round_number=1, deadline=deadline)
        else:
            verification = recorded
            rpaths = round_paths(paths.loop_dir, 1)
            context.io.write_json(rpaths.verification,
                {'schema': VERIFICATION_SCHEMA, 'round': 1, **asdict(recorded)})
            context.io.write_text(rpaths.verification_log,
                f'Previously recorded verification; no command executed.\n{recorded.stdout_tail}\n{recorded.stderr_tail}\n')
            context.io.append_event(paths.events, 'verification_reused',
                {'round': 1, 'source': str(verification_evidence), 'status': recorded.status})
        if remaining_timeout(config.turn_timeout_seconds, deadline) == 0:
            progress.status, progress.stop_reason = "failed", "budget_exhausted"
            context.io.write_text(round_paths(paths.loop_dir, 1).review,
                "budget_exhausted: deadline reached before reviewer execution.\n")
        elif not summary.strip() and not feedback_only:
            progress.status, progress.stop_reason = "failed", "handoff_missing"
        elif feedback_only or verification.status == "passed" or (verification.status == "unavailable" and config.allow_unverified):
            validate_prepared_round(context, progress, round_number=1,
                prepared=prepared, verification=verification,
                feedback_only=feedback_only, deadline=deadline,
                reviewer_prompt_builder=lambda scratch: build_work_reviewer_prompt(
                    goal_contract=context.plan,
                    baseline_checkpoint=context.initialization.baseline_commit,
                    output_checkpoint=cp.commit, summary=summary, verification=verification,
                    repo_instructions=context.repo_instructions,
                    review_context=review_context, reviewer_scratch=scratch,
                    feedback_only=feedback_only))
        else:
            progress.status, progress.stop_reason = "failed", "verification_" + verification.status
        final_status = None
        if progress.status == "complete" and not feedback_only:
            final_status = run_final_stage(context.io, store=store, paths=paths,
                initialization=context.initialization, checkpoint=cp.commit,
                command=_command(config.final_verify_command),
                timeout_seconds=config.final_verify_timeout_seconds, deadline=deadline)
            context.state["final_validation_status"] = final_status
            if final_status not in {"passed", "not_configured"}:
                progress.status, progress.stop_reason = "failed", (
                    "budget_exhausted" if remaining_timeout(config.final_verify_timeout_seconds, deadline) == 0
                    else "final_validation_" + final_status)
        store.assert_source_unchanged()
        store.assert_control_ownership(cp.commit)
        promotion_status, reason = finalize(context.io, store, paths, context.state,
            status=progress.status, stop_reason=progress.stop_reason, rounds=1,
            checkpoint=cp.commit, promote=False, promotion_deferred=progress.status == "complete")
        rpaths = round_paths(paths.loop_dir, 1)
        outcome = ExecutionOutcome(context.loop_id, paths.loop_dir, context.workspace,
            context.initialization.head_commit, context.initialization.index_tree,
            context.initialization.workspace_tree, progress.status, progress.stop_reason,
            1, cp.commit, str(context.state["round_gate_status"]) if context.state["round_gate_status"] else None,
            final_status, "deferred", promotion_status, reason,
            progress.status == "complete" and not feedback_only,
            ExecutionEvidenceRefs(paths.manifest, paths.initialization, paths.state, paths.events,
                None, rpaths.verification, rpaths.reviewer_result if rpaths.reviewer_result.exists() else None,
                rpaths.gate if rpaths.gate.exists() else None,
                paths.final_validation if paths.final_validation.exists() else None,
                paths.final_patch, paths.promotion))
        _save_continuation(config, saved, stage="completed", outcome=encode(outcome),
            outcome_hashes=_candidate_hashes(paths))
        return outcome
    except BaseException as exc:
        finalize_infrastructure_failure(context.io, store, paths, context.state, exc=exc,
            rounds=progress.rounds_completed, checkpoint=progress.current_checkpoint)
        raise
