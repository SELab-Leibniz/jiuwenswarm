"""Native Jiuwen role and configuration boundary for Core's benchmark driver.

Core owns candidate state, checks, review cadence, acceptance and delivery.
This boundary executes only the pinned Jiuwen worker, never a Chrys model loop.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import time
from typing import Any

from pact_core.schemas import AdapterProbe, ReviewDecisionCapture, TurnResult
from pact_core.review_decision import parse_review_decision


@dataclass(frozen=True)
class NativeExecution:
    worker: Any
    profile_name: str
    fingerprint: str
    summary: dict

    @classmethod
    def from_worker(cls, worker, profile_name):
        summary = {'schema': 'jiuwenswarm-pact/execution-configuration/v1',
                   'profile_name': profile_name, 'source_commit': worker.source_commit,
                   'router_enabled': False}
        summary['benchmark_workspace_policy'] = getattr(worker, 'benchmark_workspace_policy', None)
        for name in ('binary', 'config_template'):
            path = Path(getattr(worker, name))
            summary[name + '_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        fingerprint = hashlib.sha256(json.dumps(summary, sort_keys=True).encode()).hexdigest()
        return cls(worker, profile_name, fingerprint, summary)


class NativeBenchmarkAdapter:
    def __init__(self, *, semantic_role, worker, outer_loop, invocation_id,
                 output_dir, abort_event, expected_fingerprint):
        if semantic_role not in {'worker', 'reviewer'}:
            raise ValueError('benchmark supports Worker and Reviewer only')
        self.semantic_role = semantic_role
        self.id = 'jiuwenswarm-benchmark-' + semantic_role
        self.worker, self.outer_loop = worker, outer_loop
        self.invocation_id, self.output_dir = invocation_id, Path(output_dir)
        self.abort_event, self.expected_fingerprint = abort_event, expected_fingerprint
        self.sequence = 0
        self.active_task = None
        self.configuration_failure = None
        self.budget_failure = None

    def probe(self):
        return AdapterProbe(adapter_id=self.id, available=True, version=self.worker.source_commit)

    def run_turn(self, request):
        return asyncio.run_coroutine_threadsafe(self._turn(request), self.outer_loop).result()

    async def cancel_current_turn(self):
        self.abort_event.set()
        if self.active_task is not None:
            self.active_task.cancel()
            await asyncio.gather(self.active_task, return_exceptions=True)

    async def _turn(self, request):
        started = time.monotonic()
        if self.abort_event.is_set():
            return TurnResult(status='cancelled', final_text='', output_source='jiuwenswarm_native', exit_code=None)
        actual = NativeExecution.from_worker(self.worker, 'Code').fingerprint
        if actual != self.expected_fingerprint:
            self.configuration_failure = 'native execution configuration changed'
            return TurnResult(status='spawn_failed', final_text='', output_source='jiuwenswarm_native', exit_code=None)
        self.sequence += 1
        directory = self.output_dir / self.semantic_role / f'{self.sequence:04d}'
        prompt = request.prompt + (
            '\nDo not reset shared refs or source HEAD. '
            'Never write, copy, sync, install, or rebuild candidate changes into the original source checkout, '
            'even to reuse its compiled extensions or test environment. If imports resolve there, '
            'rebuild or install in a candidate-local isolated directory and recheck module __file__ paths. '
            'Keep original source Git and submodule metadata unchanged; do not copy .git files or '
            'directories between checkouts. Final delivery belongs to the controller.')
        acceptance = self.semantic_role == 'reviewer' and request.review_mode == 'acceptance'
        if acceptance:
            prompt += ('\nNative transport: finish with exactly one JSON object containing verdict '
                       '(complete, continue, failed), optional reason, and optional plan_challenge. '
                       'Put the review evidence in reason and tool observations. The host captures the '
                       'decision; your source tools remain read-only. Do not weaken the original task or '
                       'supplied checks. Internal acceptance is separate from independent grading.')
        timeout = request.timeout_seconds
        if request.deadline is not None:
            timeout = min(timeout, max(0.001, request.deadline - time.time()))
        status, text, diagnostic, native = 'spawn_failed', '', '', None
        try:
            self.active_task = asyncio.create_task(self.worker.run(
                stage='benchmark-' + self.semantic_role, prompt=prompt, workspace=request.workdir,
                output_dir=directory, read_only=self.semantic_role == 'reviewer', timeout_seconds=timeout))
            native = await self.active_task
            status = 'completed' if native.status == 'completed' and native.exit_code == 0 else native.status
            text = native.final_text
        except asyncio.CancelledError:
            status = 'cancelled'
        except Exception as exc:
            native = getattr(exc, 'result', None)
            diagnostic = type(exc).__name__
            if 'Budget' in diagnostic or 'Timeout' in diagnostic:
                status = 'timeout'
                self.budget_failure = diagnostic
            if 'Configuration' in diagnostic or 'Protocol' in diagnostic:
                self.configuration_failure = diagnostic
        finally:
            self.active_task = None
        if status not in {'completed', 'timeout', 'cancelled', 'output_missing', 'spawn_failed'}:
            status = 'spawn_failed'
        capture = ReviewDecisionCapture(verdict_status='not_applicable', plan_challenge_status='not_applicable')
        if acceptance:
            capture = parse_review_decision(text, existed=bool(text))
            if status != 'completed':
                capture = ReviewDecisionCapture(verdict_status='malformed', raw_text=text,
                    error='native acceptance review did not complete')
            if request.artifact_dir is not None:
                request.artifact_dir.mkdir(parents=True, exist_ok=True)
                (request.artifact_dir / 'reviewer-decision-raw.txt').write_text(text)
        return TurnResult(status=status, final_text=text if status == 'completed' else '',
            output_source='jiuwenswarm_native', exit_code=0 if status == 'completed' else None,
            stderr_tail=diagnostic, duration_seconds=time.monotonic()-started,
            session_id=getattr(native, 'session_id', None), review_decision=capture)


def record_work_budget(*, workspace, work_id, revision, detail, artifacts):
    from pact_core.artifacts import write_json_atomic
    target = Path(workspace) / '.pact-io' / 'jiuwenswarm-budget-suspensions' / (work_id + '.json')
    write_json_atomic(target, {'schema': 'jiuwenswarm-pact/work-budget/v1', 'work_id': work_id,
        'revision': revision, 'detail': detail, 'artifacts': str(artifacts),
        'next_action': 'Inspect the original trial ledger and retained candidate; no automatic replay.'})
    return str(target)
