"""Normalize an accepted text plan without rewriting its user-owned content."""

from __future__ import annotations

from pathlib import Path

from .planning import InitialPlan, validate_initial_plan
from .schemas import (
    GOAL_CONTRACT_SCHEMA, INITIAL_PLAN_SCHEMA, RuntimeContractError,
    validate_goal_contract,
)


def read_text_plan(path: Path, *, workspace: Path) -> tuple[dict[str, object], InitialPlan]:
    """Read once, then retain the entire UTF-8 input as the canonical Goal."""
    path = path.expanduser()
    if not path.is_absolute():
        path = workspace / path
    text = path.read_bytes().decode("utf-8")
    # A damaged structured plan must not become a different, accepted contract
    # merely because JSON parsing failed or its filename says Markdown.
    if path.suffix.lower() == ".json" or text.lstrip().startswith("{"):
        raise RuntimeContractError("structured Initial Plan requires --contract")
    contract = validate_goal_contract({
        "schema": GOAL_CONTRACT_SCHEMA,
        "goal": text,
        "acceptance_criteria": [{
            "id": "plan-fulfilled",
            "text": "Fulfil every requirement and acceptance criterion in the accepted plan.",
        }],
        "non_goals": [],
    })
    plan = validate_initial_plan({
        "schema": INITIAL_PLAN_SCHEMA,
        "constraints": [
            "Keep all requirements, constraints and non-goals in the accepted plan authoritative.",
        ],
        "missions": [{
            "id": "mission-1",
            "objective": "Implement the complete accepted plan in the Goal Contract.",
            "target_ac_ids": ["plan-fulfilled"],
            "dependencies": [],
            "verification_intent": "Run configured public checks and independently assess the complete plan.",
        }],
    }, acceptance_criterion_ids=("plan-fulfilled",))
    return contract, plan
