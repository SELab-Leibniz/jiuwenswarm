"""Conservative, per-check observations for benchmark feedback scheduling.

These describe execution evidence, not root causes or acceptance authority. An
unknown identity must still reach the driver's bounded feedback policy. Raw
output stays in the original evidence rather than in scheduling state.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import re

from ..runtime.types import VerificationEvidence, WorkSnapshot


@dataclass(frozen=True, slots=True)
class CheckObservation:
    check_id: str
    version: int
    name: str
    status: str
    failure_keys: tuple[str, ...]
    category: str
    urgent: bool


def observe_checks(state: WorkSnapshot) -> tuple[CheckObservation, ...]:
    """Observe only the current candidate; historical evidence cannot close faults."""
    if state.verification is None or state.verification.candidate != state.candidate:
        return ()
    commands = [state.policy.verify_command] if state.policy.verify_command else []
    return observe_evidence(state.verification, checks=state.checks, supplied_commands=commands)


def observe_evidence(
    evidence: VerificationEvidence | None, *,
    checks: Sequence[Mapping[str, object]] = (),
    supplied_commands: Sequence[str] = (),
) -> tuple[CheckObservation, ...]:
    """Observe registered definitions and an optional supplied check prefix.

    ``failure_ids`` is an optional explicit list of structured assertion IDs in
    a check result; no arbitrary test-framework output parser is assumed. The
    caller owns evidence integrity validation. Definition mismatches are not
    interpreted as current results, and omitted checks are never called passed.
    """
    if evidence is None:
        return ()
    active = [item for item in checks if item.get('disposition') != 'suspended']
    definitions = {str(item['name']): item for item in active}
    suspended = {str(item['name']) for item in checks if item.get('disposition') == 'suspended'}
    definition: Mapping[str, object]
    if not active and not evidence.check_results:
        command = evidence.command or (supplied_commands[0] if supplied_commands else None)
        definition = _supplied('public-1', command) if command else _aggregate()
        return (_observation(definition, _aggregate_result(evidence), evidence),)

    expected = [*(_supplied(f'public-{index + 1}', command)
                  for index, command in enumerate(supplied_commands)), *active]
    observations: list[CheckObservation] = []
    seen: set[tuple[str, str]] = set()
    for result in evidence.check_results:
        name = str(result.get('name', ''))
        source = str(result.get('source', 'derived'))
        if source == 'supplied':
            command_value = result.get('command')
            if not isinstance(command_value, str):
                continue
            definition = _supplied(name, command_value)
        else:
            if name in suspended or name not in definitions:
                continue
            definition = dict(definitions[name])
            if result.get('fingerprint') != definition.get('fingerprint'):
                continue
        seen.add((source, name))
        observations.append(_observation(definition, result, evidence))

    # Only completed results are recorded. Absence cannot prove that the next
    # check started: timeout may occur between checks or in a closeout command
    # absent from this ordinary verification list. Keep missing items skipped.
    timed_out = any(item.status == 'timeout' for item in observations)
    for definition in expected:
        identity = (str(definition.get('source', 'derived')), str(definition['name']))
        if identity in seen:
            continue
        observations.append(_observation(definition, {'status': 'skipped'}, evidence))
    if (evidence.status == 'timeout' and not timed_out
            or evidence.status in ('failed', 'unavailable', 'inconclusive')
            and not any(item.status not in ('passed', 'skipped', 'not-run') for item in observations)):
        observations.append(_observation(_aggregate(), _aggregate_result(evidence), evidence))
    return tuple(observations)


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _supplied(name: str, command: str) -> dict[str, object]:
    return {'name': name, 'id': 'supplied-' + _digest(command)[:20], 'version': 1,
        'source': 'supplied', 'command': command}


def _aggregate() -> dict[str, object]:
    return {'name': 'verification', 'id': 'verification', 'version': 1}


def _aggregate_result(evidence: VerificationEvidence) -> dict[str, object]:
    return {'status': evidence.status, 'stdout_tail': evidence.stdout_tail, 'stderr_tail': evidence.stderr_tail}


def _observation(
    definition: Mapping[str, object], result: Mapping[str, object], evidence: VerificationEvidence,
) -> CheckObservation:
    check_id, name = str(definition['id']), str(definition['name'])
    version = definition.get('version', 1)
    if type(version) is not int or version < 1:
        raise ValueError('check observation requires a positive definition version')
    status = str(result.get('status', 'inconclusive'))
    category, urgent, identities = _identities(status, result, evidence)
    urgent = urgent or status in ('timeout', 'unavailable')
    if category == 'not-run':
        status = 'skipped'
    keys = tuple(sorted({_digest([check_id, version, *identity]) for identity in identities}))
    return CheckObservation(check_id, version, name, status, keys, category, urgent)


def _identities(
    status: str, result: Mapping[str, object], evidence: VerificationEvidence,
) -> tuple[str, bool, list[tuple[str, str, str]]]:
    if status == 'passed':
        return 'passed', False, []
    if status in ('skipped', 'not-run', 'not_run'):
        return 'not-run', False, []
    if status == 'timeout':
        return status, True, [('execute', status, '')]
    output = '\n'.join(str(result.get(field, '')) for field in ('stdout_tail', 'stderr_tail'))
    if output.strip() == 'budget_exhausted':
        return 'not-run', False, []
    if output.strip().startswith('definition_changed:'):
        return 'definition-changed', True, [('definition-binding', 'definition-changed', output.strip())]
    failure_ids = result.get('failure_ids')
    if (isinstance(failure_ids, list) and failure_ids
            and all(isinstance(item, str) and item.strip() for item in failure_ids)):
        return 'assertion-failed', False, [('assertion', 'assertion-failed', str(item)) for item in failure_ids]

    # Only explicit diagnostics are recognized. A missing task output is not
    # automatically a broken validator; only failed invocation is urgent here.
    entry_paths = re.findall(r"can't open file ['\"]([^'\"\n]+)['\"]: \[Errno 2\]", output)
    missing_paths = re.findall(
        r"FileNotFoundError: \[Errno 2\] No such file or directory: ['\"]([^'\"\n]+)['\"]", output)
    commands = re.findall(r"(?m)^(?:/bin/(?:ba)?sh: (?:(?:line )?\d+: )?)([^:\n]+): (?:command not found|not found)\s*$", output)
    if entry_paths or missing_paths or commands:
        identities = [('invoke-validator', 'resource-missing', _normalize(path, evidence)) for path in entry_paths]
        identities.extend(('execute', 'resource-missing', _normalize(path, evidence)) for path in missing_paths)
        identities.extend(('invoke-validator', 'command-not-found', command.strip()) for command in commands)
        return 'resource-missing', bool(entry_paths or commands), identities
    if re.search(r'(?m)^\s*AssertionError:\s*\S', output):
        return 'assertion-failed', False, [('assertion', 'assertion-failed', _normalize(output, evidence))]
    if status == 'unavailable':
        return 'unavailable', True, []
    return 'unknown', False, []


def _normalize(output: str, evidence: VerificationEvidence) -> str:
    # Normalize only operation paths whose ID is actually bound to this evidence,
    # never arbitrary hexadecimal strings, assertion numbers or resource paths.
    match = re.search(r'/work-([0-9a-f]{32})(?:/|$)', evidence.artifact.as_posix())
    if match:
        output = re.sub(r"/[^\s'\"]*/work-" + match.group(1) + r'(?=/)', '<operation-path>', output)
    # Explicit standalone measurement records are instrumentation, unlike a
    # numerical value inside an assertion or an arbitrary time-looking string.
    return re.sub(r'(?m)^\s*(?:duration_seconds|elapsed_seconds)\s*[:=]\s*\d+(?:\.\d+)?\s*$',
        '<duration>', output).strip()
