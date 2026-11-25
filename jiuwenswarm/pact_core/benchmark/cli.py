"""CLI binding for the optional benchmark driver, not a separate engine."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
from typing import cast
import uuid

from ..adapters import create_adapter
from ..artifacts import read_json, write_json_atomic
from .driver import BenchDriver
from .budget import BenchBudget
from .types import BenchConfig, BenchResult
from ..checkpoint import PactGitError
from ..kernel import KernelContractError
from ..runtime import AcceptancePolicy, WorkRuntime
from ..runtime.export import output_directory
from ..kernel import default_worktree_root
from ..runtime.schemas import RuntimeContractError, RuntimeStateError
from ..runtime.text_plan import read_text_plan
from ..runtime.types import WorkSnapshot


def _path(value: object, root: Path, label: str) -> Path:
    if not isinstance(value, (str, Path)) or not str(value).strip():
        raise RuntimeContractError(f'{label} must be a path')
    path = Path(value).expanduser()
    return (path if path.is_absolute() else root / path).resolve()


def _read_configuration(path: Path) -> tuple[BenchConfig, Path, str | None]:
    value = read_json(path)
    if value.pop('schema', None) != 'pact-core/benchmark-config/v1':
        raise RuntimeContractError('unsupported benchmark configuration schema')
    submission = _path(value.pop('submission_dir', None), path.parent, 'submission_dir')
    brief = value.pop('acceptance_brief', None)
    if brief is not None and (not isinstance(brief, str) or not brief.strip()):
        raise RuntimeContractError('acceptance_brief must be nonempty text')
    return BenchConfig.parse(value), submission, brief


def _create(runtime: WorkRuntime, args: argparse.Namespace) -> tuple[WorkSnapshot, BenchConfig, Path]:
    if not args.plan or not args.benchmark_config:
        raise RuntimeContractError('benchmark creation requires --plan and --benchmark-config')
    if args.contract is not None:
        raise RuntimeContractError('benchmark --plan is the original task text; a generated Initial Plan is not required')
    if args.final_verify:
        raise RuntimeContractError('benchmark hidden grading stays external; use public --verify/--closeout-verify only')
    if args.final_verify_timeout is not None or args.max_unchanged_rounds is not None:
        raise RuntimeContractError('benchmark does not use --final-verify-timeout or --max-unchanged-rounds')
    path = _path(args.benchmark_config, runtime.workspace, 'benchmark configuration')
    config, submission, brief = _read_configuration(path)
    output_directory(submission / 'preflight-export',
                     (runtime.workspace, runtime.worktree_root or default_worktree_root()))
    payload = config.payload()
    # Existing explicit knobs have the same meaning as in ordinary run.
    if args.review_interval is not None:
        payload['review_interval'] = args.review_interval
    if args.review_each_round and args.execution_policy == 'continuous':
        raise RuntimeContractError('conflicting review policy options')
    if args.review_each_round or args.execution_policy == 'review_each_round':
        payload['review_each_round'] = True
    if args.turn_timeout is not None:
        payload['turn_timeout_seconds'] = args.turn_timeout
    config = BenchConfig.parse(payload)
    contract, _ = read_text_plan(_path(args.plan, runtime.workspace, 'task'), workspace=runtime.workspace)
    policy = AcceptancePolicy(verify_command=args.verify, closeout_verify_command=args.closeout_verify,
        allow_unverified=bool(args.allow_unverified),
        verify_timeout_seconds=1800.0 if args.verify_timeout is None else args.verify_timeout)
    max_turns = 3 if args.max_rounds is None else args.max_rounds
    if max_turns < 1:
        raise RuntimeContractError('--max-rounds must be positive')
    if args.campaign_id and args.loop_id and args.campaign_id != args.loop_id:
        raise RuntimeContractError('conflicting --campaign-id and --loop-id')
    metadata: dict[str, object] = {'profile': 'benchmark', 'benchmark_configuration': config.payload(),
        'submission_dir': str(submission), 'benchmark_max_turns': max_turns}
    if brief:
        metadata['input_acceptance_brief'] = brief
    state = runtime.create_work(request_id=args.request_id or f'benchmark:{config.task_id}:{config.trial_id}',
        work_id=args.campaign_id or args.loop_id, contract=contract, policy=policy, advisory=metadata)
    if state.advisory.get('benchmark_configuration') != config.payload():
        raise RuntimeContractError('existing request uses a different benchmark configuration')
    if state.advisory.get('submission_dir') != str(submission):
        raise RuntimeContractError('existing trial uses a different submission directory')
    if state.advisory.get('input_acceptance_brief') != brief:
        raise RuntimeContractError('existing request uses a different input acceptance brief')
    if brief and not state.acceptance_brief:
        state = runtime.set_acceptance_brief(state.work_id, expected_revision=state.revision,
            text=brief, source='supplied acceptance brief')
    return state, config, submission


def run_benchmark_command(args: argparse.Namespace) -> int:
    action = args.campaign_command
    runtime = WorkRuntime(args.workspace, worktree_root=getattr(args, 'worktree_root', None))
    continuing = action in {'advance', 'recover'} or bool(getattr(args, 'campaign', None))
    if continuing:
        if action == 'run' and any(getattr(args, flag, None) is not None for flag in (
                'plan', 'contract', 'benchmark_config', 'verify', 'closeout_verify', 'final_verify',
                'allow_unverified', 'request_id', 'campaign_id', 'loop_id', 'max_rounds',
                'review_interval', 'review_each_round', 'execution_policy', 'turn_timeout', 'verify_timeout',
                'final_verify_timeout', 'max_unchanged_rounds')):
            raise RuntimeContractError('benchmark continuation uses persisted inputs and budget')
        state = runtime.read_work(args.campaign)
        config = BenchConfig.parse(cast(dict[str, object], state.advisory['benchmark_configuration']))
        submission = Path(str(state.advisory['submission_dir']))
    else:
        if action != 'create':
            # Reject unsupported adapters before allocating canonical work.
            create_adapter(args.worker_adapter, role='worker', max_rounds=1)
            create_adapter(args.reviewer_adapter, role='reviewer', max_rounds=1)
        state, config, submission = _create(runtime, args)
    if getattr(args, 'expected_revision', None) is not None and state.revision != args.expected_revision:
        raise RuntimeStateError('stale expected work revision')
    if action == 'recover':
        state = runtime.recover(state.work_id, expected_revision=args.expected_revision)
        print(json.dumps({'work_id': state.work_id, 'revision': state.revision, 'status': state.status}))
        return 0
    if action == 'create':
        print(json.dumps({'work_id': state.work_id, 'revision': state.revision, 'status': state.status}))
        return 0
    max_turns = 1 if action == 'advance' else cast(int, state.advisory.get('benchmark_max_turns', 3))
    worker = create_adapter(args.worker_adapter, role='worker', max_rounds=max_turns + 1)
    reviewer = create_adapter(args.reviewer_adapter, role='reviewer', max_rounds=max_turns + 3)
    failure: str | None = None
    try:
        result = BenchDriver(runtime, worker, reviewer, config=config).run(
            state.work_id, max_turns=max_turns, expected_revision=state.revision)
    except (RuntimeContractError, RuntimeStateError, KernelContractError, PactGitError, OSError) as exc:
        # Never replay or clear an unknown executor. A sealed prior candidate is
        # still useful to the evaluator; a valid export cannot turn this error
        # into a successful run. Corrupt canonical state remains a hard error.
        recovered = runtime.read_work(state.work_id)
        budget = BenchBudget(runtime.workspace / '.pact' / 'bench' / 'trials', config, state.work_id)
        failure = f'{type(exc).__name__}: {exc}'
        result = BenchResult(recovered, 'runtime_error', budget.snapshot(), budget.trial_dir)
    exported = runtime.export_latest_candidate(result.snapshot.work_id,
        submission / f'{state.work_id}-{uuid.uuid4().hex}', expected_revision=result.snapshot.revision)
    receipt = exported.receipt
    diagnostics = [f'{item.candidate.checkpoint}: {item.code}: {item.detail}' for item in exported.diagnostics]
    report: dict[str, object] = {'schema': 'pact-core/benchmark-result/v1',
        'task_id': config.task_id, 'trial_id': config.trial_id, 'work_id': state.work_id,
        'stop_reason': result.stop_reason, 'status': result.snapshot.status,
        'submission': str(receipt.artifact) if receipt else None, 'export_diagnostics': diagnostics, 'error': failure,
        'internal_accepted': result.snapshot.acceptance is not None,
        'delivery': asdict(result.snapshot.delivery) if result.snapshot.delivery else None,
        'public_verification': asdict(result.snapshot.verification) if result.snapshot.verification else None,
        'budget': result.budget, 'external_grade': {'status': 'not_run', 'reward': None}}
    # A report is driver output, never a Core completion receipt.
    normalized = json.loads(json.dumps(report, default=str))
    write_json_atomic(result.trial_dir / 'result.json', normalized)
    print(json.dumps(normalized, ensure_ascii=False, indent=2))
    return 2 if failure is not None else 0 if receipt is not None else 1
