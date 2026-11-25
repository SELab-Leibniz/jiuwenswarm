"""One immutable fact history and an atomic current pointer for each work."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from ..artifacts import read_json as _read_json, write_json_atomic
from .schemas import (
    CAMPAIGN_REVISION_SCHEMA, EXECUTION_EVIDENCE_SCHEMA, LATEST_CAMPAIGN_SCHEMA,
    WORK_STATE_SCHEMA, RuntimeStateError, StaleWorkStateError, validate_goal_contract,
)
from .types import AcceptancePolicy


@dataclass(frozen=True, slots=True)
class CampaignPaths:
    runtime_root: Path
    campaign_dir: Path
    current_state: Path
    state_revisions: Path
    evidence_dir: Path
    latest: Path


def build_campaign_paths(workspace: Path, campaign_id: str) -> CampaignPaths:
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', campaign_id):
        raise RuntimeStateError('invalid work identifier')
    root = workspace.expanduser().resolve() / '.pact' / 'runtime'
    work = root / 'campaigns' / campaign_id
    return CampaignPaths(root, work, work / 'work-state.json', work / 'work-state-revisions',
                         work / 'evidence', root / 'latest.json')


def read_json(path: Path) -> dict[str, object]:
    try:
        return _read_json(path)
    except (OSError, ValueError, UnicodeError) as exc:
        raise RuntimeStateError('unreadable Runtime artifact') from exc


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def data_digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def now() -> str:
    return datetime.now(UTC).isoformat()


class CampaignReader:
    """Read-side compatibility name; no workflow or write authority lives here."""
    def __init__(self, workspace: Path, campaign_id: str) -> None:
        self.workspace = workspace.expanduser().resolve()
        self.campaign_id = campaign_id
        self.paths = build_campaign_paths(self.workspace, campaign_id)

    def relative_ref(self, path: Path) -> str:
        resolved = path.resolve()
        if not resolved.is_relative_to(self.workspace / '.pact'):
            raise RuntimeStateError('artifact is outside PACT storage')
        return str(resolved.relative_to(self.workspace))

    def resolve_ref(self, ref: object) -> Path:
        if not isinstance(ref, str):
            raise RuntimeStateError('artifact ref must be a string')
        path = (self.workspace / ref).resolve()
        if not path.is_relative_to(self.paths.campaign_dir):
            raise RuntimeStateError('artifact ref is outside work')
        return path

    def read_current_state(self) -> dict[str, object]:
        state = read_json(self.paths.current_state)
        if state.get('schema') != WORK_STATE_SCHEMA or state.get('campaign_id') != self.campaign_id:
            raise RuntimeStateError('unsupported work state schema or identity')
        revision = state.get('revision')
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise RuntimeStateError('invalid work revision')
        snapshot = read_json(self.resolve_ref(state.get('snapshot_ref')))
        if state != snapshot:
            raise RuntimeStateError('current work state does not match its immutable snapshot')
        if self._record_hashes(state) != state.get('record_hashes'):
            raise RuntimeStateError('work record hash binding mismatch')
        if state.get('status') not in ('active', 'blocked', 'paused', 'completed'):
            raise RuntimeStateError('invalid work status')
        checks = state.get('checks')
        if not isinstance(checks, list):
            raise RuntimeStateError('invalid registered check methods')
        for check in checks:
            if (not isinstance(check, dict) or check.get('source') != 'derived'
                    or check.get('disposition') not in ('pending', 'admitted', 'suspended')
                    or type(check.get('version')) is not int or check['version'] < 1
                    or 'method_review' not in check):
                raise RuntimeStateError('invalid check lifecycle record')
            review = check['method_review']
            if check['disposition'] == 'pending':
                if review is not None:
                    raise RuntimeStateError('pending method cannot claim reviewed disposition')
            elif (not isinstance(review, dict) or set(review) != {'review_ref', 'reason'}
                    or any(not isinstance(review.get(key), str) or not review[key].strip()
                           for key in ('review_ref', 'reason'))
                    or review['review_ref'] not in cast(list[str], state['evidence_refs'])):
                raise RuntimeStateError('check disposition lacks its recorded Review and reason')
        return state

    def _record_hashes(self, state: dict[str, object]) -> dict[str, str]:
        history = state.get('evidence_refs')
        if not isinstance(history, list):
            raise RuntimeStateError('invalid work evidence history')
        refs = [state.get(key) for key in ('contract_ref', 'candidate_ref', 'verification_ref',
                                         'review_ref', 'acceptance_ref', 'delivery_ref')]
        refs.extend(history)
        return {str(ref): digest(self.resolve_ref(ref)) for ref in refs if ref is not None}

    def read_record(self, ref: object, schema: str = EXECUTION_EVIDENCE_SCHEMA) -> dict[str, object]:
        value = read_json(self.resolve_ref(ref))
        if value.get('schema') != schema or value.get('campaign_id') != self.campaign_id:
            raise RuntimeStateError('unsupported artifact schema or work identity')
        hashes = value.get('hashes', {})
        if not isinstance(hashes, dict):
            raise RuntimeStateError('invalid artifact bindings')
        for path, expected in hashes.items():
            if not isinstance(path, str):
                raise RuntimeStateError('invalid artifact path')
            resolved = Path(path).resolve()
            if not resolved.is_relative_to(self.workspace / '.pact') or digest(resolved) != expected:
                raise RuntimeStateError('artifact evidence hash mismatch')
        return value

    def read_campaign_revision(self) -> dict[str, object]:
        state = self.read_current_state()
        record = self.read_record(state['contract_ref'], CAMPAIGN_REVISION_SCHEMA)
        if record.get('revision') != state.get('contract_revision'):
            raise RuntimeStateError('contract revision binding mismatch')
        return record

    def read_contract(self) -> dict[str, object]:
        value = self.read_campaign_revision().get('goal_contract')
        if not isinstance(value, dict):
            raise RuntimeStateError('missing Goal Contract')
        return validate_goal_contract(value)

    def read_policy(self) -> AcceptancePolicy:
        return AcceptancePolicy.parse(self.read_campaign_revision().get('policy'))

    def assert_expected_revision(self, revision: int) -> dict[str, object]:
        current = self.read_current_state()
        if isinstance(revision, bool) or current['revision'] != revision:
            raise StaleWorkStateError('stale expected work revision')
        return current


class _WorkStore(CampaignReader):
    """Only WorkRuntime may commit facts through this store."""
    def write_record(self, identity: str, payload: dict[str, object], *, schema: str = EXECUTION_EVIDENCE_SCHEMA) -> str:
        path = self.paths.evidence_dir / f'{identity}.json'
        record = dict(payload, schema=schema, campaign_id=self.campaign_id)
        if path.exists():
            if read_json(path) != record:
                raise RuntimeStateError('immutable operation record changed')
        else:
            write_json_atomic(path, record)
        return self.relative_ref(path)

    def initialize(self, state: dict[str, object]) -> dict[str, object]:
        if self.paths.current_state.exists():
            return self.read_current_state()
        path = self.paths.state_revisions / 'rev-000000.json'
        initial = dict(state, schema=WORK_STATE_SCHEMA, campaign_id=self.campaign_id,
                       revision=0, parent_ref=None, snapshot_ref=self.relative_ref(path),
                       record_hashes=self._record_hashes(state))
        if path.exists() and read_json(path) != initial:
            raise RuntimeStateError('initial work snapshot differs from creation inputs')
        write_json_atomic(path, initial)
        write_json_atomic(self.paths.current_state, initial)
        write_json_atomic(self.paths.latest, {'schema': LATEST_CAMPAIGN_SCHEMA, 'campaign_id': self.campaign_id})
        return initial

    def commit(self, *, expected_revision: int, state: dict[str, object]) -> dict[str, object]:
        from .recovery import prepare_state_commit
        previous = self.assert_expected_revision(expected_revision)
        path = self.paths.state_revisions / f"rev-{expected_revision + 1:06d}-{state['last_operation_id']}.json"
        target = dict(state, schema=WORK_STATE_SCHEMA, campaign_id=self.campaign_id,
                      revision=expected_revision + 1, parent_ref=previous['snapshot_ref'],
                      snapshot_ref=self.relative_ref(path), record_hashes=self._record_hashes(state))
        if path.exists() and read_json(path) != target:
            raise RuntimeStateError('prepared state snapshot changed')
        write_json_atomic(path, target)
        prepare_state_commit(self, previous, target)
        write_json_atomic(self.paths.current_state, target)
        return target
