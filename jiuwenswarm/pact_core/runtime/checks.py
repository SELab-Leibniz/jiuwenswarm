"""Host-registered, candidate-bound checking methods; never user requirements."""
from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath
import subprocess
from typing import cast

from .schemas import RuntimeContractError
from .store import data_digest


def normalize_checks(workspace: Path, checkpoint: str, proposals: list[dict[str, object]],
                     previous: list[dict[str, object]], reason: str) -> list[dict[str, object]]:
    if not isinstance(proposals, list) or not proposals:
        raise RuntimeContractError('checks must be a nonempty list')
    result = {str(item['name']): dict(item) for item in previous}
    names: set[str] = set()
    for proposal in proposals:
        if not isinstance(proposal, dict) or set(proposal) - {'name', 'command', 'requirement', 'files'}:
            raise RuntimeContractError('check proposals contain only name, command, requirement and files')
        for key in ('name', 'command', 'requirement'):
            if not isinstance(proposal.get(key), str) or not str(proposal[key]).strip():
                raise RuntimeContractError(f'check {key} must be nonempty')
        name = str(proposal['name'])
        if name in names:
            raise RuntimeContractError('duplicate check proposal name')
        names.add(name)
        files = proposal.get('files', [])
        if not isinstance(files, list) or any(not isinstance(path, str) for path in files):
            raise RuntimeContractError('check files must be relative paths')
        hashes: dict[str, str] = {}
        for filename in cast(list[str], files):
            path = PurePosixPath(filename)
            if (not filename or path.is_absolute() or '..' in path.parts or '\\' in filename
                    or not path.parts or any(part.lower() in {'.pact', '.pact-io', '.git'} for part in path.parts)):
                raise RuntimeContractError('check files must be non-reserved relative paths')
            listing = subprocess.run(['git', '-C', str(workspace), 'ls-tree', checkpoint, '--', filename],
                stdin=subprocess.DEVNULL, check=True, capture_output=True, timeout=30).stdout
            if not listing.startswith((b'100644 blob ', b'100755 blob ')):
                raise RuntimeContractError('check file must be a regular file in the saved candidate')
            content = subprocess.run(['git', '-C', str(workspace), 'show', f'{checkpoint}:{filename}'],
                stdin=subprocess.DEVNULL, check=True, capture_output=True, timeout=30).stdout
            hashes[filename] = hashlib.sha256(content).hexdigest()
        content_record: dict[str, object] = {'name': name, 'command': proposal['command'],
            'requirement': proposal['requirement'], 'files': hashes}
        fingerprint = data_digest(content_record)
        old = result.get(name)
        if old and old['fingerprint'] == fingerprint:
            continue
        result[name] = {**content_record, 'id': data_digest(name)[:20],
            'version': int(cast(int, old['version'])) + 1 if old else 1,
            'fingerprint': fingerprint, 'source': 'derived', 'reason': reason,
            'registered_candidate': checkpoint, 'disposition': 'pending', 'method_review': None}
    return list(result.values())


def active_checks(checks: list[dict[str, object]]) -> list[dict[str, object]]:
    """Pending methods are trialled; suspended derived methods are retained as history."""
    return [item for item in checks if item['disposition'] != 'suspended']


def normalize_decisions(checks: list[dict[str, object]], decisions: list[dict[str, object]],
                        review_ref: str) -> list[dict[str, object]]:
    if not isinstance(decisions, list) or not decisions:
        raise RuntimeContractError('check decisions must be a nonempty list')
    result = {str(item['name']): dict(item) for item in checks}
    seen: set[str] = set()
    for decision in decisions:
        if not isinstance(decision, dict) or set(decision) != {'name', 'version', 'action', 'reason'}:
            raise RuntimeContractError('check decisions require name, version, action and reason')
        name = decision['name']
        if not isinstance(name, str) or name not in result or name in seen:
            raise RuntimeContractError('check decisions must reference distinct registered derived checks')
        seen.add(name)
        item = result[name]
        if type(decision['version']) is not int or decision['version'] != item['version']:
            raise RuntimeContractError('check decision targets a stale definition version')
        if decision['action'] not in ('admit', 'suspend'):
            raise RuntimeContractError('check decision action must be admit or suspend')
        if not isinstance(decision['reason'], str) or not decision['reason'].strip():
            raise RuntimeContractError('check disposition needs an evidence-based reason')
        item.update(disposition='admitted' if decision['action'] == 'admit' else 'suspended',
            method_review={'review_ref': review_ref, 'reason': decision['reason']})
    return list(result.values())
