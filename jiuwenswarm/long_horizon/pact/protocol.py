# Copyright (c) 2026 Chrys. All rights reserved.
"""Deterministic PACT role protocol normalization adapted from Chrys.

Source: chrys 6fcfb3321892735631b49f33236b1e2a988ded2d,
``src/chrys/pact/role_runner.py``. This module contains no model runtime.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..structured import parse_unique_json_object


_FENCED_WHOLE = re.compile(r"\A\s*```[A-Za-z0-9_+-]*[ \t]*\r?\n(.*?)\r?\n[ \t]*```\s*\Z", re.DOTALL)


_JSON_PROTOCOL_ROLES = frozenset({"manager", "planner"})

# What the runtime's own prompts leave implicit and live campaigns tripped over: a
# Planner that merged two missions by deleting one ("Planner proposal cannot delete
# existing Missions"), a Planner that decorated missions with a `successors` field the
# schema rejects, a Manager that answered in prose. Said once more, plainly.
_ROLE_PROTOCOL_REMINDERS = {
    "planner": (
        "Protocol constraints (the runtime rejects proposals that break them):\n"
        "- Never delete, rename or edit an existing mission. To replace one, add a NEW mission whose "
        "`supersedes` lists the old id and add a matching `supersede_mission` operation; the "
        "old mission stays in the graph unchanged. A challenged or blocked mission MUST be "
        "superseded this way.\n"
        "- `constraints` are copied from the current plan unchanged; a proposal that edits them is "
        "rejected (R3).\n"
        "- A mission has exactly these fields: id, objective, target_ac_ids, dependencies, "
        "supersedes, verification_intent. No successors, no status, no notes.\n"
        '- `operations` items are exactly `{"op":"add_mission","mission_id":...}` or '
        '`{"op":"supersede_mission","mission_id":<old>,"replacement_mission_ids":[...]}`.\n'
        "- A `supersede_mission` must BIND the old mission to real replacements: every id in "
        "`replacement_mission_ids` has to be a NEW mission defined in this proposal's `missions` "
        "array whose `supersedes` lists the old id, and each such new mission also needs its own "
        "`add_mission` operation. An empty `replacement_mission_ids`, or one naming a mission that "
        "already exists, is rejected.\n"
        "- `affected_mission_ids` is exactly the set of mission ids your operations name (each "
        "`mission_id` plus every entry of `replacement_mission_ids`), no duplicates; "
        "`affected_ac_ids` lists the acceptance criteria those missions target, no duplicates.\n"
        "- Use `parent_plan_revision` and `input_work_state_revision` exactly as given in the input, "
        "and change something material (a proposal identical to the current plan is rejected).\n"
        "- Every mission targets at least one acceptance criterion: `target_ac_ids` is never empty, "
        "and `dependencies` name only missions that exist in the proposal.\n"
        "- A superseded mission never executes, so nothing may keep depending on it: when you "
        "supersede X, every mission whose `dependencies` include X must be superseded in the same "
        "proposal by a replacement that depends on X's replacements instead. A plan that leaves "
        "such a dependency is not executable and the Manager blocks the campaign.\n"
        "- The proposal object carries exactly these keys and no others: schema, "
        "parent_plan_revision, input_work_state_revision, reason, rationale, constraints, "
        "missions, operations, affected_mission_ids, affected_ac_ids.\n"
        "- If you are asked to repair the format of a proposal, change ONLY what the error names: "
        "every other field -- reason, rationale, constraints, missions, the revisions -- must come "
        "back byte for byte, or the repair is rejected as a changed decision.\n"
        "- Reply with the JSON object as the text of your message: no prose before or after it, "
        "no Markdown fence, and never written to a file instead of the reply."
    ),
    "manager": (
        "Protocol constraints: `selected_mission_id` belongs to the `select` action alone -- it "
        "must name a mission from the Frontier there, and must be null or absent for every other "
        "action (retry, request_replan, escalate, stop). "
        "When the governance trigger is `repeated_no_progress` (the same mission failed twice in a "
        "row), `retry` is not allowed: choose `request_replan` with concrete `constraints` for the "
        "Planner (split or re-scope the mission), or escalate/stop. "
        "Reply with the JSON decision object as the text of your message "
        "-- no prose before or after it, no Markdown fence, never written to a file instead of "
        "the reply. Use `expected_plan_revision` and `expected_work_state_revision` exactly as "
        "given in the input."
    ),
}

# A Planner asked for a format repair once wrote the repaired object to a file and
# replied with nothing; the runtime parsed "" and blocked the campaign. One follow-up
# in the same session recovers that turn at the cost of a single model call.
_JSON_FOLLOWUP_PROMPT = (
    "Your previous message contained no JSON object in its text (an object written to a file "
    "does not reach the runtime). Reply now with exactly one JSON object as the message text: "
    "no prose, no Markdown fence. If you prepared the object in a file, paste its contents."
)
_JSON_FOLLOWUP_TIMEOUT_SECONDS = 600.0


# pact_core fingerprints these fields of a Planner proposal and rejects a format
# repair that changes any of them ("format repair changed Planner semantics"). A
# model asked to fix one field tends to rewrite the others too, so the repair's
# semantics are taken from the original proposal and only the rest from the reply.
_PLANNER_SEMANTIC_FIELDS = (
    "parent_plan_revision",
    "input_work_state_revision",
    "reason",
    "rationale",
    "constraints",
    "missions",
    "affected_mission_ids",
    "affected_ac_ids",
)
_REPAIR_MARKER = "Structured Output Repair"
_INVALID_OUTPUT = re.compile(r"<invalid-output>\s*(.*?)\s*</invalid-output>", re.DOTALL)


def _preserve_repair_semantics(prompt: str, payload: str) -> str:
    """For a Planner format repair, keep the original proposal's semantic fields."""
    if _REPAIR_MARKER not in prompt:
        return payload
    match = _INVALID_OUTPUT.search(prompt)
    if match is None:
        return payload
    try:
        original = json.loads(match.group(1))
        repaired = json.loads(payload)
    except ValueError:
        return payload
    if not isinstance(original, dict) or not isinstance(repaired, dict):
        return payload
    merged = dict(repaired)
    for field in _PLANNER_SEMANTIC_FIELDS:
        if field in original:
            merged[field] = original[field]
    return json.dumps(merged)


_REQUIREMENT_HEADER = (
    "## Authoritative requirement (verbatim)\n\n"
    "The acceptance criteria above summarize this text; where they are vaguer than it, this "
    "text is the contract -- keep every option name, mode, message and behaviour exactly as "
    "written here.\n\n"
)


_BASELINE_HEADER = (
    "## Existing baseline implementation (already in the workspace)\n\n"
    "A baseline pass already implemented this requirement in the workspace before the campaign "
    "started; the plan was drawn up before it existed. Read the workspace first. Complete and "
    "correct the existing implementation against the requirement above; do not rewrite, rename "
    "or remove what already satisfies it, and keep every public name the requirement states.\n\n"
)


_PLAN_MISSION_FIELDS = ("id", "objective", "target_ac_ids", "dependencies", "supersedes", "verification_intent")


def _current_plan(workdir: Path) -> dict[str, Any] | None:
    """The campaign's current plan revision (missions and constraints), if one is on disk."""
    from .verify_shim import primary_checkout

    roots = [workdir]
    primary = primary_checkout(workdir)
    if primary is not None:
        roots.append(primary)
    for root in roots:
        states = sorted(
            (root / ".pact" / "runtime" / "campaigns").glob("*/work-state.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for state_path in states:
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
                plan_ref = state.get("plan_ref")
                if not isinstance(plan_ref, str) or not plan_ref:
                    continue
                plan = json.loads((root / plan_ref).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(plan, dict) and isinstance(plan.get("missions"), list):
                return plan
    return None


def _restore_existing_missions(workdir: Path, payload: str) -> str:
    """Put the current plan's own missions and constraints back into a Planner proposal.

    The runtime rejects a proposal that edits an existing mission or the
    constraints, and a Planner that rewrote a dependency list or reworded an
    objective while adding new missions blocked a whole campaign
    ("cannot mutate existing Mission"). Existing missions are copied back from
    the plan revision on disk field by field; the Planner's new missions and
    operations are left alone.
    """
    try:
        proposal = json.loads(payload)
    except ValueError:
        return payload
    if not isinstance(proposal, dict) or not isinstance(proposal.get("missions"), list):
        return payload
    plan = _current_plan(workdir)
    if plan is None:
        return payload
    canonical = {
        mission["id"]: mission
        for mission in plan["missions"]
        if isinstance(mission, dict) and isinstance(mission.get("id"), str)
    }
    restored: list[Any] = []
    for mission in proposal["missions"]:
        if isinstance(mission, dict) and mission.get("id") in canonical:
            source = canonical[mission["id"]]
            mission = {**mission, **{field: source[field] for field in _PLAN_MISSION_FIELDS if field in source}}
        restored.append(mission)
    proposal["missions"] = restored
    if isinstance(plan.get("constraints"), list):
        proposal["constraints"] = plan["constraints"]
    return json.dumps(proposal)


_REPEATED_NO_PROGRESS = "repeated_no_progress"
_REPLAN_CONSTRAINT = (
    "The blocked mission failed twice in a row without a final output: split it into smaller missions "
    "that each complete within the round budget, keeping its acceptance criteria and dependency order."
)


def _replan_instead_of_retry(prompt: str, payload: str) -> str:
    """Turn a Manager `retry` into `request_replan` when the trigger forbids retrying.

    On `repeated_no_progress` the runtime rejects `retry` as a protocol error,
    and a Manager that kept retrying blocked the campaign after two such
    errors. The decision is rewritten rather than rejected: the reason is kept
    and a replanning constraint added, which is the action the runtime allows.
    """
    if _REPEATED_NO_PROGRESS not in prompt:
        return payload
    try:
        decision = json.loads(payload)
    except ValueError:
        return payload
    if not isinstance(decision, dict) or decision.get("action") != "retry":
        return payload
    decision["action"] = "request_replan"
    decision["selected_mission_id"] = None
    constraints = decision.get("constraints")
    if not isinstance(constraints, list) or not constraints:
        decision["constraints"] = [_REPLAN_CONSTRAINT]
    decision["reason"] = (
        f"{decision.get('reason') or ''} Retry is not allowed on {_REPEATED_NO_PROGRESS}; replanning instead.".strip()
    )
    return json.dumps(decision)


def _staged_file(workdir: Path, name: str) -> str:
    """The newest ``<workdir>/.pact-io/chrys-pact/*/<name>``, from the workdir or its primary checkout."""
    from .verify_shim import primary_checkout

    roots = [workdir]
    primary = primary_checkout(workdir)
    if primary is not None:
        roots.append(primary)
    for root in roots:
        candidates = sorted(
            (root / ".pact-io" / "jiuwenswarm-pact").glob(f"*/{name}"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for candidate in candidates:
            try:
                text = candidate.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if text:
                return text
    return ""


_CLARIFICATION_HEADER = (
    "## Clarified requirement (review authority)\n\n"
    "The clarification below resolved the requirement's ambiguities against the repository. "
    "Judge the change against the requirement as clarified here, not only against the mission's "
    "summary: a public name, mode, message or behaviour the requirement spells out is binding.\n\n"
)


_CONTRACT_HEADER = (
    "## Goal Contract (review authority)\n\n"
    "The campaign's acceptance criteria in full; the mission under review covers some of them.\n\n"
)


def _campaign_context(workdir: Path, role: str = "worker") -> str:
    """Context sections for a Worker or Reviewer prompt.

    Workers get the requirement verbatim and the baseline note. Reviewers judge
    against the goal and the clarification: the clarified requirement (which
    embeds the original), the Goal Contract and the baseline note.
    """
    sections = []
    requirement = _staged_file(workdir, "requirement.md")
    if role == "reviewer":
        clarification = _staged_file(workdir, "clarification.md")
        if clarification:
            sections.append(_CLARIFICATION_HEADER + clarification)
        elif requirement:
            sections.append(_REQUIREMENT_HEADER + requirement)
        contract = _staged_file(workdir, "goal-contract.json")
        if contract:
            sections.append(_CONTRACT_HEADER + "```json\n" + contract + "\n```")
    elif requirement:
        sections.append(_REQUIREMENT_HEADER + requirement)
    baseline = _staged_file(workdir, "baseline.md")
    if baseline:
        sections.append(_BASELINE_HEADER + baseline)
    return "\n\n".join(sections)


def _is_json_object(text: str) -> bool:
    try:
        parse_unique_json_object(text)
    except ValueError:
        return False
    return True


def _protocol_payload(text: str) -> str:
    """Unwrap one complete protocol object without selecting an ambiguous fragment.

    The original Chrys PACT seam accepts fences and prose around JSON. Use the
    shared strict transport decoder for these same forms, without repairing
    payload bytes. Invalid text stays available for diagnostics; role adapters
    must reject it before pact-core's less-strict JSON decoder can accept it.
    """
    try:
        payload = parse_unique_json_object(text)
    except ValueError:
        return text
    return json.dumps(payload, ensure_ascii=False, allow_nan=False)


def _unfenced(text: str) -> str:
    """Return the body of a response that is exactly one Markdown code fence.

    The Manager and Planner protocols want a bare JSON object; models keep
    wrapping it in ```json anyway, and the first live campaign was blocked
    with ``manager_protocol_error`` on a decision that was otherwise correct.
    A response with anything outside the fence is left alone.
    """
    match = _FENCED_WHOLE.match(text)
    return match.group(1) if match else text
