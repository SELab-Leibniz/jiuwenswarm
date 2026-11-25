"""Workflow-independent work operations and the sole canonical-state committer."""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
import json
import math
from pathlib import Path
import shlex
import sys
from typing import TYPE_CHECKING, cast
import uuid

from ..adapters.base import AgentAdapter
from ..artifacts import atomic_write_text, read_json, write_json_atomic
from ..config import LoopConfig
from .._runtime_serialization import decode, encode
from ..kernel import (
    ExecutionOutcome, PromotionAuthorization, PromotionReceipt, WorkCandidate,
    WorkExecutionHandle, WorkVerification, accept_work_candidate, capture_work_candidate,
    prepare_work_execution, promote, read_work_candidate, read_work_execution, read_work_operation,
    recover_promotion, verify_work_candidate, workspace_identity,
)
from .boundary import (
    assert_source_basis, begin_operation, campaign_lock, executor_lock,
    finish_operation, marker_path, read_operation, require_clear_boundary,
)
from .handoff import parse_handoff_observations
from .checks import active_checks, normalize_checks, normalize_decisions
from .recovery import (
    check_hashes, operation_input, prepared_operation, receipt_hashes, record_result,
)
from .requests import publish_creation_directory, request_identity
from .schemas import CAMPAIGN_REVISION_SCHEMA, RuntimeContractError, RuntimeStateError, goal_contract_criteria, validate_goal_contract
from .store import CampaignReader, _WorkStore, data_digest, digest, now
from .types import (
    AcceptancePolicy, AcceptanceReceipt, CandidateRef, ReviewEvidence,
    VerificationEvidence, WorkSnapshot, object_record,
)

if TYPE_CHECKING:
    from .export import ExportReceipt, LatestCandidateExport


@dataclass
class ExecutionHandle:
    work_id: str
    operation_id: str
    expected_revision: int
    kernel_handle: WorkExecutionHandle
    _owner: WorkRuntime
    _locks: ExitStack
    _closed: bool = False

    @property
    def workdir(self) -> Path:
        return self.kernel_handle.workdir

    @property
    def input_checkpoint(self) -> str:
        return self.kernel_handle.input_checkpoint

    @property
    def artifact_dir(self) -> Path:
        return self.kernel_handle.artifact.parent / 'host'

    def close(self) -> None:
        """Release process locks, never claim an unfinished external action ended."""
        if not self._closed:
            self._closed = True
            self._locks.close()

    def __enter__(self) -> ExecutionHandle:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class WorkRuntime:
    """Trusted hosts choose operations; only this class commits work facts."""
    def __init__(self, workspace: Path, *, worktree_root: Path | None = None) -> None:
        self.workspace = workspace.expanduser().resolve()
        self.worktree_root = worktree_root.expanduser().resolve() if worktree_root else None

    def create_work(self, *, request_id: str, contract: dict[str, object],
                    policy: AcceptancePolicy | None = None, work_id: str | None = None,
                    advisory: dict[str, object] | None = None, constraints: tuple[str, ...] = ()) -> WorkSnapshot:
        contract = validate_goal_contract(contract)
        policy = policy or AcceptancePolicy()
        constraints = _constraints(constraints)
        inputs: dict[str, object] = {'contract': contract, 'policy': policy.payload(), 'constraints': list(constraints)}
        with request_identity(self.workspace, request_id, inputs, campaign_id=work_id) as identity:
            store = _WorkStore(self.workspace, identity.campaign_id)
            with executor_lock(store), campaign_lock(store):
                ownership = store.paths.campaign_dir / 'creation-request.json'
                if store.paths.current_state.exists():
                    if not ownership.exists() or read_json(ownership) != identity.record:
                        raise RuntimeContractError('creation ownership conflicts with existing work')
                    return self.read_work(store.campaign_id)
                if store.paths.campaign_dir.exists():
                    if not ownership.exists() or read_json(ownership) != identity.record:
                        raise RuntimeContractError('existing work is not owned by this request')
                else:
                    publish_creation_directory(store, identity.record)
                if workspace_identity(self.workspace) != identity.source_basis:
                    raise RuntimeStateError('source changed during work creation')
                seed = store.paths.campaign_dir / 'creation.json'
                if seed.exists():
                    creation = read_json(seed)
                else:
                    creation = {'schema': 'pact-runtime/work-creation/v1', 'operation_id': uuid.uuid4().hex,
                                'created_at': now(), 'advisory': _json_object(advisory or {})}
                    write_json_atomic(seed, creation)
                op = str(creation['operation_id'])
                candidate_path = self.workspace / '.pact' / 'loops' / f'work-{op}' / 'work-candidate.json'
                if candidate_path.exists():
                    handle = read_work_execution(candidate_path.with_name('work-execution.json'))
                    candidate = capture_work_candidate(handle, summary='Initial filesystem input; implementation and acceptance remain unverified.')
                else:
                    handle = prepare_work_execution(self.workspace, operation_id=op,
                        worktree_root=self.worktree_root, context='Capture the actual user input before execution.')
                    candidate = capture_work_candidate(handle, summary='Initial filesystem input; implementation and acceptance remain unverified.')
                if candidate.source_basis != identity.source_basis:
                    raise RuntimeStateError('input checkpoint differs from request source')
                candidate_ref = self._candidate_record(store, op, candidate)
                contract_ref = store.write_record('contract-1', {
                    'revision': 1, 'goal_contract': contract, 'policy': policy.payload(),
                    'constraints': list(constraints), 'authority': 'user', 'reason': 'initial_request',
                }, schema=CAMPAIGN_REVISION_SCHEMA)
                state: dict[str, object] = {
                    'status': 'active', 'workspace_basis': identity.source_basis,
                    'contract_ref': contract_ref, 'contract_revision': 1,
                    'candidate_ref': candidate_ref, 'verification_ref': None, 'review_ref': None,
                    'acceptance_ref': None, 'delivery_ref': None, 'advisory': creation['advisory'],
                    'handoff': {}, 'unresolved_assumptions': [], 'user_decisions': [],
                    'review_requested': False, 'evidence_refs': [candidate_ref],
                    'acceptance_brief': {}, 'checks': [],
                    'last_operation_id': None, 'created_at': creation['created_at'],
                }
                store.initialize(state)
        return self.read_work(identity.campaign_id)

    def read_work(self, work_id: str) -> WorkSnapshot:
        reader = CampaignReader(self.workspace, work_id)
        state = reader.read_current_state()
        contract_record = reader.read_campaign_revision()
        candidate = self._candidate(reader, state)
        current = CandidateRef(work_id, candidate.artifact, candidate.checkpoint, candidate.tree)
        verification: VerificationEvidence | None = None
        if state['verification_ref'] is not None:
            rec = reader.read_record(state['verification_ref'])
            result = object_record(rec['verification'], 'verification')
            verification = VerificationEvidence(Path(str(rec['artifact'])), current, str(result['status']),
                cast(str | None, result.get('command')), cast(int | None, result.get('exit_code')),
                str(result.get('stdout_tail', '')), str(result.get('stderr_tail', '')),
                tuple(_objects(rec.get('check_results', []))))
        review: ReviewEvidence | None = None
        if state['review_ref'] is not None:
            rec = reader.read_record(state['review_ref'])
            reviewed = self._candidate(reader, {'candidate_ref': rec['candidate_ref']})
            reviewed_ref = CandidateRef(work_id, reviewed.artifact, reviewed.checkpoint, reviewed.tree)
            # The canonical text path is stored explicitly; no latest-file search.
            review_path = Path(str(rec['review_artifact']))
            feedback = review_path.read_text(encoding='utf-8') if review_path.is_file() else str(rec.get('feedback', ''))
            review = ReviewEvidence(review_path, reviewed_ref, bool(rec['complete']), feedback,
                                    tuple(cast(list[str], rec.get('reasons', []))))
        acceptance: AcceptanceReceipt | None = None
        if state['acceptance_ref'] is not None:
            rec = reader.read_record(state['acceptance_ref'])
            if rec.get('basis') != self._acceptance_basis(reader, state):
                raise RuntimeStateError('acceptance binding differs from current work')
            acceptance = AcceptanceReceipt(work_id, reader.resolve_ref(state['acceptance_ref']), current)
        delivery: PromotionReceipt | None = None
        if state['delivery_ref'] is not None:
            rec = reader.read_record(state['delivery_ref'])
            delivery = decode(PromotionReceipt, rec['receipt'])
        handoff = dict(object_record(state['handoff'], 'handoff'))
        handoff['summary'] = candidate.summary.read_text(encoding='utf-8')
        return WorkSnapshot(work_id, cast(int, state['revision']), str(state['status']),
            cast(int, state['contract_revision']), reader.read_contract(), reader.read_policy(),
            tuple(cast(list[str], contract_record['constraints'])), current,
            _json_object(state['advisory']), handoff, verification, review, acceptance, delivery,
            cast(bool, state['review_requested']), tuple(_objects(state['unresolved_assumptions'])),
            tuple(_objects(state['user_decisions'])), reader.paths.campaign_dir,
            _json_object(state['acceptance_brief']), tuple(_objects(state['checks'])))

    def begin_execution(self, work_id: str, *, expected_revision: int, context: str = '',
                        draft: bool = False) -> ExecutionHandle:
        if not isinstance(draft, bool):
            raise RuntimeContractError('draft must be boolean')
        store = _WorkStore(self.workspace, work_id)
        locks = ExitStack()
        try:
            locks.enter_context(executor_lock(store))
            locks.enter_context(campaign_lock(store))
            state = self._mutable(store, expected_revision)
            policy = store.read_policy()
            if not policy.verify_command and not active_checks(_objects(state['checks'])) and not policy.allow_unverified and not draft:
                raise RuntimeContractError('verification_required: configure public verification before execution')
            candidate = self._candidate(store, state)
            operation = begin_operation(store, state, action='save_candidate', parameters={'context': context, 'draft': draft})
            kernel_handle = prepare_work_execution(self.workspace, operation_id=operation,
                previous_candidate=candidate.artifact, worktree_root=self.worktree_root, context=context)
            record = read_operation(store)
            record['execution_handle'] = encode(kernel_handle)
            write_json_atomic(marker_path(store), record)
            return ExecutionHandle(work_id, operation, expected_revision, kernel_handle, self, locks)
        except BaseException:
            locks.close()
            raise

    def save_candidate(self, handle: ExecutionHandle, *, summary: str,
                       advisory: dict[str, object] | None = None) -> WorkSnapshot:
        if handle._owner is not self or handle._closed:
            raise RuntimeStateError('execution handle is closed or belongs to another runtime')
        store = _WorkStore(self.workspace, handle.work_id)
        try:
            state = store.assert_expected_revision(handle.expected_revision)
            assert_source_basis(store, state)
            operation = read_operation(store)
            if operation['id'] != handle.operation_id or operation.get('execution_handle') != encode(handle.kernel_handle):
                raise RuntimeStateError('execution ownership changed')
            parameters = object_record(operation['parameters'], 'operation parameters')
            parameters.update(summary=summary, advisory=_json_object(advisory) if advisory is not None else None)
            operation['parameters'] = parameters
            write_json_atomic(marker_path(store), operation)
            candidate = capture_work_candidate(handle.kernel_handle, summary=summary)
            record_result(store, {'kind': 'candidate', 'candidate': encode(candidate)}, receipt_hashes(candidate))
            return self._finish(store)
        finally:
            handle.close()

    def verify_candidate(self, work_id: str, *, expected_revision: int,
                         deadline: float | None = None) -> WorkSnapshot:
        return self._evaluate(work_id, expected_revision, 'verify', None, 3600.0, deadline=deadline)

    def review_candidate(self, work_id: str, *, expected_revision: int, reviewer: AgentAdapter,
                         timeout_seconds: float = 3600.0, context: str = '',
                         deadline: float | None = None,
                         verification_evidence: VerificationEvidence | None = None) -> WorkSnapshot:
        return self._evaluate(work_id, expected_revision, 'review', reviewer, timeout_seconds,
                              context=context, deadline=deadline, verification_evidence=verification_evidence)

    def accept_candidate(self, work_id: str, *, expected_revision: int, reviewer: AgentAdapter,
                         timeout_seconds: float = 3600.0, deadline: float | None = None) -> WorkSnapshot:
        return self._evaluate(work_id, expected_revision, 'accept', reviewer, timeout_seconds, deadline=deadline)

    def set_acceptance_brief(self, work_id: str, *, expected_revision: int, text: str, source: str) -> WorkSnapshot:
        """Persist advisory acceptance analysis without changing the user contract."""
        return self._change(work_id, expected_revision, 'acceptance_brief', {
            'text': _reason(text), 'source': _reason(source)})

    def register_checks(self, work_id: str, *, expected_revision: int,
                        checks: list[dict[str, object]], reason: str) -> WorkSnapshot:
        """Add/revise derived methods; supplied policy checks are never replaced."""
        _reason(reason)
        store = _WorkStore(self.workspace, work_id)
        with executor_lock(store), campaign_lock(store):
            state = self._mutable(store, expected_revision)
            candidate = self._candidate(store, state)
            read_work_candidate(candidate.artifact)
            normalized = normalize_checks(self.workspace, candidate.checkpoint, checks,
                                          _objects(state['checks']), reason)
            if normalized == state['checks']:
                return self.read_work(work_id)
            payload: dict[str, object] = {'checks': normalized, 'reason': reason}
            begin_operation(store, state, action='register_checks', parameters=payload)
            record_result(store, {'kind': 'register_checks', 'payload': payload}, {})
            return self._finish(store)

    def review_checks(self, work_id: str, *, expected_revision: int,
                      decisions: list[dict[str, object]], review: ReviewEvidence) -> WorkSnapshot:
        """Commit host-interpreted method decisions from a valid current Review.

        This changes supplemental methods only. It cannot waive the original
        Goal, supplied verification, or turn historical passes into acceptance.
        """
        store = _WorkStore(self.workspace, work_id)
        with executor_lock(store), campaign_lock(store):
            state = self._mutable(store, expected_revision)
            snapshot = self.read_work(work_id)
            if review is None or review != snapshot.review or review.candidate != snapshot.candidate:
                raise RuntimeStateError('check decisions require a current recorded Review')
            record = store.read_record(state['review_ref'])
            outcome = decode(ExecutionOutcome, record['outcome'])
            role = read_json(outcome.evidence.reviewer_result) if outcome.evidence.reviewer_result else {}
            if (role.get('invoked') is not True or role.get('status') != 'completed'
                    or not review.feedback.strip() or set(review.reasons).intersection({
                        'reviewer_mutation', 'budget_exhausted', 'reviewer_output_missing', 'reviewer_protocol_error'})):
                raise RuntimeStateError('check decisions require an executed, valid, read-only Review')
            normalized = normalize_decisions(_objects(state['checks']), decisions, str(state['review_ref']))
            if normalized == state['checks']:
                return snapshot
            if record.get('checks_fingerprint') != data_digest(state['checks']):
                raise RuntimeStateError('Review used different check definitions or dispositions')
            payload: dict[str, object] = {'checks': normalized, 'decisions': decisions, 'review_ref': state['review_ref']}
            begin_operation(store, state, action='review_checks', parameters=payload)
            record_result(store, {'kind': 'review_checks', 'payload': payload}, {})
            return self._finish(store)

    def export_candidate(self, candidate: CandidateRef, destination: Path) -> 'ExportReceipt':
        from .export import export_candidate
        return export_candidate(self, candidate, destination)

    def export_latest_candidate(self, work_id: str, destination: Path, *,
                                expected_revision: int | None = None) -> 'LatestCandidateExport':
        """Export current or eligible history to an exact new directory, without delivery."""
        from .export import export_latest_candidate
        return export_latest_candidate(self, work_id, destination, expected_revision=expected_revision)

    def _evaluate(self, work_id: str, revision: int, action: str,
                  reviewer: AgentAdapter | None, timeout: float, *, context: str = '',
                  deadline: float | None = None,
                  verification_evidence: VerificationEvidence | None = None) -> WorkSnapshot:
        if deadline is not None and (isinstance(deadline, bool) or not isinstance(deadline, (float, int))
                                     or not math.isfinite(deadline)):
            raise RuntimeContractError('deadline must be finite UTC epoch seconds')
        if not isinstance(context, str):
            raise RuntimeContractError('review context must be text')
        store = _WorkStore(self.workspace, work_id)
        with executor_lock(store), campaign_lock(store):
            state = self._mutable(store, revision)
            evidence_path = None
            if verification_evidence is not None:
                if action != 'review':
                    raise RuntimeContractError('saved evidence is only available for diagnostic review')
                record = self._saved_verification(store, state)
                if self.read_work(work_id).verification != verification_evidence:
                    raise RuntimeStateError('diagnostic evidence differs from current recorded verification')
                evidence_path = Path(str(record['artifact']))
            operation = begin_operation(store, state, action=action,
                parameters={'timeout_seconds': timeout, 'context': context, 'deadline': deadline,
                    'verification_ref': state['verification_ref'] if evidence_path else None})
            config = self._config(store, state, operation, full=action == 'accept', timeout=timeout, deadline=deadline)
            candidate = self._candidate(store, state)
            if action == 'verify':
                result: WorkVerification | ExecutionOutcome = verify_work_candidate(config, candidate.artifact,
                    operation_id=operation, deadline=deadline)
            else:
                assert reviewer is not None
                result = accept_work_candidate(config, candidate.artifact, reviewer=reviewer, operation_id=operation,
                    review_context=self._review_context(store, state) + '\n' + context,
                    feedback_only=action == 'review', deadline=deadline,
                    verification_evidence=evidence_path)
            record_result(store, {'kind': action, 'outcome': encode(result)}, receipt_hashes(result))
            return self._finish(store)

    def _saved_verification(self, store: _WorkStore, state: dict[str, object]) -> dict[str, object]:
        if state['verification_ref'] is None:
            raise RuntimeStateError('no current recorded verification is available for diagnosis')
        record = store.read_record(state['verification_ref'])
        if (record.get('kind') != 'verify' or record.get('candidate_ref') != state['candidate_ref']
                or record.get('checks_fingerprint') != data_digest(state['checks'])):
            raise RuntimeStateError('diagnostic verification candidate or check definitions changed')
        candidate = self._candidate(store, state)
        read_work_candidate(candidate.artifact)
        if read_json(Path(str(record['artifact']))) != record['verification']:
            raise RuntimeStateError('diagnostic verification differs from its recorded result')
        return record

    def deliver(self, work_id: str, *, expected_revision: int, acceptance: AcceptanceReceipt) -> WorkSnapshot:
        store = _WorkStore(self.workspace, work_id)
        with executor_lock(store), campaign_lock(store):
            state = store.assert_expected_revision(expected_revision)
            if state['status'] == 'completed':
                if self.read_work(work_id).acceptance != acceptance:
                    raise RuntimeStateError('delivery replay uses a different acceptance')
                assert_source_basis(store, state)
                return self.read_work(work_id)
            state = self._mutable(store, expected_revision)
            outcome = self._authorized_outcome(store, state, acceptance)
            begin_operation(store, state, action='deliver', parameters={'acceptance_ref': state['acceptance_ref']})
            receipt = promote(PromotionAuthorization(outcome=outcome,
                expected_source_workspace=self.workspace,
                expected_source_head_commit=outcome.source_head_commit,
                expected_source_index_tree=outcome.source_index_tree,
                expected_source_workspace_tree=outcome.source_workspace_tree,
                expected_candidate_checkpoint=outcome.candidate_checkpoint))
            record_result(store, {'kind': 'deliver', 'receipt': encode(receipt)}, {str(receipt.artifact): digest(receipt.artifact)})
            return self._finish(store)

    def recover(self, work_id: str, *, expected_revision: int | None = None) -> WorkSnapshot:
        store = _WorkStore(self.workspace, work_id)
        with executor_lock(store), campaign_lock(store):
            if expected_revision is not None:
                store.assert_expected_revision(expected_revision)
            if not marker_path(store).exists():
                return self.read_work(work_id)
            operation = read_operation(store)
            prepared = prepared_operation(store)
            if prepared is not None:
                write_json_atomic(store.paths.current_state, prepared)
                finish_operation(store, str(operation['id']))
                return self.read_work(work_id)
            original = operation_input(store)
            if store.read_current_state() != original:
                raise RuntimeStateError('operation input no longer matches current work')
            if operation.get('result') is None:
                self._recover_result(store, original, operation)
            return self._finish(store)

    def _recover_result(self, store: _WorkStore, state: dict[str, object], operation: dict[str, object]) -> None:
        action = operation['action']
        op = str(operation['id'])
        if action in ('save_candidate', 'adopt'):
            path = self.workspace / '.pact' / 'loops' / f'work-{op}' / 'work-candidate.json'
            if not path.is_file():
                raise RuntimeStateError('not_auto_resumable: external executor has no completed checkpoint')
            if action == 'save_candidate':
                handle = decode(WorkExecutionHandle, operation['execution_handle'])
                params = object_record(operation['parameters'], 'candidate parameters')
                candidate = capture_work_candidate(handle, summary=str(params['summary']))
            else:
                handle = read_work_execution(path.with_name('work-execution.json'))
                params = object_record(operation['parameters'], 'adoption parameters')
                candidate = capture_work_candidate(handle,
                    summary=f"User adopted actual source: {params['reason']}; revalidate requirements.")
            record_result(store, {'kind': 'candidate' if action == 'save_candidate' else 'adopt',
                                  'candidate': encode(candidate)}, receipt_hashes(candidate))
        elif action in ('verify', 'review', 'accept'):
            result = read_work_operation(self.workspace, op)
            if result is None:
                raise RuntimeStateError('not_auto_resumable: operation has no complete result')
            record_result(store, {'kind': action, 'outcome': encode(result)}, receipt_hashes(result))
        elif action == 'deliver':
            # The source may already have changed. Validate the exact Kernel
            # receipt instead of calling promotion a second time.
            ref = state.get('acceptance_ref')
            record = store.read_record(ref)
            if record.get('basis') != self._acceptance_basis(store, state):
                raise RuntimeStateError('delivery acceptance changed')
            outcome = decode(ExecutionOutcome, record['outcome'])
            receipt = recover_promotion(outcome)
            if receipt is None:
                raise RuntimeStateError('not_auto_resumable: no complete promotion receipt; reconcile source explicitly')
            record_result(store, {'kind': 'deliver', 'receipt': encode(receipt)}, {str(receipt.artifact): digest(receipt.artifact)})
        else:
            raise RuntimeStateError('not_auto_resumable: interrupted transition requires explicit reset')

    def update_advisory(self, work_id: str, *, expected_revision: int, advisory: dict[str, object]) -> WorkSnapshot:
        return self._change(work_id, expected_revision, 'advisory', {'advisory': _json_object(advisory)})

    def pause(self, work_id: str, *, expected_revision: int, reason: str = 'user_paused') -> WorkSnapshot:
        return self._change(work_id, expected_revision, 'pause', {'status': 'paused', 'reason': _reason(reason)})

    def resume(self, work_id: str, *, expected_revision: int, reason: str = 'user_resumed') -> WorkSnapshot:
        return self._change(work_id, expected_revision, 'resume', {'status': 'active', 'reason': _reason(reason)})

    def request_review(self, work_id: str, *, expected_revision: int, reason: str = 'user_requested_review') -> WorkSnapshot:
        return self._change(work_id, expected_revision, 'request_review', {'review_requested': True, 'acceptance_ref': None, 'reason': _reason(reason)})

    def revise_work(self, work_id: str, *, expected_revision: int, reason: str,
                    contract: dict[str, object] | None = None, policy: AcceptancePolicy | None = None,
                    constraints: tuple[str, ...] | None = None) -> WorkSnapshot:
        store = _WorkStore(self.workspace, work_id)
        current = store.assert_expected_revision(expected_revision)
        old = store.read_campaign_revision()
        value = {'goal_contract': validate_goal_contract(contract) if contract is not None else old['goal_contract'],
                 'policy': policy.payload() if policy is not None else old['policy'],
                 'constraints': list(_constraints(constraints)) if constraints is not None else old['constraints'],
                 'authority': 'user', 'reason': _reason(reason), 'revision': cast(int, current['contract_revision']) + 1}
        if all(value[key] == old[key] for key in ('goal_contract', 'policy', 'constraints')):
            raise RuntimeContractError('revision must change requirements or acceptance policy')
        return self._change(work_id, expected_revision, 'revise', value)

    def record_user_decision(self, work_id: str, *, expected_revision: int, question_id: str, answer: str) -> WorkSnapshot:
        return self._change(work_id, expected_revision, 'user_decision', {
            'question_id': question_id, 'answer': _reason(answer), 'authority': 'user'})

    def reset_operation(self, work_id: str, *, expected_revision: int, reason: str,
                        executor_stopped: bool = False) -> WorkSnapshot:
        _reason(reason)
        if executor_stopped is not True:
            raise RuntimeContractError('reset requires executor_stopped=True after confirming the old executor ended')
        store = _WorkStore(self.workspace, work_id)
        with executor_lock(store), campaign_lock(store):
            state = store.assert_expected_revision(expected_revision)
            assert_source_basis(store, state)
            if not marker_path(store).exists():
                return self.read_work(work_id)
            op = read_operation(store)
            if op.get('target') is not None or op.get('result') is not None:
                raise RuntimeStateError('completed operation evidence must be recovered, not reset')
            # Recorded Kernel results can exist before the Runtime result write.
            candidate = self.workspace / '.pact' / 'loops' / f"work-{op['id']}" / 'work-candidate.json'
            if candidate.exists() or read_work_operation(self.workspace, str(op['id'])) is not None:
                raise RuntimeStateError('recorded result must be recovered, not reset')
            op.update(reset_reason=reason, executor_stopped=True)
            write_json_atomic(marker_path(store), op)
            finish_operation(store, str(op['id']))
            return self.read_work(work_id)

    def adopt(self, work_id: str, *, expected_revision: int, reason: str,
              executor_stopped: bool = False) -> WorkSnapshot:
        _reason(reason)
        store = _WorkStore(self.workspace, work_id)
        with executor_lock(store), campaign_lock(store):
            state = store.assert_expected_revision(expected_revision)
            if state['status'] == 'completed':
                raise RuntimeStateError('completed work cannot adopt a new source; create a new work')
            if marker_path(store).exists():
                if executor_stopped is not True:
                    raise RuntimeContractError('adopting interrupted work requires executor_stopped=True')
                interrupted = read_operation(store)
                operation_input(store)
                interrupted.update(resolution='user_adopted_source', executor_stopped=True,
                                   resolution_reason=reason)
                write_json_atomic(marker_path(store), interrupted)
                finish_operation(store, str(interrupted['id']))
            # Explicit User adoption is the only operation allowed to begin from
            # a changed source. Its original input snapshot remains journal-bound.
            operation = self._begin_adopt(store, state, reason)
            handle = prepare_work_execution(self.workspace, operation_id=operation, worktree_root=self.worktree_root,
                context=f'User source adoption: {reason}')
            candidate = capture_work_candidate(handle, summary=f'User adopted actual source: {reason}; revalidate requirements.')
            record_result(store, {'kind': 'adopt', 'candidate': encode(candidate)}, receipt_hashes(candidate))
            return self._finish(store)

    def _begin_adopt(self, store: _WorkStore, state: dict[str, object], reason: str) -> str:
        from .schemas import OPERATION_SCHEMA
        op = uuid.uuid4().hex
        write_json_atomic(marker_path(store), {'schema': OPERATION_SCHEMA, 'campaign_id': store.campaign_id,
            'id': op, 'action': 'adopt', 'parameters': {'reason': reason}, 'expected_revision': state['revision'],
            'source_basis': state['workspace_basis'], 'input_state_ref': state['snapshot_ref'],
            'input_state_sha256': digest(store.resolve_ref(state['snapshot_ref'])),
            'phase': 'started', 'target': None, 'result': None, 'promotion': None, 'created_at': now()})
        return op

    def _change(self, work_id: str, revision: int, action: str, payload: dict[str, object]) -> WorkSnapshot:
        store = _WorkStore(self.workspace, work_id)
        with executor_lock(store), campaign_lock(store):
            state = self._mutable(store, revision, permit_paused=True)
            if action == 'user_decision':
                question = next((q for q in _objects(state['unresolved_assumptions'])
                                 if q.get('id') == payload['question_id']), None)
                if question is None:
                    raise RuntimeContractError('user answer must reference a current unresolved question')
                payload = {**payload, 'question': question, 'contract_revision': state['contract_revision']}
            begin_operation(store, state, action=action, parameters=payload)
            record_result(store, {'kind': action, 'payload': payload}, {})
            return self._finish(store)

    def _mutable(self, store: _WorkStore, revision: int, *, permit_paused: bool = False) -> dict[str, object]:
        state = store.assert_expected_revision(revision)
        require_clear_boundary(store)
        assert_source_basis(store, state)
        if state['status'] == 'completed' or state['status'] == 'paused' and not permit_paused:
            raise RuntimeStateError('work does not allow this operation in its current state')
        return state

    def _candidate(self, reader: CampaignReader, state: dict[str, object]) -> WorkCandidate:
        record = reader.read_record(state['candidate_ref'])
        try:
            candidate = decode(WorkCandidate, record['candidate'])
        except ValueError as exc:
            raise RuntimeStateError(f'invalid candidate record: {exc}') from exc
        if candidate.source_workspace != self.workspace:
            raise RuntimeStateError('candidate belongs to another source')
        return candidate

    def _candidate_record(self, store: _WorkStore, op: str, candidate: WorkCandidate) -> str:
        return store.write_record(f'{op}-candidate', {'kind': 'candidate', 'candidate': encode(candidate), 'hashes': receipt_hashes(candidate)})

    def _acceptance_basis(self, reader: CampaignReader, state: dict[str, object]) -> str:
        return data_digest({'contract': digest(reader.resolve_ref(state['contract_ref'])),
            'candidate': digest(reader.resolve_ref(state['candidate_ref'])),
            'checks': state['checks'], 'acceptance_brief': state['acceptance_brief'],
            'decisions': state['user_decisions'], 'questions': state['unresolved_assumptions']})

    def _authorized_outcome(self, store: _WorkStore, state: dict[str, object], receipt: AcceptanceReceipt) -> ExecutionOutcome:
        if state['acceptance_ref'] is None or receipt.work_id != store.campaign_id:
            raise RuntimeStateError('no acceptance bound to this work')
        snapshot = self.read_work(store.campaign_id)
        if receipt != snapshot.acceptance or snapshot.unresolved_assumptions:
            raise RuntimeStateError('acceptance receipt does not match current work')
        record = store.read_record(state['acceptance_ref'])
        if record.get('kind') != 'accept' or record.get('complete') is not True:
            raise RuntimeStateError('feedback does not authorize delivery')
        outcome = decode(ExecutionOutcome, record['outcome'])
        if not outcome.promotion_ready:
            raise RuntimeStateError('acceptance does not authorize promotion')
        return outcome

    def _config(self, store: _WorkStore, state: dict[str, object], op: str, *, full: bool, timeout: float,
                deadline: float | None = None) -> LoopConfig:
        contract_record = store.read_campaign_revision()
        policy = store.read_policy()
        plan_path = store.paths.campaign_dir / 'operations-inputs' / f'{op}.md'
        content = json.dumps({'goal_contract': contract_record['goal_contract'],
            'user_constraints': contract_record['constraints']}, ensure_ascii=False, indent=2)
        if plan_path.exists() and plan_path.read_text(encoding='utf-8') != content:
            raise RuntimeStateError('operation Goal input changed')
        atomic_write_text(plan_path, content)
        commands = [policy.verify_command]
        if full and policy.closeout_verify_command and policy.closeout_verify_command not in commands:
            commands.append(policy.closeout_verify_command)
        actual = [command for command in commands if command]
        command = ' && '.join(f'( {cmd}\n)' for cmd in actual) if len(actual) > 1 else actual[0] if actual else None
        derived = active_checks(_objects(state['checks']))
        if derived:
            check_manifest = plan_path.with_suffix('.checks.json')
            methods: list[dict[str, object]] = [{'name': f'public-{i + 1}', 'command': cmd, 'source': 'supplied'}
                       for i, cmd in enumerate(actual)]
            methods.extend({key: value for key, value in item.items()
                            if key not in {'disposition', 'method_review'}} for item in derived)
            write_json_atomic(check_manifest, {'schema': 'pact-runtime/check-run/v1',
                'candidate': self._candidate(store, state).checkpoint, 'checks': methods,
                'checks_fingerprint': data_digest(state['checks']), 'deadline': deadline})
            runner = Path(__file__).with_name('check_runner.py')
            command = shlex.join([sys.executable, '-I', str(runner), str(check_manifest), digest(check_manifest)])
        return LoopConfig(self.workspace, plan_path, max_rounds=1, verify_command=command,
            verify_timeout_seconds=policy.verify_timeout_seconds, allow_unverified=policy.allow_unverified,
            final_verify_command=policy.final_verify_command if full else None,
            final_verify_timeout_seconds=policy.final_verify_timeout_seconds,
            turn_timeout_seconds=timeout, worktree_root=self.worktree_root)

    def _review_context(self, store: _WorkStore, state: dict[str, object]) -> str:
        candidate = self._candidate(store, state)
        context = {'handoff': state['handoff'], 'unresolved_questions': state['unresolved_assumptions'],
                   'user_decisions': state['user_decisions'], 'candidate_tree': candidate.tree,
                   'acceptance_brief': state['acceptance_brief'], 'derived_checks': state['checks'],
                   'evidence_policy': 'The brief and derived checks are advisory methods, not new user requirements. '
                       'Compare them with the entire original Goal; test existence or a proposal is not execution evidence.'}
        if state['verification_ref'] is not None:
            verification = store.read_record(state['verification_ref'])
            context['previous_check_results'] = verification.get('check_results', [])
            context['completed_check_logs'] = [path for path in cast(dict[str, str], verification['hashes'])
                                               if Path(path).name.startswith('check-') and path.endswith('.log')]
        return json.dumps(context, ensure_ascii=False)

    def _check_results(self, artifact: Path, state: dict[str, object],
                       candidate: WorkCandidate, *, full: bool = False) -> tuple[list[dict[str, object]], dict[str, str]]:
        path = artifact.parent / 'check-results.json'
        derived = active_checks(_objects(state['checks']))
        if not derived:
            return [], {}
        if not path.exists():
            if read_json(artifact).get('status') == 'passed':
                raise RuntimeStateError('passed verification is missing derived check evidence')
            return [], {}
        data = read_json(path)
        if (data.get('schema') != 'pact-runtime/check-results/v1'
                or data.get('candidate') != candidate.checkpoint
                or data.get('checks_fingerprint') != data_digest(state['checks'])):
            raise RuntimeStateError('derived check evidence does not match the candidate and definitions')
        policy = _WorkStore(self.workspace, str(state['campaign_id'])).read_policy()
        commands = [policy.verify_command]
        if full and policy.closeout_verify_command and policy.closeout_verify_command not in commands:
            commands.append(policy.closeout_verify_command)
        expected: list[dict[str, object]] = [
            {'name': f'public-{i + 1}', 'source': 'supplied', 'command': cmd, 'fingerprint': None}
            for i, cmd in enumerate(command for command in commands if command)]
        expected.extend(derived)
        results = _objects(data['results'])
        if len(results) > len(expected):
            raise RuntimeStateError('unexpected derived check results')
        for result, definition in zip(results, expected):
            if any(result.get(key) != definition.get(key) for key in ('name', 'source', 'command', 'fingerprint')):
                raise RuntimeStateError('check result differs from its registered definition')
            if (result.get('status') not in ('passed', 'failed')
                    or result.get('status') == 'passed' and (type(result.get('exit_code')) is not int or result['exit_code'] != 0)):
                raise RuntimeStateError('invalid check execution result')
        if read_json(artifact).get('status') == 'passed' and (
                len(results) != len(expected) or any(item['status'] != 'passed' for item in results)):
            raise RuntimeStateError('passed verification lacks a complete passing check set')
        hashes = {str(path): digest(path)}
        for index in range(1, len(results) + 1):
            for stream in ('stdout', 'stderr'):
                log = artifact.parent / f'check-{index:03d}-{stream}.log'
                if not log.is_file() or log.is_symlink():
                    raise RuntimeStateError('completed check is missing its original output log')
                hashes[str(log)] = digest(log)
        return results, hashes

    def _finish(self, store: _WorkStore) -> WorkSnapshot:
        operation = read_operation(store)
        state = operation_input(store)
        if store.read_current_state() != state:
            raise RuntimeStateError('operation acknowledgement targets stale work')
        check_hashes(store, operation.get('hashes', {}))
        result = object_record(operation.get('result'), 'operation result')
        params = object_record(operation.get('parameters'), 'operation parameters')
        op = str(operation['id'])
        kind = result['kind']
        expected_kind = 'candidate' if operation['action'] == 'save_candidate' else operation['action']
        if kind != expected_kind:
            raise RuntimeStateError('operation result kind does not match requested action')
        next_state = dict(state, last_operation_id=op)
        refs = list(cast(list[str], state['evidence_refs']))
        if kind in ('candidate', 'adopt'):
            candidate = decode(WorkCandidate, result['candidate'])
            if kind != 'adopt' and candidate.source_basis != state['workspace_basis']:
                raise RuntimeStateError('candidate was captured from another source basis')
            ref = self._candidate_record(store, op, candidate)
            refs.append(ref)
            summary = candidate.summary.read_text(encoding='utf-8')
            observed = parse_handoff_observations(summary,
                requirement_ids=[c['id'] for c in goal_contract_criteria(store.read_contract())],
                input_checkpoint=candidate.parent)
            questions = list(_objects(state['unresolved_assumptions']))
            answered = {d['question_id'] for d in _objects(state['user_decisions'])}
            known = {q['id'] for q in questions} | answered
            for assumption in _objects(observed['assumptions']):
                key = data_digest({'contract_ref': state['contract_ref'], 'requirements': assumption['requirement_ids'],
                                   'question': assumption['assumption']})
                if assumption.get('material') is True and key not in known:
                    questions.append(dict(assumption, id=key))
                    known.add(key)
            next_state.update(candidate_ref=ref, handoff=observed, unresolved_assumptions=questions,
                verification_ref=None, acceptance_ref=None, delivery_ref=None,
                status='blocked' if candidate.namespace_violations else 'active')
            if params.get('advisory') is not None:
                next_state['advisory'] = params['advisory']
            if kind == 'adopt':
                next_state['workspace_basis'] = candidate.source_basis
        elif kind == 'verify':
            verification = decode(WorkVerification, result['outcome'])
            current_candidate = self._candidate(store, state)
            if (verification.candidate != current_candidate.artifact
                    or verification.checkpoint != current_candidate.checkpoint):
                raise RuntimeStateError('verification candidate does not match current work')
            value = read_json(verification.verification)
            check_results, extra_hashes = self._check_results(verification.verification, state, current_candidate)
            ref = store.write_record(op, {'kind': 'verify', 'artifact': str(verification.verification),
                'verification': value, 'check_results': check_results, 'checks_fingerprint': data_digest(state['checks']),
                'candidate_ref': state['candidate_ref'], 'hashes': receipt_hashes(verification) | extra_hashes})
            refs.append(ref)
            next_state['verification_ref'] = ref
            next_state['acceptance_ref'] = None
        elif kind in ('review', 'accept'):
            outcome = decode(ExecutionOutcome, result['outcome'])
            basis = object_record(state['workspace_basis'], 'source basis')
            if (outcome.source_workspace != self.workspace
                    or outcome.source_head_commit != basis['head_commit']
                    or outcome.source_index_tree != basis['index_tree']
                    or outcome.source_workspace_tree != basis['workspace_tree']
                    or outcome.candidate_checkpoint != self._candidate(store, state).checkpoint):
                raise RuntimeStateError('acceptance outcome source or candidate mismatch')
            # Evidence paths come from the Kernel facade, not role-provided paths.
            verification_value = read_json(outcome.evidence.verification) if outcome.evidence.verification else {}
            reused_ref = params.get('verification_ref')
            if reused_ref is not None:
                if kind != 'review' or reused_ref != state['verification_ref']:
                    raise RuntimeStateError('invalid diagnostic verification binding')
                prior = self._saved_verification(store, state)
                if verification_value != prior['verification']:
                    raise RuntimeStateError('diagnostic verification copy differs from recorded evidence')
                check_results = _objects(prior['check_results'])
                extra_hashes = cast(dict[str, str], prior['hashes'])
            else:
                check_results, extra_hashes = (self._check_results(outcome.evidence.verification, state,
                    self._candidate(store, state), full=kind == 'accept') if outcome.evidence.verification else ([], {}))
            if reused_ref is not None:
                # Diagnosis produces a Review, never another execution fact.
                # Preserve evidence identity so observers do not increment fault
                # counters or infer a newly passing check from the diagnostic copy.
                verify_ref = str(reused_ref)
            else:
                verify_ref = store.write_record(f'{op}-verification', {'kind': 'verify',
                    'artifact': str(outcome.evidence.verification), 'verification': verification_value,
                    'check_results': check_results, 'checks_fingerprint': data_digest(state['checks']),
                    'candidate_ref': state['candidate_ref'],
                    'hashes': ({str(outcome.evidence.verification): digest(outcome.evidence.verification)}
                               if outcome.evidence.verification else {}) | extra_hashes})
            # Existing round path naming is owned by artifacts, not inferred from mtime.
            from ..artifacts import round_paths
            review_path = round_paths(outcome.loop_dir, 1).review
            decision_path = round_paths(outcome.loop_dir, 1).review_decision
            complete = bool(kind == 'accept' and outcome.promotion_ready and not state['unresolved_assumptions'])
            reasons = [] if complete else [outcome.stop_reason]
            gate_record = read_json(outcome.evidence.gate_decision) if outcome.evidence.gate_decision else {}
            if ('reviewer_mutation' in cast(list[str], gate_record.get('blocked_by', []))
                    and 'reviewer_mutation' not in reasons):
                # Formal Gate rejection uses a broad stop reason. Preserve the
                # mutation fact publicly so callers do not treat that rejection
                # as usable diagnostic feedback.
                reasons.append('reviewer_mutation')
            role_record = read_json(outcome.evidence.reviewer_result) if outcome.evidence.reviewer_result else {}
            decision_record = read_json(decision_path) if decision_path.is_file() else {}
            if (role_record and (role_record.get('invoked') is not True or role_record.get('status') != 'completed')
                    or decision_record.get('verdict_source') == 'controller_synthesized'):
                if 'reviewer_output_missing' not in reasons:
                    reasons.append('reviewer_output_missing')
            if state['unresolved_assumptions']:
                reasons.append('unresolved_user_questions')
            ref = store.write_record(op, {'kind': kind, 'outcome': encode(outcome),
                'basis': self._acceptance_basis(store, state), 'complete': complete,
                'checks_fingerprint': data_digest(state['checks']),
                'reused_verification_ref': reused_ref,
                'candidate_ref': state['candidate_ref'],
                'review_artifact': str(review_path), 'reasons': reasons,
                'feedback': '; '.join(reasons),
                'hashes': receipt_hashes(outcome) | receipt_hashes(review_path) | receipt_hashes(decision_path)})
            refs.extend([ref] if reused_ref is not None else [verify_ref, ref])
            next_state.update(verification_ref=verify_ref, review_ref=ref,
                              acceptance_ref=ref if complete else None, review_requested=False)
            if state['unresolved_assumptions']:
                next_state['status'] = 'blocked'
        elif kind == 'deliver':
            receipt = decode(PromotionReceipt, result['receipt'])
            accepted = store.read_record(state['acceptance_ref'])
            outcome = decode(ExecutionOutcome, accepted['outcome'])
            recovered = recover_promotion(outcome)
            if recovered != receipt or receipt.status not in ('applied', 'no_changes'):
                raise RuntimeStateError('delivery lacks an exact completed Kernel receipt')
            ref = store.write_record(op, {'kind': 'delivery', 'receipt': encode(receipt),
                'acceptance_ref': state['acceptance_ref'], 'hashes': {str(receipt.artifact): digest(receipt.artifact)}})
            refs.append(ref)
            next_state.update(status='completed', delivery_ref=ref, workspace_basis=workspace_identity(self.workspace))
        elif kind == 'revise':
            payload = object_record(result['payload'], 'contract revision')
            ref = store.write_record(f'{op}-contract', payload, schema=CAMPAIGN_REVISION_SCHEMA)
            old_goal = store.read_contract()
            new_goal = object_record(payload['goal_contract'], 'Goal Contract')
            old_criteria = {c['id']: c['text'] for c in goal_contract_criteria(old_goal)}
            new_criteria = {c['id']: c['text'] for c in goal_contract_criteria(new_goal)}
            affected = {key for key in old_criteria if old_criteria[key] != new_criteria.get(key)}
            questions = [q for q in _objects(state['unresolved_assumptions'])
                         if not affected.intersection(cast(list[str], q['requirement_ids']))]
            next_state.update(contract_ref=ref, contract_revision=payload['revision'], acceptance_ref=None,
                              verification_ref=None, review_ref=None, unresolved_assumptions=questions)
            if not questions and state['status'] == 'blocked':
                next_state['status'] = 'active'
        elif kind == 'user_decision':
            answer = object_record(result['payload'], 'User decision')
            next_state.update(user_decisions=[*_objects(state['user_decisions']), answer],
                unresolved_assumptions=[q for q in _objects(state['unresolved_assumptions']) if q['id'] != answer['question_id']],
                acceptance_ref=None, review_ref=None, status='active' if state['status'] == 'blocked' else state['status'])
        elif kind in ('acceptance_brief', 'register_checks', 'review_checks'):
            payload = object_record(result['payload'], 'acceptance method')
            ref = store.write_record(op, {'kind': kind, **payload})
            refs.append(ref)
            if kind == 'acceptance_brief':
                next_state['acceptance_brief'] = {**payload, 'fingerprint': data_digest(payload)}
            else:
                next_state.update(checks=payload['checks'], verification_ref=None)
            next_state['acceptance_ref'] = None
        elif kind in ('pause', 'resume', 'advisory', 'request_review'):
            payload = object_record(result['payload'], 'state change')
            next_state.update({key: value for key, value in payload.items() if key != 'reason'})
        else:
            raise RuntimeStateError(f'unknown core operation result: {kind}')
        next_state['evidence_refs'] = refs
        if workspace_identity(self.workspace) != next_state['workspace_basis']:
            raise RuntimeStateError('source changed before operation acknowledgement')
        store.commit(expected_revision=cast(int, state['revision']), state=next_state)
        finish_operation(store, op)
        return self.read_work(store.campaign_id)


def _json_object(value: object) -> dict[str, object]:
    normalized = json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    return object_record(normalized, 'advisory metadata')


def _objects(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise RuntimeStateError('expected records')
    return [object_record(item, 'record') for item in value]


def _reason(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeContractError('a nonempty User reason or answer is required')
    return value


def _constraints(values: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(values, tuple) or any(not isinstance(v, str) or not v.strip() for v in values):
        raise RuntimeContractError('constraints must be a tuple of nonempty strings')
    return values
