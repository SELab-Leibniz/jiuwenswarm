"""Current versioned schemas for the accepted Runtime research slice."""

from __future__ import annotations


GOAL_CONTRACT_SCHEMA = "pact-runtime/goal-contract/v1"
CAMPAIGN_REVISION_SCHEMA = "pact-runtime/campaign-revision/v4"
INITIAL_PLAN_SCHEMA = "pact-runtime/initial-plan/v1"
WORK_STATE_SCHEMA = "pact-runtime/work-state/v10"
EXECUTION_EVIDENCE_SCHEMA = "pact-runtime/execution-evidence/v9"
LATEST_CAMPAIGN_SCHEMA = "pact-runtime/latest-campaign/v1"
DASHBOARD_PROJECTION_SCHEMA = "pact-runtime/dashboard-projection/v6"
OPERATION_SCHEMA = "pact-runtime/operation/v5"
REQUEST_IDENTITY_SCHEMA = "pact-runtime/request-identity/v1"



class RuntimeContractError(ValueError):
    """Raised when the user-authored Goal Contract is invalid or unsupported."""


class RuntimeStateError(RuntimeError):
    """Raised when durable Runtime state is corrupt or internally inconsistent."""


class StaleWorkStateError(RuntimeStateError):
    """Raised when a canonical state commit targets an obsolete revision."""


def validate_goal_contract(payload: dict[str, object]) -> dict[str, object]:
    """Validate and normalize the deliberately small demo Goal Contract."""
    schema = payload.get("schema")
    if schema != GOAL_CONTRACT_SCHEMA:
        raise RuntimeContractError(f"unsupported Goal Contract schema: {schema!r}")
    goal = payload.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        raise RuntimeContractError("Goal Contract goal must be a non-empty string")
    criteria = payload.get("acceptance_criteria")
    if not isinstance(criteria, list) or not criteria:
        raise RuntimeContractError(
            "Goal Contract acceptance_criteria must be a non-empty array"
        )
    normalized_criteria: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for index, criterion in enumerate(criteria):
        if not isinstance(criterion, dict):
            raise RuntimeContractError(
                f"acceptance_criteria[{index}] must be an object"
            )
        criterion_id = criterion.get("id")
        text = criterion.get("text")
        if not isinstance(criterion_id, str) or not criterion_id.strip():
            raise RuntimeContractError(
                f"acceptance_criteria[{index}].id must be a non-empty string"
            )
        if criterion_id in seen_ids:
            raise RuntimeContractError(f"duplicate acceptance criterion id: {criterion_id}")
        if not isinstance(text, str) or not text.strip():
            raise RuntimeContractError(
                f"acceptance_criteria[{index}].text must be a non-empty string"
            )
        seen_ids.add(criterion_id)
        normalized_criteria.append({"id": criterion_id, "text": text})
    non_goals = payload.get("non_goals")
    if not isinstance(non_goals, list) or any(
        not isinstance(value, str) or not value.strip() for value in non_goals
    ):
        raise RuntimeContractError("Goal Contract non_goals must be an array of strings")
    return {
        "schema": GOAL_CONTRACT_SCHEMA,
        "goal": goal,
        "acceptance_criteria": normalized_criteria,
        "non_goals": list(non_goals),
    }


def goal_contract_criteria(contract: dict[str, object]) -> list[dict[str, str]]:
    """Return typed AC records from an already validated Goal Contract."""
    value = contract["acceptance_criteria"]
    assert isinstance(value, list)
    result: list[dict[str, str]] = []
    for item in value:
        assert isinstance(item, dict)
        criterion_id = item["id"]
        text = item["text"]
        assert isinstance(criterion_id, str)
        assert isinstance(text, str)
        result.append({"id": criterion_id, "text": text})
    return result
