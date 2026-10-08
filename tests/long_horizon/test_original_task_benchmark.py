"""Real new PACT control plane with synthetic native roles; no paid model calls."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import subprocess
from unittest.mock import patch

import pytest
pytest.importorskip('pact_core.benchmark')
from jiuwenswarm.long_horizon.pact.long_horizon_benchmark import BenchmarkArguments, LongHorizonBenchmark
from jiuwenswarm.long_horizon.clarification.types import ClarificationResult, ClarificationSelection


class NativeFixture:
    source_commit = 'a' * 40
    def __init__(self, root):
        self.binary = root / 'fixture-worker'
        self.config_template = root / 'config.yaml'
        self.binary.write_text('fixture')
        self.config_template.write_text('fixture: true')
        self.calls = []
        self.requests = []
    async def run(self, *, stage, prompt, workspace, output_dir, read_only, timeout_seconds, **continuation):
        self.calls.append((stage, workspace, read_only))
        self.requests.append(continuation)
        output_dir.mkdir(parents=True)
        if not read_only:
            (workspace / 'result.txt').write_text('done\n')
        return SimpleNamespace(status='completed', exit_code=0, final_text='A candidate is ready.',
                               session_id=continuation.get('session_id') or ('native-' + str(len(self.calls))))


async def clarification(self, *, revision, background, snapshot):
    return ClarificationResult(strategy_version='legacy-v1-stabilized', revision=1, delta='',
        selection=ClarificationSelection(), status='completed', empty_reason='requirement_complete')


@pytest.mark.parametrize("worker_session", ["new", "resume"])
def test_real_core_original_task_p0_clarification_and_sealed_latest_candidate(tmp_path, worker_session):
    repo = tmp_path / 'repo';repo.mkdir()
    subprocess.run(['git','init','-q',str(repo)],check=True)
    subprocess.run(['git','-C',str(repo),'config','user.name','Fixture'],check=True)
    subprocess.run(['git','-C',str(repo),'config','user.email','fixture@localhost'],check=True)
    (repo / 'README.md').write_text('fixture\n')
    subprocess.run(['git','-C',str(repo),'add','.'],check=True)
    subprocess.run(['git','-C',str(repo),'commit','-qm','base'],check=True)
    prompt = tmp_path / 'task.md';prompt.write_text('Create result.txt containing done.\n')
    out = tmp_path / 'output'
    worker = NativeFixture(tmp_path)
    args = BenchmarkArguments(task=prompt, workdir=repo, output=out, task_id='fixture',trial_id='trial1',
        budget_seconds=60,max_role_calls=20,max_turns=2,turn_timeout_seconds=10,
        worktree_root=tmp_path / 'worktrees', worker_session=worker_session)
    with patch('jiuwenswarm.long_horizon.pact.long_horizon_benchmark.ClarificationService.clarify', clarification):
        outcome=asyncio.run(LongHorizonBenchmark(args,worker=worker).run())
    data=json.loads((out/'selection.json').read_text())
    assert not outcome.internal_failure, data.get('internal_failure')
    assert data['schema']=='jiuwenswarm-pact/benchmark-result/v1'
    assert data['workflow']=='long-horizon-benchmark'
    assert data['original_task_sha256']
    assert data['long_horizon']['p0_ran_this_invocation'] is True
    assert data['long_horizon']['phase']=='first_review_done'
    assert data['export_status']=='exported'
    assert data['external_grade']['status']=='not_run'
    assert 'goal-contract' not in str(list(out.rglob('*')))
    assert not (repo/'result.txt').exists()
    assert len([c for c in worker.calls if c[0]=='benchmark-worker'])==2
    assert all(ro for stage,workdir,ro in worker.calls if stage=='benchmark-reviewer')
    assert all(workdir != repo for stage,workdir,ro in worker.calls)
    from pact_core.runtime.export import verify_export
    assert Path(data['export']['patch']).read_text()
    turns=[(call,request) for call,request in zip(worker.calls,worker.requests) if call[0]=='benchmark-worker']
    if worker_session=='resume':
        assert turns[0][1]['session_id'] is None
        assert turns[1][1]['session_id']=='native-1'
        assert turns[0][0][1]==turns[1][0][1]
    else:
        assert all('session_id' not in request for _,request in turns)
    assert all(request.get('session_id') is None for call,request in zip(worker.calls,worker.requests)
               if call[0]=='benchmark-reviewer')
    if worker_session == 'resume':
        from dataclasses import replace
        from pact_core.runtime import WorkRuntime
        state=WorkRuntime(repo,worktree_root=args.worktree_root).read_work(outcome.work_id)
        ledger=repo/'.pact/bench/trials/trial1/budget.json'
        before_budget=json.loads(ledger.read_text())
        fresh=NativeFixture(tmp_path)
        resumed=replace(args,work_id=state.work_id,expected_revision=state.revision)
        with patch('jiuwenswarm.long_horizon.pact.long_horizon_benchmark.ClarificationService.clarify', clarification):
            second=asyncio.run(LongHorizonBenchmark(resumed,worker=fresh).run())
        assert not second.internal_failure, json.loads((out/'selection.json').read_text()).get('internal_failure')
        requests=[request for call,request in zip(fresh.calls,fresh.requests) if call[0]=='benchmark-worker']
        assert requests and requests[0]['session_id']=='native-1'
        after_budget=json.loads(ledger.read_text())
        assert after_budget['started_at']==before_budget['started_at']
        assert after_budget['deadline']==before_budget['deadline']
        assert len(after_budget['calls'])>len(before_budget['calls'])



def test_execution_fingerprint_binds_real_launcher_and_configuration_bytes(tmp_path):
    from jiuwenswarm.long_horizon.pact.benchmark_native import NativeExecution
    worker=NativeFixture(tmp_path)
    first=NativeExecution.from_worker(worker,'Code').fingerprint
    worker.config_template.write_text('fixture: changed')
    assert NativeExecution.from_worker(worker,'Code').fingerprint != first


@pytest.mark.asyncio
async def test_every_native_clarification_turn_has_one_finished_trial_admission(tmp_path):
    from jiuwenswarm.long_horizon.clarification.model import JiuwenClarificationModel
    from jiuwenswarm.long_horizon.clarification.budget import SideCallTicket
    from unittest.mock import AsyncMock
    model=object.__new__(JiuwenClarificationModel)
    ticket=SideCallTicket('real-ticket',1)
    calls=[]
    class Admission:
        def admit(self,stage):calls.append(('admit',stage));return ticket
        def finish(self,actual,*,status,detail=''):calls.append(('finish',actual.token,status))
    model.side_call_admission=Admission()
    model._call_unbudgeted=AsyncMock(return_value='native')
    assert await model._call('proposal','prompt')=='native'
    assert calls==[('admit','proposal'),('finish','real-ticket','completed')]
    model._call_unbudgeted=AsyncMock(side_effect=ValueError('fixture'))
    with pytest.raises(ValueError):await model._call('selector','prompt')
    assert calls[-1]==('finish','real-ticket','error')


@pytest.mark.asyncio
async def test_refused_side_call_never_invokes_native_worker():
    from jiuwenswarm.long_horizon.clarification.model import JiuwenClarificationModel
    from jiuwenswarm.long_horizon.clarification.budget import SideCallBudgetExhausted
    from unittest.mock import AsyncMock
    model=object.__new__(JiuwenClarificationModel)
    class Admission:
        def admit(self,stage):raise SideCallBudgetExhausted('fixed trial expired')
        def finish(self,*a,**k):raise AssertionError('no admission')
    model.side_call_admission=Admission();model._call_unbudgeted=AsyncMock()
    with pytest.raises(SideCallBudgetExhausted):await model._call('proposal','prompt')
    model._call_unbudgeted.assert_not_called()
