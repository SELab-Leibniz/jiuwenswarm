"""Save an unfinished Jiuwen role before PACT disposes of its candidate.

The suspension contract follows Chrys 6fcfb3321892735631b49f33236b1e2a988ded2d
(src/chrys/pact/budget.py); snapshotting here uses only Python and Git.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

LATCH = Path('.pact-io/jiuwenswarm-budget-suspension.json')


class RoleBudgetExhausted(RuntimeError):
    """Budget suspension is neither a failed round nor a task verdict."""

    def __init__(self, message: str, *, receipt_path: Path | None = None):
        super().__init__(message)
        self.receipt_path = receipt_path


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + '\n')
    temporary.chmod(0o600)
    temporary.replace(path)


def require_no_pending_suspension(workspace: Path) -> None:
    receipt = workspace / LATCH
    if receipt.exists():
        raise RoleBudgetExhausted(
            f'Pending PACT budget suspension: {receipt}; inspect recovery before starting another campaign.',
            receipt_path=receipt,
        )


def record_campaign_suspension(workspace: Path, error: RoleBudgetExhausted) -> None:
    _write_json(workspace / LATCH, {
        'schema': 'jiuwenswarm/pact-budget-suspension/v1',
        'status': 'budget_exhausted', 'detail': str(error),
        'receipt_path': str(error.receipt_path) if error.receipt_path else None,
    })


def _git(workdir: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(['git', '-C', str(workdir), *args], capture_output=True, check=check, timeout=60)


def preserve_role_budget(*, campaign_id: str, role: str, workdir: Path,
                         source_workspace: Path, session_id: str | None,
                         artifact_dir: Path | None, output_dir: Path, detail: str,
                         stop_status: str = 'budget_exhausted') -> RoleBudgetExhausted:
    """Copy candidate bytes and bundle all refs plus detached HEAD, without changing refs."""
    if stop_status not in ('budget_exhausted', 'interrupted'):
        raise ValueError('Unsupported role recovery status')
    root = output_dir / ('budget-suspensions' if stop_status == 'budget_exhausted' else 'interruptions') / uuid4().hex
    root.mkdir(parents=True, mode=0o700)
    receipt = root / ('budget-exhausted.json' if stop_status == 'budget_exhausted' else 'interrupted.json')
    record = {
        'schema': ('jiuwenswarm/pact-role-budget/v1' if stop_status == 'budget_exhausted'
                   else 'jiuwenswarm/pact-role-interruption/v1'), 'status': stop_status,
        'campaign_id': campaign_id, 'role': role, 'workdir': str(workdir),
        'session_id': session_id, 'artifact_dir': str(artifact_dir) if artifact_dir else None,
        'detail': detail, 'recovery_status': 'capturing',
        'next_action': 'Inspect the saved candidate and session; continue remaining work without a new attempt.',
    }
    _write_json(receipt, record)
    try:
        # Following links could escape the candidate or copy the host runtime.
        shutil.copytree(workdir, root / 'workspace', symlinks=True,
                        ignore=shutil.ignore_patterns('.git'))
        repos = [('candidate', workdir)]
        if workdir.resolve() != source_workspace.resolve():
            repos.append(('primary', source_workspace))
        git_records = {}
        for name, repository in repos:
            probe = _git(repository, 'rev-parse', '--verify', 'HEAD', check=False)
            if probe.returncode:
                git_records[name] = {'status': 'not_a_git_repository'}
                continue
            git_dir = root / name
            git_dir.mkdir()
            refs = _git(repository, 'show-ref', '--head', check=False)
            (git_dir / 'refs.txt').write_bytes(refs.stdout)
            (git_dir / 'status.txt').write_bytes(_git(repository, 'status', '--porcelain=v1', '--untracked-files=all').stdout)
            (git_dir / 'working.patch').write_bytes(_git(repository, 'diff', '--binary', 'HEAD').stdout)
            bundle = git_dir / 'history.bundle'
            _git(repository, 'bundle', 'create', str(bundle), '--all', 'HEAD')
            _git(repository, 'bundle', 'verify', str(bundle))
            git_records[name] = {'status': 'saved', 'head': probe.stdout.decode().strip(),
                                 'bundle': str(bundle.relative_to(root)),
                                 'bundle_sha256': hashlib.sha256(bundle.read_bytes()).hexdigest()}
        manifest = []
        for item in sorted((root / 'workspace').rglob('*')):
            if item.is_symlink():
                manifest.append({'path': str(item.relative_to(root)), 'symlink': str(item.readlink())})
            elif item.is_file():
                manifest.append({'path': str(item.relative_to(root)),
                                 'sha256': hashlib.sha256(item.read_bytes()).hexdigest()})
        _write_json(root / 'manifest.json', {'files': manifest, 'git': git_records})
        record.update(recovery_status='saved', workspace_manifest=str(root / 'manifest.json'),
                      workspace_manifest_sha256=hashlib.sha256((root / 'manifest.json').read_bytes()).hexdigest())
    except Exception as error:
        record.update(recovery_status='capture_failed', capture_error=f'{type(error).__name__}: {error}')
    _write_json(receipt, record)
    return RoleBudgetExhausted(
        f"PACT {stop_status}; role={role}; recovery={record['recovery_status']}; checkpoint={receipt}",
        receipt_path=receipt,
    )
