"""Bound operation results and exact prepared-state acknowledgement."""
from __future__ import annotations

from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import cast

from ..artifacts import read_json, write_json_atomic
from ..kernel import workspace_identity
from .boundary import marker_path, read_operation
from .schemas import RuntimeStateError
from .store import CampaignReader, digest

artifact_digest = digest


def receipt_hashes(value: object) -> dict[str, str]:
    if isinstance(value, Path):
        return {str(value): digest(value)} if value.is_file() and value.name not in {'state.json', 'events.jsonl', 'promotion.json'} else {}
    result: dict[str, str] = {}
    if is_dataclass(value) and not isinstance(value, type):
        for field in fields(value):
            result.update(receipt_hashes(getattr(value, field.name)))
    elif isinstance(value, dict):
        for child in value.values():
            result.update(receipt_hashes(child))
    return result


def check_hashes(reader: CampaignReader, hashes: object) -> None:
    if not isinstance(hashes, dict):
        raise RuntimeStateError('operation lacks artifact hashes')
    for path, expected in hashes.items():
        if not isinstance(path, str):
            raise RuntimeStateError('invalid operation artifact path')
        artifact = Path(path).resolve()
        if not artifact.is_relative_to(reader.workspace / '.pact') or digest(artifact) != expected:
            raise RuntimeStateError('operation artifact hash mismatch')


def operation_input(reader: CampaignReader) -> dict[str, object]:
    operation = read_operation(reader)
    path = reader.resolve_ref(operation.get('input_state_ref'))
    if digest(path) != operation.get('input_state_sha256'):
        raise RuntimeStateError('operation input snapshot hash mismatch')
    state = read_json(path)
    if (state.get('revision') != operation.get('expected_revision')
            or state.get('workspace_basis') != operation.get('source_basis')
            or state.get('campaign_id') != reader.campaign_id):
        raise RuntimeStateError('operation input identity mismatch')
    return state


def record_result(reader: CampaignReader, result: dict[str, object], hashes: dict[str, str]) -> None:
    operation = read_operation(reader)
    operation_input(reader)
    if operation.get('result') is not None and (operation['result'] != result or operation.get('hashes') != hashes):
        raise RuntimeStateError('operation result changed')
    operation.update(result=result, hashes=hashes, phase='result_saved')
    write_json_atomic(marker_path(reader), operation)


def prepare_state_commit(reader: CampaignReader, previous: dict[str, object], state: dict[str, object]) -> None:
    operation = read_operation(reader)
    if previous != operation_input(reader) or state.get('last_operation_id') != operation['id']:
        raise RuntimeStateError('state commit is not bound to the current operation')
    path = reader.resolve_ref(state['snapshot_ref'])
    refs = [state.get(name) for name in ('contract_ref', 'candidate_ref', 'verification_ref', 'review_ref', 'acceptance_ref', 'delivery_ref')]
    histories = state.get('evidence_refs', [])
    if not isinstance(histories, list):
        raise RuntimeStateError('invalid evidence history')
    refs.extend(histories)
    hashes = {str(reader.resolve_ref(ref)): digest(reader.resolve_ref(ref)) for ref in refs if ref is not None}
    target = {'ref': state['snapshot_ref'], 'sha256': digest(path), 'hashes': hashes}
    if operation.get('target') is not None and operation['target'] != target:
        raise RuntimeStateError('operation target changed')
    operation.update(target=target, phase='state_prepared')
    write_json_atomic(marker_path(reader), operation)


def prepared_operation(reader: CampaignReader) -> dict[str, object] | None:
    operation = read_operation(reader)
    original = operation_input(reader)
    target = operation.get('target')
    if target is None:
        return None
    if not isinstance(target, dict):
        raise RuntimeStateError('invalid prepared operation')
    path = reader.resolve_ref(target.get('ref'))
    if digest(path) != target.get('sha256'):
        raise RuntimeStateError('prepared snapshot hash mismatch')
    state = read_json(path)
    check_hashes(reader, target.get('hashes'))
    check_hashes(reader, operation.get('hashes', {}))
    current = reader.read_current_state()
    if (state.get('last_operation_id') != operation['id']
            or state.get('revision') != cast(int, original['revision']) + 1
            or state.get('parent_ref') != original['snapshot_ref']
            or current not in (original, state)):
        raise RuntimeStateError('prepared snapshot is unrelated to current work')
    if workspace_identity(reader.workspace) != state.get('workspace_basis'):
        raise RuntimeStateError('prepared operation source changed')
    return state
