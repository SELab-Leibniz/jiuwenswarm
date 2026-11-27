"""Optional default-driver task input validation; no canonical workflow state."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .schemas import (
    INITIAL_PLAN_SCHEMA,
    RuntimeContractError,
)

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")

@dataclass(frozen=True, slots=True)
class MissionDefinition:
    id: str
    objective: str
    target_ac_ids: tuple[str, ...]
    dependencies: tuple[str, ...]
    verification_intent: str


@dataclass(frozen=True, slots=True)
class InitialPlan:
    missions: tuple[MissionDefinition, ...]
    constraints: tuple[str, ...]


def initial_plan_payload(plan: InitialPlan) -> dict[str, object]:
    """Return the normalized user-authored Initial Plan shape."""
    return {
        "schema": INITIAL_PLAN_SCHEMA,
        "constraints": list(plan.constraints),
        "missions": [
            {
                "id": mission.id,
                "objective": mission.objective,
                "target_ac_ids": list(mission.target_ac_ids),
                "dependencies": list(mission.dependencies),
                "verification_intent": mission.verification_intent,
            }
            for mission in plan.missions
        ],
    }


def validate_initial_plan(
    payload: dict[str, object],
    *,
    acceptance_criterion_ids: tuple[str, ...],
) -> InitialPlan:
    """Validate an optional initial task list and its dependencies."""
    if payload.get("schema") != INITIAL_PLAN_SCHEMA:
        raise RuntimeContractError(
            f"unsupported Initial Plan schema: {payload.get('schema')!r}"
        )
    allowed_top_level = {"schema", "missions", "constraints"}
    unknown_top_level = set(payload).difference(allowed_top_level)
    if unknown_top_level:
        raise RuntimeContractError(
            f"Initial Plan contains unsupported field(s): {sorted(unknown_top_level)}"
        )
    raw_constraints = payload.get("constraints", [])
    constraints = _string_array(raw_constraints, "Initial Plan constraints")
    raw_missions = payload.get("missions")
    if not isinstance(raw_missions, list) or not raw_missions:
        raise RuntimeContractError("Initial Plan missions must be a non-empty array")

    ac_ids = set(acceptance_criterion_ids)
    if len(ac_ids) != len(acceptance_criterion_ids):
        raise RuntimeContractError("Goal Contract acceptance criterion ids must be unique")
    staged: list[tuple[str, str, tuple[str, ...], tuple[str, ...], str]] = []
    seen_ids: set[str] = set()
    targeted_ac_ids: set[str] = set()
    allowed_mission_fields = {
        "id",
        "objective",
        "target_ac_ids",
        "dependencies",
        "verification_intent",
    }
    for index, raw_mission in enumerate(raw_missions):
        label = f"Initial Plan missions[{index}]"
        if not isinstance(raw_mission, dict):
            raise RuntimeContractError(f"{label} must be an object")
        unknown_fields = set(raw_mission).difference(allowed_mission_fields)
        if unknown_fields:
            raise RuntimeContractError(
                f"{label} contains unsupported field(s): {sorted(unknown_fields)}"
            )
        mission_id = _non_empty_string(raw_mission.get("id"), f"{label}.id")
        if not _ID_RE.fullmatch(mission_id):
            raise RuntimeContractError(f"{label}.id is not a stable identifier")
        if mission_id in seen_ids:
            raise RuntimeContractError(f"duplicate Mission id: {mission_id}")
        seen_ids.add(mission_id)
        objective = _non_empty_string(
            raw_mission.get("objective"), f"{label}.objective"
        )
        target_ac_ids = _string_array(
            raw_mission.get("target_ac_ids"), f"{label}.target_ac_ids"
        )
        if not target_ac_ids:
            raise RuntimeContractError(f"{label}.target_ac_ids must not be empty")
        if len(set(target_ac_ids)) != len(target_ac_ids):
            raise RuntimeContractError(f"{label}.target_ac_ids contains duplicates")
        unknown_ac_ids = set(target_ac_ids).difference(ac_ids)
        if unknown_ac_ids:
            raise RuntimeContractError(
                f"{label} references unknown AC id(s): {sorted(unknown_ac_ids)}"
            )
        targeted_ac_ids.update(target_ac_ids)
        dependencies = _string_array(
            raw_mission.get("dependencies"), f"{label}.dependencies"
        )
        if len(set(dependencies)) != len(dependencies):
            raise RuntimeContractError(f"{label}.dependencies contains duplicates")
        verification_intent = _non_empty_string(
            raw_mission.get("verification_intent"),
            f"{label}.verification_intent",
        )
        staged.append(
            (
                mission_id,
                objective,
                target_ac_ids,
                dependencies,
                verification_intent,
            )
        )

    missing_ac_ids = ac_ids.difference(targeted_ac_ids)
    if missing_ac_ids:
        raise RuntimeContractError(
            f"Initial Plan does not target AC id(s): {sorted(missing_ac_ids)}"
        )
    dependencies_by_id = {item[0]: item[3] for item in staged}
    for mission_id, dependencies in dependencies_by_id.items():
        missing_dependencies = set(dependencies).difference(seen_ids)
        if missing_dependencies:
            raise RuntimeContractError(
                f"Mission {mission_id} references unknown dependency id(s): "
                f"{sorted(missing_dependencies)}"
            )
        if mission_id in dependencies:
            raise RuntimeContractError(f"Mission {mission_id} cannot depend on itself")
    _assert_acyclic(dependencies_by_id)

    missions = tuple(
        MissionDefinition(
            id=mission_id,
            objective=objective,
            target_ac_ids=target_ac_ids,
            dependencies=dependencies,
            verification_intent=verification_intent,
        )
        for (
            mission_id,
            objective,
            target_ac_ids,
            dependencies,
            verification_intent,
        ) in staged
    )
    return InitialPlan(missions=missions, constraints=constraints)


def _assert_acyclic(dependencies_by_id: dict[str, tuple[str, ...]]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(mission_id: str) -> None:
        if mission_id in visiting:
            raise RuntimeContractError("Initial Plan Mission dependencies contain a cycle")
        if mission_id in visited:
            return
        visiting.add(mission_id)
        for dependency in dependencies_by_id[mission_id]:
            visit(dependency)
        visiting.remove(mission_id)
        visited.add(mission_id)

    for mission_id in dependencies_by_id:
        visit(mission_id)


def _non_empty_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeContractError(f"{label} must be a non-empty string")
    return value.strip()


def _string_array(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise RuntimeContractError(f"{label} must be an array of non-empty strings")
    return tuple(item.strip() for item in value)
