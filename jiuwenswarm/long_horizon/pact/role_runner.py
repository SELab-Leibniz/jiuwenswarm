"""Bind all four PACT semantic roles to native Jiuwen worker turns.

Deterministic role rules derive from Chrys
6fcfb3321892735631b49f33236b1e2a988ded2d:src/chrys/pact/role_runner.py.
The model loop, tools and session lifecycle belong to the Jiuwen worker.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import threading
import time
from pathlib import Path
from typing import Any, Literal

from .budget import RoleBudgetExhausted, preserve_role_budget
from .review_context import cleanup_review_context, prepare_review_context
from .protocol import (
    _JSON_FOLLOWUP_PROMPT, _JSON_FOLLOWUP_TIMEOUT_SECONDS,
    _ROLE_PROTOCOL_REMINDERS, _campaign_context,
    _is_json_object, _preserve_repair_semantics, _protocol_payload,
    _replan_instead_of_retry, _restore_existing_missions,
)

SemanticRole = Literal['worker', 'reviewer', 'planner', 'manager']
SEMANTIC_ROLES = ('worker', 'reviewer', 'planner', 'manager')
_REVIEW_PROMPT = '''
Adapter transport requirement: finish with exactly one JSON object as the text
of your final message. It must contain `verdict` equal to `complete`, `continue`,
or `failed`. To challenge the plan, use `continue` and a `plan_challenge` object
with non-empty `reason` and `gap_signature`, and `recommended_action` equal to
`replan`, `retry`, `request_user`, or `block`. No other fields or Markdown fences.
The host records this decision artifact. Your tools are read-only: do not write
or alter the checkout. Put review evidence in tool observations before the final
decision. Completion requires the deterministic public verification to pass.
'''


class RoleTurnCancelled(RuntimeError):
    """The invocation was cancelled; this is not a Work State decision."""

    code = "INTERRUPTED"


class JiuwenRoleAdapter:
    def __init__(self, *, semantic_role: SemanticRole, worker: Any,
                 outer_loop: asyncio.AbstractEventLoop, campaign_id: str,
                 workspace: Path, output_dir: Path, abort_event: threading.Event,
                 context_dir: Path | None = None):
        if semantic_role not in SEMANTIC_ROLES:
            raise ValueError(f'Unknown semantic role: {semantic_role}')
        self.id = f'jiuwenswarm-native-{semantic_role}'
        self.semantic_role = semantic_role
        self.worker = worker
        self.outer_loop = outer_loop
        self.campaign_id = campaign_id
        self.workspace = workspace
        self.context_dir = context_dir
        self.output_dir = output_dir
        self.abort_event = abort_event
        self.sequence = 0
        self.active_task: asyncio.Task | None = None

    def probe(self):
        from pact_core.schemas import AdapterProbe
        return AdapterProbe(adapter_id=self.id, available=True,
                            version='49c9f5b4c66d193e4168bfa9c82c2df025c773f0',
                            binary_name='jiuwenswarm-native-worker',
                            isolation_mode='fresh_native_session_per_role_turn')

    def run_turn(self, request):
        # pact-core uses role='worker' for Manager and Planner, so bind role on
        # construction, never infer semantic behavior from TurnRequest.role.
        expected = 'reviewer' if self.semantic_role == 'reviewer' else 'worker'
        if request.role != expected:
            raise ValueError(f'{self.semantic_role} expected {expected}, got {request.role}')
        if self.abort_event.is_set():
            raise RoleTurnCancelled('PACT invocation cancelled')
        self.sequence += 1
        return asyncio.run_coroutine_threadsafe(
            self._run_turn(request, self.sequence), self.outer_loop,
        ).result()

    async def cancel_current_turn(self) -> None:
        self.abort_event.set()
        task = self.active_task
        if task is not None and not task.done():
            task.cancel()
            done, _ = await asyncio.wait({task}, timeout=30)
            if task in done:
                try:
                    task.result()
                except BaseException:
                    pass

    async def _call_worker(self, request, prompt, directory, *, session_id=None, timeout=None):
        if self.abort_event.is_set():
            raise RoleTurnCancelled('PACT invocation cancelled before role start')
        task = asyncio.create_task(self.worker.run(
            stage=f'pact_{self.semantic_role}', prompt=prompt,
            workspace=request.workdir, output_dir=directory,
            read_only=self.semantic_role != 'worker',
            timeout_seconds=timeout if timeout is not None else request.timeout_seconds,
            session_id=session_id,
        ))
        self.active_task = task
        try:
            return await task
        finally:
            if self.active_task is task:
                self.active_task = None

    def _normalize(self, prompt: str, final_text: str) -> str:
        final_text = _protocol_payload(final_text)
        if not _is_json_object(final_text):
            # Do not let legacy semantic helpers' json.loads collapse duplicate
            # keys or repair an ambiguous transport into an accepted decision.
            return final_text
        if self.semantic_role == 'planner':
            final_text = _preserve_repair_semantics(prompt, final_text)
            # Governance roles execute in a PACT scratch directory, so read
            # the canonical campaign workspace rather than that scratch dir.
            final_text = _restore_existing_missions(self.workspace, final_text)
        elif self.semantic_role == 'manager':
            final_text = _replan_instead_of_retry(prompt, final_text)
        return final_text

    def _record_call(self, records, directory, result, *, requested_session_id, correction_kind):
        """Record observed per-call output and usage; never infer provider calls."""
        raw = getattr(result, 'final_text', '')
        if isinstance(raw, str):
            (directory / 'native-final-raw.txt').write_text(raw, encoding='utf-8')
        events = getattr(result, 'events', [])
        request_ids = sorted({event['request_id'] for event in events
                              if isinstance(event, dict) and isinstance(event.get('request_id'), str)})
        event_file = directory / 'events.jsonl'
        records.append({
            'attempt': len(records) + 1, 'correction_kind': correction_kind,
            'session_id': getattr(result, 'session_id', None),
            'requested_session_id': requested_session_id,
            'request_ids': request_ids,
            'output_dir': str(directory.relative_to(self.output_dir)),
            'status': getattr(result, 'status', 'unavailable'),
            'usage': getattr(result, 'usage', {}) or {},
            'native_final_sha256': hashlib.sha256(raw.encode()).hexdigest() if isinstance(raw, str) else None,
            'native_events_sha256': hashlib.sha256(event_file.read_bytes()).hexdigest() if event_file.is_file() else None,
        })

    def _write_call_chain(self, directory, records, *, correction_attempted, status):
        cumulative = {}
        for record in records:
            usage = record['usage']
            if not isinstance(usage, dict):
                continue
            for name, value in usage.items():
                if type(value) is int or (type(value) is float and math.isfinite(value)):
                    cumulative[name] = cumulative.get(name, 0) + value
        (directory / 'role-call-chain.json').write_text(json.dumps({
            'schema': 'jiuwenswarm/pact-role-call-chain/v1',
            'semantic_role': self.semantic_role, 'campaign_id': self.campaign_id,
            'correction_attempted': correction_attempted, 'status': status,
            'calls': records, 'cumulative_usage': cumulative,
            'usage_scope': 'sum_of_observed_current_call_worker_results',
        }, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')

    async def _run_turn(self, request, sequence):
        from pact_core.review_decision import parse_review_decision
        from pact_core.schemas import ReviewDecisionCapture, TurnResult
        started = time.monotonic()
        directory = self.output_dir / 'roles' / self.semantic_role / f'{sequence:04d}'
        directory.mkdir(parents=True, exist_ok=False)
        prompt = request.prompt
        if self.semantic_role in _ROLE_PROTOCOL_REMINDERS:
            prompt += '\n\n' + _ROLE_PROTOCOL_REMINDERS[self.semantic_role]
        if self.semantic_role in ('worker', 'reviewer'):
            context = await asyncio.to_thread(_campaign_context, self.workspace, self.semantic_role)
            if self.context_dir is not None:
                names = ['requirement.md', 'baseline.md']
                if self.semantic_role == 'reviewer':
                    names += ['clarification.md', 'goal-contract.json']
                sections = []
                for name in names:
                    path = self.context_dir / name
                    if path.is_file() and not path.is_symlink():
                        authority = ('The original requirement is authoritative; preserve exact public names and behavior.'
                                     if name == 'requirement.md' else
                                     'Clarification is subordinate guidance: do not add acceptance criteria absent from the original requirement.'
                                     if name == 'clarification.md' else
                                     'Read this accepted campaign input together with the original requirement.')
                        sections.append(f'## Accepted campaign input: {name}\n\n{authority}\n\n' + path.read_text())
                if sections:
                    context += '\n\n' + '\n\n'.join(sections)
            if context:
                prompt += '\n\n' + context
            prompt += (
                f'\n\nAssigned candidate checkout: {request.workdir.resolve()}\n'
                'Treat requirement repository paths as relative to this checkout. '
                'Run probes here and verify imported module paths belong here. '
                'The baseline implementation is already present: read it first and preserve what satisfies the requirement. '
                'The candidate may be a clean commit: an empty diff does not mean work is absent. '
                'Do not edit the primary checkout, reset to an older base, or change shared Git refs. '
                'The controller owns promotion to the primary workspace.'
            )
        if self.semantic_role == 'reviewer':
            prompt += '\n\n' + _REVIEW_PROMPT
        final_text = ''
        diagnostic = ''
        result = None
        session_id = None
        status = 'spawn_failed'
        budget_detail = None
        review_context = None
        call_records = []
        active_directory = directory
        requested_session_id = None
        correction_attempted = False
        correction_kind = None
        try:
            if self.semantic_role == 'reviewer':
                review_context = await asyncio.to_thread(
                    prepare_review_context, request.workdir, request.prompt,
                    self.output_dir / 'review-evidence' / f'{sequence:04d}',
                )
                if review_context is not None:
                    prompt += review_context.prompt
            result = await self._call_worker(request, prompt, directory)
            self._record_call(call_records, directory, result,
                              requested_session_id=None, correction_kind=None)
            session_id = result.session_id
            status = result.status
            final_text = result.final_text
            if status == 'completed' and not final_text.strip():
                status = 'output_missing'
            if status in ('completed', 'output_missing') and self.semantic_role in ('planner', 'manager'):
                final_text = self._normalize(prompt, final_text)
                (directory / 'protocol-payload.txt').write_text(final_text, encoding='utf-8')
                if not _is_json_object(final_text):
                    # The original PACT adapter permits exactly one transport
                    # correction in the same role session, never a fresh replay.
                    remaining = request.timeout_seconds - (time.monotonic() - started)
                    if remaining <= 0:
                        status = 'budget_exhausted'
                        budget_detail = 'Role deadline exhausted before JSON format correction'
                    elif session_id:
                        correction_attempted = True
                        correction_kind = 'format_schema'
                        requested_session_id = session_id
                        active_directory = directory.with_name(directory.name + '-format-repair')
                        active_directory.mkdir(parents=True, exist_ok=False)
                        result = await self._call_worker(
                            request, _JSON_FOLLOWUP_PROMPT, active_directory,
                            session_id=requested_session_id,
                            timeout=min(remaining, _JSON_FOLLOWUP_TIMEOUT_SECONDS),
                        )
                        self._record_call(call_records, active_directory, result,
                                          requested_session_id=requested_session_id,
                                          correction_kind=correction_kind)
                        if result.session_id != requested_session_id:
                            raise ValueError('Native format correction changed session identity')
                        session_id = result.session_id
                        status = result.status
                        final_text = result.final_text
                        if status == 'completed':
                            final_text = self._normalize(prompt, final_text)
                            (active_directory / 'protocol-payload.txt').write_text(final_text, encoding='utf-8')
                    if status in ('completed', 'output_missing') and not _is_json_object(final_text):
                        status = 'output_missing'
                        diagnostic = 'Role final response did not contain one unambiguous complete JSON object'
                    (directory / 'protocol-output.json').write_text(json.dumps({
                        'schema': 'jiuwenswarm/pact-role-protocol-output/v1',
                        'status': status, 'semantic_role': self.semantic_role,
                        'correction_attempted': correction_attempted,
                        'continuation': 'same_native_session' if correction_attempted else 'not_started',
                        'session_id': session_id,
                        'paths_relative_to': str(directory.parent.relative_to(self.output_dir)),
                        'initial_payload_path': str((directory / 'protocol-payload.txt').relative_to(directory.parent)),
                        'final_payload_path': str((active_directory / 'protocol-payload.txt').relative_to(directory.parent)),
                    }, indent=2) + '\n')
        except asyncio.CancelledError:
            status = 'cancelled'
            recovery = await asyncio.to_thread(
                preserve_role_budget, campaign_id=self.campaign_id, role=self.semantic_role,
                workdir=request.workdir, source_workspace=self.workspace, session_id=session_id,
                artifact_dir=request.artifact_dir, output_dir=self.output_dir,
                detail='Outer invocation cancelled; native task drained before candidate capture.',
                stop_status='interrupted',
            )
            raise RoleTurnCancelled(str(recovery)) from None
        except RoleTurnCancelled:
            status = 'cancelled'
            raise
        except Exception as error:
            result = getattr(error, 'result', None)
            session_id = getattr(result, 'session_id', None) or session_id
            status = getattr(result, 'status', 'spawn_failed')
            diagnostic = f'{type(error).__name__}: {error}'[-4000:]
            if result is not None:
                self._record_call(call_records, active_directory, result,
                                  requested_session_id=requested_session_id, correction_kind=correction_kind)
            # Typed worker exceptions have already saved process/session data.
            if ('BudgetExhausted' in type(error).__name__ or type(error).__name__ == 'BudgetStop'
                    or isinstance(error, TimeoutError) or 'Timeout' in type(error).__name__
                    or getattr(error, 'code', '') in {'BUDGET_EXHAUSTED', 'OUTPUT_BUDGET_EXHAUSTED', 'TIMEOUT'}):
                budget_detail = diagnostic
                status = 'budget_exhausted'
        finally:
            self._write_call_chain(directory, call_records, correction_attempted=correction_attempted, status=status)
            await asyncio.to_thread(cleanup_review_context, review_context)
        if status in ('budget_exhausted', 'timed_out', 'timeout') or budget_detail:
            suspension = await asyncio.to_thread(
                preserve_role_budget, campaign_id=self.campaign_id,
                role=self.semantic_role, workdir=request.workdir,
                source_workspace=self.workspace, session_id=session_id,
                artifact_dir=request.artifact_dir, output_dir=self.output_dir,
                detail=budget_detail or diagnostic or status,
            )
            self.abort_event.set()
            raise suspension
        if status == 'cancelled' or self.abort_event.is_set():
            recovery = await asyncio.to_thread(
                preserve_role_budget, campaign_id=self.campaign_id, role=self.semantic_role,
                workdir=request.workdir, source_workspace=self.workspace, session_id=session_id,
                artifact_dir=request.artifact_dir, output_dir=self.output_dir,
                detail=diagnostic or 'Native role was interrupted.', stop_status='interrupted',
            )
            raise RoleTurnCancelled(str(recovery))
        if status not in ('completed', 'output_missing', 'timeout', 'spawn_failed'):
            status = 'spawn_failed'
        if status == 'completed' and not final_text.strip():
            status = 'output_missing'
        capture = ReviewDecisionCapture(verdict_status='not_applicable',
                                        plan_challenge_status='not_applicable')
        if self.semantic_role == 'reviewer':
            raw = _protocol_payload(final_text)
            (directory / 'reviewer-decision-raw.txt').write_text(raw, encoding='utf-8')
            if request.artifact_dir is not None:
                request.artifact_dir.mkdir(parents=True, exist_ok=True)
                (request.artifact_dir / 'reviewer-decision-raw.txt').write_text(raw, encoding='utf-8')
            if _is_json_object(raw):
                capture = parse_review_decision(raw, existed=bool(raw.strip()))
            else:
                capture = ReviewDecisionCapture(
                    verdict_status='malformed' if raw.strip() else 'missing',
                    plan_challenge_status='unavailable', raw_text=raw,
                    error='Reviewer final response did not contain one unambiguous complete JSON object',
                )
            if status != 'completed' and capture.verdict_status == 'valid':
                capture = ReviewDecisionCapture(
                    verdict_status='malformed', plan_challenge_status='unavailable', raw_text=raw,
                    error=f'Decision is not credible because turn status was {status}',
                )
        return TurnResult(
            status=status, final_text=final_text if status == 'completed' else '',
            output_source='jiuwenswarm_native', exit_code=0 if status == 'completed' else None,
            stderr_tail=diagnostic, session_id=session_id,
            duration_seconds=time.monotonic() - started, review_decision=capture,
        )
