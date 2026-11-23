"""Native Jiuwen role and configuration boundary for Core's benchmark driver.

Core owns candidate state, checks, review cadence, acceptance and delivery.
This boundary executes only the pinned Jiuwen worker, never a Chrys model loop.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pact_core.review_decision import parse_review_decision
from pact_core.schemas import AdapterProbe, ReviewDecisionCapture, TurnResult

from jiuwenswarm.lh.review_transport import acceptance_review_text


_CAPACITY_SCALE_ENV = "LONG_HORIZON_CAPACITY_SCALE"


def _capacity_setting() -> str | None:
    raw = os.environ.get(_CAPACITY_SCALE_ENV)
    if raw is None:
        return None
    raw = raw.strip()
    return raw or None


def _parse_capacity(raw: str) -> int:
    try:
        value = int(raw, 10)
    except (TypeError, ValueError):
        raise ValueError(f"{_CAPACITY_SCALE_ENV} must be a positive integer") from None
    if isinstance(value, bool) or not 1 <= value <= 10_000:
        raise ValueError(f"{_CAPACITY_SCALE_ENV} must be an integer from 1 to 10000")
    return value


def _capacity() -> tuple[int, bool]:
    """Return the controller's scale and whether the opt-in setting is present.

    The second element preserves the legacy execution fingerprint when the
    setting is absent: no capacity keys are added and no value is injected into
    a role environment.  An explicit value, including 1, is part of the
    fingerprint and is carried into every role turn.
    """
    raw = _capacity_setting()
    if raw is None:
        return 1, False
    return _parse_capacity(raw), True


def _capacity_scale() -> int:
    return _capacity()[0]


def _scaled(value: int) -> int:
    return max(1, int(value) * _capacity_scale())


@dataclass(frozen=True)
class NativeExecution:
    worker: Any
    profile_name: str
    fingerprint: str
    summary: dict

    @classmethod
    def from_worker(cls, worker, profile_name, capacity=None):
        scale, explicit = _capacity() if capacity is None else capacity
        summary = {'schema': 'jiuwenswarm-pact/execution-configuration/v1',
                   'profile_name': profile_name, 'source_commit': worker.source_commit,
                   'router_enabled': False}
        if explicit:
            summary['capacity_setting'] = _CAPACITY_SCALE_ENV
            summary['capacity_scale'] = scale
        summary['benchmark_workspace_policy'] = getattr(worker, 'benchmark_workspace_policy', None)
        for name in ('binary', 'config_template'):
            path = Path(getattr(worker, name))
            summary[name + '_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        fingerprint = hashlib.sha256(json.dumps(summary, sort_keys=True).encode()).hexdigest()
        return cls(worker, profile_name, fingerprint, summary)


class NativeBenchmarkAdapter:
    def __init__(self, *, semantic_role, worker, outer_loop, invocation_id,
                 output_dir, abort_event, expected_fingerprint,
                 executable_reviewer=False, reviewer_launch_policy=None):
        if semantic_role not in {'worker', 'reviewer'}:
            raise ValueError('benchmark supports Worker and Reviewer only')
        self.semantic_role = semantic_role
        self.id = 'jiuwenswarm-benchmark-' + semantic_role
        self.worker, self.outer_loop = worker, outer_loop
        run = getattr(worker, 'run', None)
        parameters = inspect.signature(run).parameters if run is not None else {}
        self._worker_accepts_environment = (
            'environment' in parameters
            or any(param.kind is inspect.Parameter.VAR_KEYWORD for param in parameters.values())
        )
        self.invocation_id, self.output_dir = invocation_id, Path(output_dir)
        self.abort_event, self.expected_fingerprint = abort_event, expected_fingerprint
        # One controller reading governs both the fingerprinted summary and the
        # environment every role turn consumes.
        self._capacity = _capacity()
        self.executable_reviewer = bool(executable_reviewer)
        self.reviewer_launch_policy = reviewer_launch_policy if semantic_role == 'reviewer' else None
        self.sequence = 0
        self.active_task = None
        self.configuration_failure = None
        self.budget_failure = None

    def probe(self):
        return AdapterProbe(adapter_id=self.id, available=True, version=self.worker.source_commit)

    def run_turn(self, request):
        if self.abort_event.is_set():
            return TurnResult(status='cancelled', final_text='', output_source='jiuwenswarm_native', exit_code=None)
        try:
            from jiuwenswarm.lh.turn_environment import current_environment
        except ImportError:
            environment = dict(os.environ)
        else:
            environment = current_environment()
        # A caller may hand us a deliberately small environment.  The adapter's
        # one controller reading is authoritative: a different value already in
        # the turn snapshot is a configuration failure, not a silent override.
        environment = dict(environment)
        scale, explicit = self._capacity
        requested = environment.get(_CAPACITY_SCALE_ENV)
        requested = str(requested).strip() if requested is not None else None
        if requested:
            try:
                turn_scale = _parse_capacity(requested)
            except ValueError:
                self.configuration_failure = 'native capacity scale is invalid'
                return TurnResult(status='spawn_failed', final_text='', output_source='jiuwenswarm_native',
                                  exit_code=None, stderr_tail='invalid capacity scale')
            if turn_scale != scale:
                self.configuration_failure = 'native capacity scale changed'
                return TurnResult(status='spawn_failed', final_text='', output_source='jiuwenswarm_native',
                                  exit_code=None, stderr_tail='inconsistent capacity scale')
        if explicit:
            environment[_CAPACITY_SCALE_ENV] = str(scale)
        else:
            environment.pop(_CAPACITY_SCALE_ENV, None)
        return asyncio.run_coroutine_threadsafe(self._turn(request, environment), self.outer_loop).result()

    async def cancel_current_turn(self):
        self.abort_event.set()
        if self.active_task is not None:
            self.active_task.cancel()
            await asyncio.gather(self.active_task, return_exceptions=True)

    async def _turn(self, request, environment=None):
        started = time.monotonic()
        if self.abort_event.is_set():
            return TurnResult(status='cancelled', final_text='', output_source='jiuwenswarm_native', exit_code=None)
        actual = NativeExecution.from_worker(self.worker, 'Code', self._capacity).fingerprint
        if actual != self.expected_fingerprint:
            self.configuration_failure = 'native execution configuration changed'
            return TurnResult(status='spawn_failed', final_text='', output_source='jiuwenswarm_native', exit_code=None)
        if self.semantic_role == 'reviewer' and not self._worker_accepts_environment:
            self.configuration_failure = 'reviewer_environment_unsupported'
            return TurnResult(status='spawn_failed', final_text='', output_source='jiuwenswarm_native',
                              exit_code=None, stderr_tail='reviewer_environment_unsupported')
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
                       'decision; run and write only inside the disposable candidate copy. Do not weaken '
                       'the original task or supplied checks. Internal acceptance is separate from '
                       'independent grading.')
        timeout = request.timeout_seconds
        if request.deadline is not None:
            remaining = request.deadline - time.time()
            if remaining <= 0:
                # An expired admitted window is a refusal, never a 0.001s call.
                self.budget_failure = 'deadline reached before native worker run'
                return TurnResult(status='timeout', final_text='', output_source='jiuwenswarm_native',
                                  exit_code=None, stderr_tail='DeadlineExpired',
                                  duration_seconds=time.monotonic()-started)
            timeout = min(timeout, remaining)
        status, text, diagnostic, native = 'spawn_failed', '', '', None
        try:
            launch = ({'reviewer_launch_policy': self.reviewer_launch_policy}
                      if self.reviewer_launch_policy is not None and self.reviewer_launch_policy.mode == 'uid_drop'
                      else {})
            if self._worker_accepts_environment:
                launch['environment'] = environment
            self.active_task = asyncio.create_task(self.worker.run(
                stage='benchmark-' + self.semantic_role, prompt=prompt, workspace=request.workdir,
                output_dir=directory,
                read_only=self.semantic_role == 'reviewer' and not self.executable_reviewer,
                timeout_seconds=timeout, **launch))
            native = await self.active_task
            status = 'completed' if native.status == 'completed' and native.exit_code == 0 else native.status
            text = native.final_text
        except asyncio.CancelledError:
            status = 'cancelled'
        except Exception as exc:
            if self.reviewer_launch_policy is not None and type(exc).__name__ == 'ReviewerProtectionError':
                self.reviewer_launch_policy.enforcement_error = str(exc)
            native = getattr(exc, 'result', None)
            diagnostic = type(exc).__name__
            if 'Budget' in diagnostic or 'Timeout' in diagnostic:
                status = 'timeout'
                self.budget_failure = diagnostic
            if 'Configuration' in diagnostic or 'Protocol' in diagnostic:
                self.configuration_failure = diagnostic
        finally:
            self.active_task = None
            if self.reviewer_launch_policy is not None and self.reviewer_launch_policy.mode == 'uid_drop':
                await asyncio.to_thread(shutil.rmtree, directory / 'state', ignore_errors=True)
        if status not in {'completed', 'timeout', 'cancelled', 'output_missing', 'spawn_failed'}:
            status = 'spawn_failed'
        capture = ReviewDecisionCapture(verdict_status='not_applicable', plan_challenge_status='not_applicable')
        final_text = acceptance_review_text(text) if acceptance else text
        if acceptance:
            capture = parse_review_decision(final_text, existed=bool(final_text))
            if status != 'completed':
                capture = ReviewDecisionCapture(verdict_status='malformed', raw_text=text,
                    error='native acceptance review did not complete')
            if request.artifact_dir is not None:
                request.artifact_dir.mkdir(parents=True, exist_ok=True)
                (request.artifact_dir / 'reviewer-decision-raw.txt').write_text(text)
        return TurnResult(status=status, final_text=final_text if status == 'completed' else '',
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
