"""Real Git/PACT control-plane checks with scripted native turns, no model calls."""
import asyncio
import hashlib
import json
import re
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from pact_core.schemas import TurnRequest
from jiuwenswarm.long_horizon.pact.budget import RoleBudgetExhausted, require_no_pending_suspension
from jiuwenswarm.long_horizon.pact.campaign import run_campaign, _valid_receipt
from jiuwenswarm.long_horizon.pact.public_verification import bind_public_verification, command_from_clarification
from jiuwenswarm.long_horizon.pact.protocol import _protocol_payload, _preserve_repair_semantics, _replan_instead_of_retry
from jiuwenswarm.long_horizon.pact.role_runner import JiuwenRoleAdapter
from jiuwenswarm.long_horizon.pact.verify_shim import link_ignored_dependencies, wrap_verify_command


def git(path, *args):
    return subprocess.run(['git', '-C', str(path), *args], capture_output=True, text=True, check=True).stdout.strip()


class ScriptedWorker:
    def __init__(self, *, public_pass=True, budget=False):
        self.calls = []
        self.public_pass = public_pass
        self.budget = budget

    async def run(self, **kwargs):
        self.calls.append(kwargs)
        role = kwargs['stage']
        text = 'Implementation finished.'
        status = 'completed'
        if role == 'pact_manager':
            match = re.search(r'<revision-bound-input>\s*(.*?)\s*</revision-bound-input>', kwargs['prompt'], re.S)
            projection = json.loads(match.group(1))
            eligible = projection['work_state']['frontier']['eligible']
            decision = {'schema': 'pact-runtime/manager-decision-proposal/v1', 'phase': 'route',
                        'expected_plan_revision': projection['plan_revision'],
                        'expected_work_state_revision': projection['work_state_revision'],
                        'action': 'select' if eligible and projection['trigger'] == 'frontier_selection' else 'block',
                        'reason': 'Scripted deterministic decision.'}
            if decision['action'] == 'select':
                decision['selected_mission_id'] = eligible[0]
            text = json.dumps(decision)
        elif role == 'pact_worker':
            (kwargs['workspace'] / 'answer.txt').write_text('done\n' if self.public_pass else 'wrong\n')
            if self.budget:
                (kwargs['workspace'] / 'unfinished.txt').write_text('partial\n')
                status = 'budget_exhausted'
        elif role == 'pact_reviewer':
            text = '{"verdict":"complete"}'
        return SimpleNamespace(final_text=text, status=status, events=[], session_id='native-test', usage={}, exit_code=0)


class PactTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        git(self.workspace, 'init')
        git(self.workspace, 'config', 'user.name', 'Test')
        git(self.workspace, 'config', 'user.email', 'test@example.invalid')
        (self.workspace / '.gitignore').write_text('.pact/\n.pact-io/\n')
        (self.workspace / 'README').write_text('Public task repository\n')
        git(self.workspace, 'add', '.')
        git(self.workspace, 'commit', '-m', 'base')
        self.inputs = self.root / 'inputs'
        self.inputs.mkdir()
        goal = {'schema': 'pact-runtime/goal-contract/v1', 'goal': 'Write answer',
                'acceptance_criteria': [{'id': 'AC1', 'text': 'answer.txt says done'}], 'non_goals': []}
        plan = {'schema': 'pact-runtime/initial-plan/v1', 'constraints': [], 'missions': [
            {'id': 'M1', 'objective': 'Write answer', 'target_ac_ids': ['AC1'], 'dependencies': [],
             'verification_intent': 'Read answer.txt'}]}
        verification = {'schema': 'jiuwenswarm/pact-public-verification/v1', 'status': 'ready',
                        'reason': 'Public file contract', 'checks': [
                            {'id': 'C1', 'command': 'test "$(cat answer.txt)" = done',
                             'target_ac_ids': ['AC1'], 'rationale': 'Assert public behavior'}]}
        self.goal = self.inputs / 'goal-contract.json'
        self.plan = self.inputs / 'initial-plan.json'
        self.spec = self.inputs / 'public-verification.json'
        self.goal.write_text(json.dumps(goal))
        self.plan.write_text(json.dumps(plan))
        self.spec.write_text(json.dumps(bind_public_verification(goal, plan, verification)))
        self.output = self.root / 'suite' / 'pact'

    def tearDown(self):
        self.temp.cleanup()

    async def campaign(self, worker, **kwargs):
        return await run_campaign(worker, self.workspace, self.goal, self.plan, self.spec, self.output,
                                  max_rounds=1, role_timeout_seconds=60, **kwargs)

    async def test_real_campaign_promotes_only_after_public_pass(self):
        worker = ScriptedWorker()
        result = await self.campaign(worker)
        self.assertEqual(result['status'], 'completed', worker.calls)
        self.assertEqual((self.workspace / 'answer.txt').read_text(), 'done\n')
        evidence = result['public_verification']
        self.assertEqual(evidence['status'], 'executed')
        self.assertTrue(evidence['all_passed'])
        self.assertEqual(evidence['spec_sha256'], hashlib.sha256(self.spec.read_bytes()).hexdigest())
        receipt = json.loads((self.output.parent / evidence['receipt_paths'][0]).read_text())
        self.assertIsNotNone(_valid_receipt(receipt, evidence['spec_sha256']))
        self.assertTrue(next(call for call in worker.calls if call['stage'] == 'pact_reviewer')['read_only'])
        self.assertNotIn('resolved', result)

    async def test_reviewer_complete_does_not_override_failed_public_check(self):
        result = await self.campaign(ScriptedWorker(public_pass=False))
        self.assertNotEqual(result['status'], 'completed')
        self.assertEqual(result['public_verification']['status'], 'executed')
        self.assertFalse(result['public_verification']['all_passed'])
        self.assertFalse((self.workspace / 'answer.txt').exists())

    async def test_budget_preserves_candidate_and_all_git_refs_before_cleanup(self):
        worker = ScriptedWorker(budget=True)
        with self.assertRaises(RoleBudgetExhausted) as raised:
            await self.campaign(worker)
        receipt = json.loads(raised.exception.receipt_path.read_text())
        self.assertEqual(receipt['recovery_status'], 'saved', receipt)
        saved = raised.exception.receipt_path.parent
        self.assertEqual((saved / 'workspace' / 'unfinished.txt').read_text(), 'partial\n')
        self.assertTrue((saved / 'candidate' / 'history.bundle').is_file())
        self.assertTrue((saved / 'primary' / 'history.bundle').is_file())
        with self.assertRaises(RoleBudgetExhausted):
            require_no_pending_suspension(self.workspace)
        self.assertEqual([c['stage'] for c in worker.calls if c['stage'] != 'pact_manager'], ['pact_worker'])

    async def test_semantic_role_binding_and_no_fresh_session_replay(self):
        class Worker:
            calls = []
            async def run(self, **kwargs):
                self.calls.append(kwargs)
                return SimpleNamespace(status='completed', final_text='no json', session_id='s1')
        worker = Worker()
        adapter = JiuwenRoleAdapter(semantic_role='manager', worker=worker, outer_loop=asyncio.get_running_loop(),
                                   campaign_id='test', workspace=self.workspace, output_dir=self.output,
                                   abort_event=threading.Event())
        request = TurnRequest(role='worker', prompt='repeated_no_progress', workdir=self.workspace, timeout_seconds=60)
        result = await asyncio.to_thread(adapter.run_turn, request)
        self.assertEqual(result.final_text, 'no json')
        self.assertEqual(len(worker.calls), 1)
        self.assertTrue(worker.calls[0]['read_only'])
        self.assertEqual(worker.calls[0]['stage'], 'pact_manager')
        self.assertTrue((self.output / 'roles/manager/0001/protocol-output.json').exists())
        with self.assertRaises(ValueError):
            adapter.run_turn(TurnRequest(role='reviewer', prompt='', workdir=self.workspace, timeout_seconds=1))
        normalized = adapter._normalize(request.prompt, '{"action":"retry","reason":"need progress"}')
        self.assertEqual(json.loads(normalized)['action'], 'request_replan')

    def test_public_goal_plan_binding_rejects_changed_plan(self):
        changed = json.loads(self.plan.read_text())
        changed['constraints'] = ['Changed after verification generation']
        self.plan.write_text(json.dumps(changed))
        with self.assertRaisesRegex(ValueError, 'different PACT inputs'):
            command_from_clarification(self.goal, self.plan, workspace=self.workspace, campaign_id='bad')

    def test_protocol_repair_preserves_planner_decision(self):
        original = {'reason': 'original', 'missions': [{'id': 'M1'}], 'operations': []}
        prompt = 'Structured Output Repair\n<invalid-output>' + json.dumps(original) + '</invalid-output>'
        fixed = _preserve_repair_semantics(prompt, '{"reason":"changed","missions":[],"operations":[1]}')
        self.assertEqual(json.loads(fixed)['missions'], original['missions'])
        self.assertEqual(json.loads(fixed)['operations'], [1])
        self.assertEqual(json.loads(_protocol_payload('prose\n```json\n{"action":"block"}\n```'))['action'], 'block')

    def test_receipt_rejects_model_claims_and_zero_executions(self):
        self.assertIsNone(_valid_receipt({'status': 'passed'}, 'abc'))
        payload = {'schema': 'pact-core/verification/v1', 'status': 'passed', 'exit_code': 0,
                   'checks': [{'name': 'command_status_line', 'passed': True, 'detail': json.dumps({
                       'schema': 'jiuwenswarm/pact-public-verification-result/v1', 'status': 'passed',
                       'spec_sha256': 'abc', 'planned_checks': 1, 'executed_checks': 0})}]}
        self.assertIsNone(_valid_receipt(payload, 'abc'))

    def test_verify_worktree_links_ignored_dependencies_but_not_pact_state(self):
        with (self.workspace / '.gitignore').open('a') as stream:
            stream.write('node_modules/\n')
        git(self.workspace, 'add', '.gitignore')
        git(self.workspace, 'commit', '-m', 'ignore public dependency cache')
        (self.workspace / 'node_modules').mkdir()
        (self.workspace / 'node_modules' / 'fixture').write_text('installed')
        (self.workspace / '.pact').mkdir()
        (self.workspace / '.pact' / 'state').write_text('controller only')
        candidate = self.root / 'detached'
        git(self.workspace, 'worktree', 'add', '--detach', str(candidate), 'HEAD')
        linked = link_ignored_dependencies(candidate)
        self.assertEqual(linked, [candidate / 'node_modules'])
        self.assertEqual((candidate / 'node_modules' / 'fixture').read_text(), 'installed')
        self.assertFalse((candidate / '.pact').exists())
        self.assertEqual(link_ignored_dependencies(candidate), [])
        command = wrap_verify_command('test -f node_modules/fixture')
        checked = subprocess.run(command, shell=True, cwd=candidate, capture_output=True)
        self.assertEqual(checked.returncode, 0, checked.stderr)

    async def test_outer_cancellation_drains_active_worker(self):
        started, drained = asyncio.Event(), asyncio.Event()
        class BlockingWorker(ScriptedWorker):
            async def run(inner, **kwargs):
                if kwargs['stage'] != 'pact_worker':
                    return await super().run(**kwargs)
                (kwargs['workspace'] / 'cancelled-work.txt').write_text('unfinished candidate')
                started.set()
                try:
                    await asyncio.Future()
                finally:
                    drained.set()
        task = asyncio.create_task(self.campaign(BlockingWorker()))
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(drained.is_set())
        self.assertFalse((self.output / 'result.json').exists())
        receipts = list((self.output / 'interruptions').glob('*/interrupted.json'))
        self.assertEqual(len(receipts), 1)
        self.assertEqual(json.loads(receipts[0].read_text())['status'], 'interrupted')
        self.assertEqual((receipts[0].parent / 'workspace/cancelled-work.txt').read_text(), 'unfinished candidate')

    async def test_native_signal_is_interrupted_and_preserves_candidate(self):
        class InterruptedWorker(ScriptedWorker):
            async def run(inner, **kwargs):
                result = await super().run(**kwargs)
                if kwargs['stage'] == 'pact_worker':
                    result.status = 'cancelled'
                return result
        with self.assertRaisesRegex(RuntimeError, 'interrupted') as raised:
            await self.campaign(InterruptedWorker())
        self.assertEqual(raised.exception.code, 'INTERRUPTED')
        receipts = list((self.output / 'interruptions').glob('*/interrupted.json'))
        self.assertEqual(len(receipts), 1)
        self.assertEqual(json.loads(receipts[0].read_text())['recovery_status'], 'saved')
        self.assertEqual((receipts[0].parent / 'workspace/answer.txt').read_text(), 'done\n')
        self.assertEqual(json.loads((self.output / 'failure.json').read_text())['status'], 'interrupted')
