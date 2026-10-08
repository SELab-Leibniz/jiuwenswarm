"""Run pinned pact-core with native Jiuwen semantic role adapters.

The native role transport derives from Chrys
6fcfb3321892735631b49f33236b1e2a988ded2d:src/chrys/pact/campaign.py.
Only pact-core owns canonical Work State, gates, checkpoints and promotion.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import threading
import uuid
from pathlib import Path
from typing import Any

from .budget import RoleBudgetExhausted, record_campaign_suspension, require_no_pending_suspension
from .role_runner import JiuwenRoleAdapter, RoleTurnCancelled, SEMANTIC_ROLES

RESULT_SCHEMA = 'jiuwenswarm/pact-campaign-result/v1'
RECEIPT_SCHEMA = 'jiuwenswarm/pact-public-verification-result/v1'


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def _valid_receipt(payload: dict, expected_sha256: str) -> dict | None:
    """Check the PACT command result, not model assertions or arbitrary stdout."""
    if payload.get('schema') != 'pact-core/verification/v1':
        return None
    for check in payload.get('checks', []):
        if not isinstance(check, dict) or check.get('name') != 'command_status_line':
            continue
        try:
            receipt = json.loads(check.get('detail', ''))
        except (TypeError, ValueError):
            continue
        if not isinstance(receipt, dict) or receipt.get('schema') != RECEIPT_SCHEMA:
            continue
        if receipt.get('spec_sha256') != expected_sha256:
            continue
        planned, executed = receipt.get('planned_checks'), receipt.get('executed_checks')
        if (type(planned) is not int or type(executed) is not int or
                not 0 < executed <= planned):
            continue
        status = receipt.get('status')
        if status == 'passed':
            if (executed != planned or payload.get('status') != 'passed' or
                    type(payload.get('exit_code')) is not int or payload['exit_code'] != 0 or
                    check.get('passed') is not True):
                continue
        elif status == 'failed':
            if (payload.get('status') != 'failed' or type(payload.get('exit_code')) is not int or
                    payload['exit_code'] == 0 or check.get('passed') is not False):
                continue
        else:
            continue
        return receipt
    return None


def archive_public_evidence(*, workspace: Path, output_dir: Path,
                            accepted_spec: bytes, loop_ids: set[str]) -> dict:
    directory = output_dir / 'public-verification'
    directory.mkdir(parents=True, exist_ok=True)
    spec_path = directory / 'accepted-public-verification.json'
    spec_path.write_bytes(accepted_spec)
    digest = hashlib.sha256(accepted_spec).hexdigest()
    paths = []
    receipts = []
    for loop_id in sorted(loop_ids):
        for source in sorted((workspace / '.pact' / 'loops' / loop_id).glob('round-*-verification.json')):
            if source.is_symlink():
                continue
            destination = directory / 'receipts' / loop_id / source.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            try:
                payload = json.loads(destination.read_text())
            except (OSError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            receipt = _valid_receipt(payload, digest)
            if receipt is not None:
                paths.append(str(destination.relative_to(output_dir.parent)))
                receipts.append(receipt)
    return {
        'status': 'executed' if receipts else 'unavailable',
        'spec_path': str(spec_path.relative_to(output_dir.parent)),
        'archived_spec_path': str(spec_path.relative_to(output_dir.parent)),
        'spec_sha256': digest, 'receipt_paths': paths,
        'all_passed': bool(receipts) and all(r['status'] == 'passed' for r in receipts),
        'executed_receipts': len(receipts),
    }


async def run_campaign(worker: Any, workspace: Path, contract_path: Path,
                       plan_path: Path, public_verification_path: Path,
                       output_dir: Path, max_rounds: int = 3,
                       role_timeout_seconds: float = 10800, *,
                       control_plane: Any = None, worker_session: str = "new") -> dict:
    """Run fresh Work through the public Runtime and DefaultDriver APIs.

    ``output_dir`` is the suite's ``pact`` directory outside the candidate
    workspace. Returned public evidence paths are relative to its parent.
    This function has no independent benchmark grader and never sets resolved.
    """
    from pact_core.runtime import AcceptancePolicy, WorkRuntime
    from pact_core.runtime.configuration import DriverConfiguration
    from pact_core.runtime.driver import DefaultDriver
    from pact_core.runtime.planning import initial_plan_payload, validate_initial_plan
    from pact_core.runtime.schemas import goal_contract_criteria, validate_goal_contract
    from .public_verification import command_from_clarification
    workspace, output_dir = Path(workspace).resolve(strict=True), Path(output_dir).resolve()
    if output_dir == workspace or workspace in output_dir.parents:
        raise ValueError('Campaign output_dir must be outside the candidate workspace')
    if max_rounds < 1 or role_timeout_seconds <= 0:
        raise ValueError('Campaign bounds must be positive')
    require_no_pending_suspension(workspace)
    output_dir.mkdir(parents=True, exist_ok=True)
    campaign_id = f'jiuwenswarm-pact-{uuid.uuid4().hex}'
    accepted_spec = Path(public_verification_path).read_bytes()
    admitted = output_dir / 'admitted-inputs'
    admitted.mkdir(exist_ok=False)
    # Keep exactly the accepted input bytes away from candidate edits.
    admitted_contract, admitted_plan = admitted / 'goal-contract.json', admitted / 'initial-plan.json'
    admitted_contract.write_bytes(Path(contract_path).read_bytes())
    admitted_plan.write_bytes(Path(plan_path).read_bytes())
    (admitted / 'public-verification.json').write_bytes(accepted_spec)
    verify_command = command_from_clarification(
        admitted_contract, admitted_plan, workspace=workspace, campaign_id=campaign_id,
    )
    event_loop = asyncio.get_running_loop()
    abort = threading.Event()
    adapters = {
        role: JiuwenRoleAdapter(semantic_role=role, worker=worker, outer_loop=event_loop,
                               campaign_id=campaign_id, workspace=workspace,
                               output_dir=output_dir, abort_event=abort,
                               context_dir=Path(contract_path).parent, worker_session=worker_session)
        for role in SEMANTIC_ROLES
    }
    runtime = control_plane or WorkRuntime(workspace, worktree_root=output_dir / 'worktrees')
    contract = validate_goal_contract(json.loads(admitted_contract.read_text()))
    plan = validate_initial_plan(json.loads(admitted_plan.read_text()),
        acceptance_criterion_ids=tuple(row['id'] for row in goal_contract_criteria(contract)))
    configuration = DriverConfiguration(max_rounds=max_rounds, turn_timeout_seconds=role_timeout_seconds, worker_session=worker_session)

    def run_current_work():
        state = runtime.create_work(request_id=campaign_id, work_id=campaign_id,
            contract=contract, policy=AcceptancePolicy(verify_command=verify_command),
            constraints=plan.constraints, worker_probe=adapters["worker"].probe(),
            advisory={'plan': initial_plan_payload(plan), 'driver_configuration': configuration.payload()})
        driver = DefaultDriver(runtime, adapters['worker'], adapters['reviewer'])
        state = driver.run(state.work_id, expected_revision=state.revision, max_turns=max_rounds,
                           stop_requested=abort.is_set)
        # A bounded implementation window ends with independent whole-work review
        # when public checks pass. Only Core acceptance can authorize delivery.
        progress = state.advisory.get('default_driver', {})
        if (state.status == 'active' and state.verification is not None
                and state.verification.status == 'passed'
                and progress.get('worker_status') == 'completed' and not abort.is_set()):
            state = runtime.request_review(state.work_id, expected_revision=state.revision,
                reason='Review the candidate after the bounded implementation window.')
            state = driver.run(state.work_id, expected_revision=state.revision, max_turns=1,
                               stop_requested=abort.is_set)
        return state
    prior_loops = {path.name for path in (workspace / '.pact' / 'loops').glob('*') if path.is_dir()}
    completion = event_loop.create_future()
    def settled(result, error):
        if completion.done():
            return
        if error is None:
            completion.set_result(result)
        else:
            completion.set_exception(error)
    def run():
        try:
            result = run_current_work()
        except BaseException as error:
            event_loop.call_soon_threadsafe(settled, None, error)
        else:
            event_loop.call_soon_threadsafe(settled, result, None)
    thread = threading.Thread(target=run, name=campaign_id, daemon=True)
    thread.start()
    result = None
    failure = None
    try:
        result = await asyncio.shield(completion)
    except RoleBudgetExhausted as error:
        record_campaign_suspension(workspace, error)
        failure = error
        raise
    except asyncio.CancelledError:
        abort.set()
        await asyncio.gather(*(adapter.cancel_current_turn() for adapter in adapters.values()))
        # Owned role tasks have drained. A verifier subprocess is owned by
        # pact-core and obeys its separate verification timeout; do not fake
        # a canonical transition while that control thread is still settling.
        await asyncio.to_thread(thread.join, 30)
        if not completion.done():
            completion.add_done_callback(lambda future: None if future.cancelled() else future.exception())
        elif not completion.cancelled():
            completion.exception()
        raise
    except BaseException as error:
        failure = error
        raise
    finally:
        current_loops = {path.name for path in (workspace / '.pact' / 'loops').glob('*') if path.is_dir()}
        evidence = archive_public_evidence(workspace=workspace, output_dir=output_dir,
                                           accepted_spec=accepted_spec, loop_ids=current_loops - prior_loops)
        # Preserve canonical artifacts even when the role budget stops before
        # the control plane can produce a CampaignRunResult.
        campaign_dir = workspace / '.pact' / 'runtime' / 'campaigns' / campaign_id
        if campaign_dir.is_dir():
            shutil.copytree(campaign_dir, output_dir / 'campaign', dirs_exist_ok=True, symlinks=True)
        original_path = Path(public_verification_path).resolve()
        if original_path.is_relative_to(output_dir.parent):
            evidence['original_spec_path'] = str(original_path.relative_to(output_dir.parent))
        evidence['original_spec_sha256'] = hashlib.sha256(accepted_spec).hexdigest()
        _write_json(output_dir / 'public-verification-summary.json', evidence)
        if failure is not None:
            _write_json(output_dir / 'failure.json', {
                'schema': RESULT_SCHEMA, 'campaign_id': campaign_id,
                'status': ('budget_exhausted' if isinstance(failure, RoleBudgetExhausted) else
                           'interrupted' if isinstance(failure, RoleTurnCancelled) else 'failed'),
                'error': f'{type(failure).__name__}: {failure}', 'public_verification': evidence,
            })
    terminal = {
        'schema': RESULT_SCHEMA, 'status': result.status,
        'campaign_id': result.work_id, 'revision': result.revision,
        'next_action': 'none' if result.status == 'completed' else 'continue_work',
        'artifact_ref': str((output_dir / 'campaign').relative_to(output_dir.parent)),
        'public_verification': evidence,
    }
    _write_json(output_dir / 'result.json', terminal)
    return terminal
