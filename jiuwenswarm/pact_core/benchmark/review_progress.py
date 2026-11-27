"""Small durable scheduling observations, subordinate to Runtime evidence.

These records never certify a check or acceptance. They merge review reasons per
check version; the Runtime operation history retains full evidence and decisions.
"""
from __future__ import annotations

from typing import cast

from .failures import observe_checks
from ..runtime.schemas import RuntimeContractError
from ..runtime.types import WorkSnapshot


def records(progress: dict[str, object]) -> dict[str, dict[str, object]]:
    raw = progress.get('check_observations', {})
    if not isinstance(raw, dict) or any(not isinstance(k, str) or not isinstance(v, dict) for k, v in raw.items()):
        raise RuntimeContractError('Invalid benchmark check observations')
    return {str(k): dict(v) for k, v in raw.items()}


def pending(progress: dict[str, object]) -> bool:
    return any(item.get('pending') is True for item in records(progress).values())


def observe(progress: dict[str, object], state: WorkSnapshot) -> dict[str, object]:
    observations = records(progress)
    evidence = state.verification
    if evidence is None:
        return progress
    turn = int(cast(int, progress.get('worker_turns', 0)))
    derived = {str(check['id']) for check in state.checks}
    for observation in observe_checks(state):
        key = f'{observation.check_id}:{observation.version}'
        old = observations.get(key, {})
        if old.get('evidence') == str(evidence.artifact) or observation.status in {'skipped', 'not_run'}:
            continue
        failing = observation.status not in {'passed', 'skipped', 'not_run'}
        old_failing = old.get('status') not in {None, 'passed', 'skipped', 'not_run'}
        keys = list(observation.failure_keys)
        new_failure = failing and (not old_failing or bool(keys) and keys != old.get('failure_keys')
            or observation.category != old.get('category') or observation.status != old.get('status'))
        reasons = list(cast(list[str], old.get('reasons', [])))
        if not old and observation.check_id in derived:
            reasons.append('first_result')
        if new_failure:
            reasons.append('failure_changed' if old_failing else 'failure_started')
        counts = dict(cast(dict[str, int], old.get('failure_counts', {})))
        for failure in keys:
            counts[failure] = counts.get(failure, 0) + 1
        # A current pass closes execution failure, not an unreviewed new method.
        due = bool(old.get('pending')) or new_failure or (not old and observation.check_id in derived)
        if observation.status == 'passed' and 'first_result' not in reasons:
            due = False
            reasons = []
        observations[key] = {**old, 'check_id': observation.check_id, 'version': observation.version,
            'name': observation.name, 'status': observation.status, 'failure_keys': keys,
            'category': observation.category, 'failure_counts': counts,
            'evidence': str(evidence.artifact), 'candidate_tree': state.candidate.tree,
            'pending': due, 'reasons': sorted(set(reasons)),
            'priority': bool(old.get('priority')) or bool(new_failure and observation.urgent),
            'first_observed_turn': old.get('first_observed_turn', turn)}
    if evidence.candidate == state.candidate and evidence.status == 'passed':
        for item in observations.values():
            if item.get('check_id') == 'verification':
                item.update(pending=False, priority=False, resolution='current_verification_passed')
    # A superseded definition stays in history but does not demand its own review.
    current = {str(check['id']): int(cast(int, check['version'])) for check in state.checks}
    suspended = {str(check['id']) for check in state.checks if check.get('disposition') == 'suspended'}
    for item in observations.values():
        identity = str(item['check_id'])
        if identity in current and (item['version'] != current[identity] or identity in suspended):
            item.update(pending=False, priority=False, resolution='definition_superseded_or_suspended')
    return {**progress, 'check_observations': observations}


def mark_attempt(progress: dict[str, object]) -> dict[str, object]:
    observations = records(progress)
    for item in observations.values():
        item['priority'] = False
    return {**progress, 'check_observations': observations,
            'last_feedback_attempt_turn': progress.get('worker_turns', 0)}


def mark_reviewed(progress: dict[str, object], keys: list[str], artifact: str) -> dict[str, object]:
    observations = records(progress)
    for key in keys:
        if key in observations:
            observations[key].update(pending=False, priority=False, reasons=[], reviewed_by=artifact)
    return {**progress, 'check_observations': observations}
