"""Small read-only view of work facts, rebuilt from the current immutable snapshot."""
from __future__ import annotations

from datetime import UTC, datetime

from .._runtime_serialization import decode
from ..kernel import WorkCandidate
from .boundary import marker_path, read_operation
from .schemas import CAMPAIGN_REVISION_SCHEMA, DASHBOARD_PROJECTION_SCHEMA, goal_contract_criteria
from .store import CampaignReader
from .types import object_record


def build_dashboard_projection(reader: CampaignReader) -> dict[str, object]:
    state = reader.read_current_state()
    contract = reader.read_record(state['contract_ref'], schema=CAMPAIGN_REVISION_SCHEMA)
    goal = object_record(contract['goal_contract'], 'Goal Contract')
    candidate = decode(WorkCandidate, reader.read_record(state['candidate_ref'])['candidate'])
    verification = None
    if state['verification_ref']:
        record = reader.read_record(state['verification_ref'])
        value = object_record(record['verification'], 'verification')
        verification = {key: value.get(key) for key in ('status', 'exit_code', 'stdout_tail', 'stderr_tail')}
    review = reader.read_record(state['review_ref']) if state['review_ref'] else None
    operation = None
    if marker_path(reader).exists():
        marker = read_operation(reader)
        operation = {key: marker.get(key) for key in ('action', 'phase', 'expected_revision')}
        # A journal indicates ownership/recovery needs, not process liveness.
        operation['liveness'] = 'unknown'
    refs = state['evidence_refs']
    assert isinstance(refs, list)
    criteria = goal_contract_criteria(goal)
    return {
        'schema': DASHBOARD_PROJECTION_SCHEMA, 'projection_is_writable': False,
        'availability': 'ready', 'generated_at': datetime.now(UTC).isoformat(),
        'overview': {'work_id': reader.campaign_id, 'status': state['status'],
                     'revision': state['revision'], 'contract_revision': state['contract_revision'],
                     'goal': str(goal['goal'])},
        'candidate': {'checkpoint': candidate.checkpoint, 'tree': candidate.tree,
                      'summary': candidate.summary.read_text(encoding='utf-8')[-12000:]},
        'verification': verification,
        'review': {'complete': review['complete'], 'reasons': review['reasons'],
                   'candidate_ref': review['candidate_ref']} if review else None,
        'acceptance': {'accepted': bool(state['acceptance_ref']), 'delivered': bool(state['delivery_ref']),
                       'criteria': criteria},
        'questions': state['unresolved_assumptions'], 'user_decisions': state['user_decisions'],
        'review_requested': state['review_requested'], 'operation': operation,
        'artifacts': [reader.relative_ref(reader.resolve_ref(ref)) for ref in refs[-32:]],
    }


def empty_dashboard_projection(*, availability: str = 'empty', campaign_id: str | None = None) -> dict[str, object]:
    return {'schema': DASHBOARD_PROJECTION_SCHEMA, 'projection_is_writable': False,
            'availability': availability, 'work_id': campaign_id}
