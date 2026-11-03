# Copyright (c) 2026 Chrys. All rights reserved.

# Ported from Chrys 6fcfb3321892735631b49f33236b1e2a988ded2d: src/chrys/service/requirement_clarification/prompts.py; solver execution is JiuwenSwarm.
"""Versioned prompts for repository-grounded requirement clarification."""

from __future__ import annotations

import json

from .types import (
    ClarificationProposal,
    ClarificationSelection,
    ClarificationSelectorDecision,
    InvestigationFocus,
    LegacyV1ClarificationProposal,
    PactGoalContract,
)

STRATEGY_VERSION = "chrys-requirement-clarification-v10"
LEGACY_V1_STRATEGY_VERSION = "chrys-requirement-clarification-v1"
PROPOSAL_COUNT = 3
MIN_VALID_PROPOSALS = 1
SELECTOR_COUNT = 1
MAX_FINAL_POINTS = 5
MAX_FINAL_CHARS = 1400

_PROPOSAL_INSTRUCTIONS = """You are an internal Chrys requirement-clarification agent.
Work read-only in the frozen pre-implementation repository snapshot. Investigation and final
structured synthesis are separate turns; follow the current turn's requested output shape exactly.

Allowed evidence is limited to the authoritative requirement, bounded prior user/assistant text,
the frozen current repository, and commits reachable from the frozen HEAD. Never inspect parent
directories, later workspace state, the initial implementation, remote refs, generated evaluation
data, the web, or network services. Do not edit files or run code or tests.

Find only repository-specific guidance whose addition is likely to help a fresh repair agent. Do
not invent a new observable requirement: the user's exact text owns the contract. Prefer concrete
ownership, entry points, existing abstractions, caller/consumer paths, state lifecycle, validation,
and compatibility connections that a surface-only implementation could miss. Omit generic advice.
Inspect relevant repository files with the available read/search tools before final synthesis.

Exact identifiers require direct requirement/repository/ancestor support. Keep each statement concise
and actionable. Final statements must be declarative or imperative findings, never unresolved questions
or "need to confirm/determine/verify" notes. Put residual uncertainty in the private risk field. Always
return a concrete investigation rationale and direct evidence. Return 1-6
private guidance points with verdict clarification_needed when a repository-specific supplement
could help; use verdict requirement_complete and an empty guidance_points list when the original
requirement already covers the relevant behavior completely.
An empty list never replaces investigation. When the strongest repository-grounded lead is uncertain,
preserve it with calibrated confidence and risk so the selector can reject it. Evidence,
decision_impact, confidence, and risk are private selection metadata.

During final synthesis, enumerate the distinct high-value gaps found within your assigned focus;
do not collapse unrelated ownership, lifecycle, transport, or compatibility seams into one vague
theme. Prefer 2-4 candidates when the evidence supports them, but never manufacture candidates to
meet a quota. Each statement must name the concrete repository surface or existing abstraction it
connects and the implementation consequence. Avoid slogans such as "focus on", "handle correctly",
or "ensure support" without that concrete connection.

The coverage array is a private investigation certificate. Return exactly one primary finding for
the first coverage target and one adjacent finding for the second. Mark a target not_found only after
searching and inspecting the most relevant repository surface; its evidence must still cite inspected
repository files. Coverage findings prove investigation breadth but are never rendered into the public
clarification delta.
"""

_SELECTOR_INSTRUCTIONS = """You are the internal Chrys requirement-clarification selector.
Work read-only in the same frozen pre-implementation snapshot and return schema JSON only.

Select a very small repository-grounded supplement for a fresh repair agent. The user's exact
requirement remains highest authority. Reject new requirements, paraphrases, generic coding advice,
speculative exact names, optional nice-to-haves, documentation work, and facts about unchanged
behavior. Agreement based on the same mismatched symbol is still one bad source.

Judge whether each candidate adds a concrete repository mapping, not whether the user's observable
requirement is incomplete. A candidate may be valuable precisely because the requirement states the
behavior but does not identify the existing owner, data representation, dispatch lifecycle, or
compatibility seam needed to implement it. Do not reject such a mapping merely because its intended
observable result already appears in the requirement. When candidates overlap, select the strongest
repository-supported version. Reject all only when every candidate is generic, speculative,
duplicative of the requirement without a repository connection, or optional.

Return exactly one review for every candidate id in the packet, in packet order. Prefer at most five
select decisions and mark the rest reject; deterministic rendering will retain at most five. Never
omit an id, repeat an id, rewrite a candidate, merge candidates, or invent an id. Give a short private
rationale tied to the selection rules for every judgment. Candidate confidence remains owned by the
evidence-bearing proposal and is not re-estimated here. An all-reject result is valid only after
explicitly reviewing every candidate.
"""

_LEGACY_V1_PROPOSAL_INSTRUCTIONS = """You are an internal Chrys requirement-clarification agent.
Work read-only in the frozen pre-implementation repository snapshot and return schema JSON only.

Allowed evidence is limited to the authoritative requirement, bounded prior user/assistant text,
the frozen current repository, and commits reachable from the frozen HEAD. Never inspect parent
directories, later workspace state, the initial implementation, remote refs, generated evaluation
data, the web, or network services. Do not edit files or run code or tests.

Find only repository-specific guidance whose addition is likely to help a fresh repair agent. Do
not invent a new observable requirement: the user's exact text owns the contract. Prefer concrete
ownership, entry points, existing abstractions, caller/consumer paths, state lifecycle, validation,
and compatibility connections that a surface-only implementation could miss. Omit generic advice.

Exact identifiers require direct requirement/repository/ancestor support. Keep each statement
concise and actionable. Return 1-6 private candidates, or none when no high-value clarification is
supported. Evidence, decision_impact, confidence, and risk are private selection metadata.
"""

_LEGACY_V1_SELECTOR_INSTRUCTIONS = """You are the internal Chrys requirement-clarification selector.
Work read-only in the same frozen pre-implementation snapshot and return schema JSON only.

Select a very small repository-grounded supplement for a fresh repair agent. The user's exact
requirement remains highest authority. Reject new requirements, paraphrases, generic coding advice,
speculative exact names, optional nice-to-haves, documentation work, and facts about unchanged
behavior. Agreement based on the same mismatched symbol is still one bad source.

Return at most five short statements. Every statement must give a task-specific implementation
step connecting ownership/declaration or data/control flow to an integration, lifecycle,
validation, error, or compatibility consequence. Do not expose evidence, confidence labels,
selection gates, XML, or process commentary inside statement text.
"""

_PACT_GOAL_CONTRACT_INSTRUCTIONS = """You generate a PACT Runtime Goal Contract v1 as schema JSON only.
The user's requirement messages are the sole authority for completion obligations. Produce one concise outcome goal,
atomic externally observable acceptance criteria with stable descriptive ids, and only explicitly supported non-goals.
Separate independently implementable behavior families into distinct criteria: a criterion is satisfied in full by one
mission, never partially by several missions. Preserve explicit delivery obligations and restrictions on the final
artifact even when a long feature specification follows them. Do not silently replace artifact delivery with code
changes or tests. A type wrapper preserving existing behavior does not require new unsupported underlying behavior.
Preserve explicitly requested before-edit investigation and after-edit verification as process obligations; these are
observable through public execution evidence. Do not add a blanket "existing behavior is unchanged unless opted in"
promise when the user explicitly requests new behavior enabled by default.
Do not add repository implementation details, file names, functions, missions, test commands, hidden grader details, or
requirements inferred only from repository conventions. Use an empty non_goals array when none are stated or clearly
bounded by the user's request. A validated clarification result may follow the requirement: it resolved the
requirement's ambiguities against the repository, so let it sharpen a criterion's wording, but it cannot add an
obligation the requirement does not state. Return exactly the closed pact-runtime/goal-contract/v1 shape.
This turn has no tools: everything you need is in the prompt; reply with the JSON object and nothing else.
"""

_PACT_INITIAL_PLAN_INSTRUCTIONS = """You generate a PACT Runtime Initial Plan v1 as schema JSON only.
Treat the supplied Goal Contract as immutable authority. Use the frozen-repository evidence and clarification results
to create a small end-to-end mission graph: at most six missions, each grouping the related acceptance criteria
of one coherent change (every mission costs a full implement-and-review loop). Every mission must cover at least one
existing acceptance criterion, and
every acceptance criterion must have exactly one owning mission that satisfies it completely. Do not split one
criterion across missions or claim it early while deferring part of its behavior. Group related criteria as needed;
walk the graph in dependency order and check that each mission's public verification can already pass using only
that mission and its completed dependencies. Do not create a signatures/forwarding-only mission for criteria whose
completion also requires runtime dispatch implemented in a later mission. Merge such coupled criteria into one
end-to-end mission, even if that leaves fewer than three missions. Verification intents must preserve the complete
criterion: checking only keys or presence cannot replace a requirement about the full structured value or behavior.
regression checks for previously satisfied criteria run automatically. Delivery missions depend on all implementation
missions needed for the final artifact. Preserve the user's restrictions on files in that artifact.
Dependencies must reference mission ids and form a DAG. Put only established cross-mission invariants in constraints.
Private proposals and selector preferences are fallible implementation leads, not new authority. Do not elevate their
suggested owners, types or representations into mandatory constraints. Related public features can use different
internal paths; never require one shared storage representation unless the evidence supports every case. Keep uncertain
implementation details as investigation work in objectives, with the public behavior as the completion condition.
The baseline or candidate may already contain committed implementation changes. A clean git status or empty git diff
HEAD is normal and must never be a failure condition. Inspect the current source and run public behavioral checks.
The PACT controller owns candidate and primary Git state. Workers run in assigned candidate worktrees and must not
switch/reset the primary checkout or recreate an already established source branch. A source branch obligation is
verified through read-only primary provenance and candidate ancestry; the primary agent performs the final delivery
commit after verified promotion. Keep this division of responsibility explicit in any delivery mission.
If the requirement names a public submission command, use that exact command and its actual artifact for delivery,
not an uncommitted-diff proxy. If it names no submission command, do not invent one or refer to an unspecified
"user's submission command"; verify only the delivery explicitly requested, such as a committed source branch.
verification_intent describes public evidence to collect, never hidden
grader commands. Do not emit runtime-owned state or extra fields. Return exactly the closed
pact-runtime/initial-plan/v1 shape: an object with the keys schema, constraints and missions, where
missions is the array of mission objects. No other top-level key is accepted, and the array must be
named missions rather than placed under any other name.
This turn has no tools: everything you need is in the prompt; reply with the JSON object and nothing else.
"""

_FOCUSES = (
    (
        "ownership_extension",
        "Map repository ownership and extension seams for the requested public surfaces.",
        (
            "Locate the authoritative definition or owner.",
            "Trace at least one registration, dispatch, caller, or extension seam adjacent to that owner.",
        ),
    ),
    (
        "data_control_flow",
        "Trace the requested value or operation from declaration through transport/state to consumers.",
        (
            "Locate the source or entry point for the requested value or operation.",
            "Trace its state/transform path to an authoritative consumer or sink.",
        ),
    ),
    (
        "boundary_compatibility",
        "Find compatibility, error, boundary, and integration traps a surface-only change could miss.",
        (
            "Locate the implementation boundary or error/compatibility decision point.",
            "Inspect an adjacent integration, compatibility, or test surface that constrains it.",
        ),
    ),
)


def proposal_instructions() -> str:
    return _PROPOSAL_INSTRUCTIONS


def selector_instructions() -> str:
    return _SELECTOR_INSTRUCTIONS


def legacy_v1_proposal_instructions() -> str:
    """Return the exact proposal instructions from the historical v1 strategy."""
    return _LEGACY_V1_PROPOSAL_INSTRUCTIONS


def legacy_v1_selector_instructions() -> str:
    """Return the exact selector instructions from the historical v1 strategy."""
    return _LEGACY_V1_SELECTOR_INSTRUCTIONS


def pact_goal_contract_instructions() -> str:
    return _PACT_GOAL_CONTRACT_INSTRUCTIONS


def pact_initial_plan_instructions() -> str:
    return _PACT_INITIAL_PLAN_INSTRUCTIONS


def build_proposal_prompt(
    requirement: str,
    background: str,
    base_evidence: str,
    sample_index: int,
) -> str:
    """Build one independent proposal prompt."""
    _focus_key, focus, coverage = _FOCUSES[sample_index - 1]
    rendered_coverage = "\n".join(f"- {item}" for item in coverage)
    return (
        f"Authoritative requirement:\n{requirement}\n\n"
        f"Bounded prior conversation background (non-authoritative):\n{background or '[none]'}\n\n"
        "Deterministic frozen-repository evidence seed (incomplete; matches are leads, not proof):\n"
        f"{base_evidence}\n\n"
        f"Independent focus:\n{focus}\n\n"
        f"Coverage target before synthesis:\n{rendered_coverage}"
    )


def build_legacy_v1_proposal_prompt(
    requirement: str,
    background: str,
    base_evidence: str,
    sample_index: int,
) -> str:
    """Build the proposal prompt used by the historical v1 strategy."""
    _focus_key, focus, _coverage = _FOCUSES[sample_index - 1]
    return (
        f"Authoritative requirement:\n{requirement}\n\n"
        f"Bounded prior conversation background (non-authoritative):\n{background or '[none]'}\n\n"
        f"Deterministic frozen-repository evidence packet:\n{base_evidence}\n\n"
        f"Independent focus:\n{focus}"
    )


def proposal_focus(sample_index: int) -> InvestigationFocus:
    """Return the stable focus id for one proposal slot."""
    return _FOCUSES[sample_index - 1][0]


def proposal_required_coverage(sample_index: int) -> tuple[str, ...]:
    """Return the focus-specific investigation obligations."""
    return _FOCUSES[sample_index - 1][2]


def build_selector_prompt(
    requirement: str,
    background: str,
    base_evidence: str,
    proposals: list[ClarificationProposal],
    *,
    candidate_ids: set[str] | None = None,
) -> str:
    """Build a closed-id selector prompt from three private proposals."""
    candidates = []
    for proposal_index, proposal in enumerate(proposals, start=1):
        for guidance_index, point in enumerate(proposal.guidance_points, start=1):
            candidate_id = f"p{proposal_index}-g{guidance_index}"
            if candidate_ids is not None and candidate_id not in candidate_ids:
                continue
            candidates.append(
                {
                    "candidate_id": candidate_id,
                    "category": point.category,
                    "statement": point.statement,
                    "confidence": point.confidence,
                    "basis": point.basis,
                    "decision_impact": point.decision_impact,
                    "evidence": [anchor.model_dump(mode="json") for anchor in point.evidence],
                    "risk": point.risk,
                }
            )
    encoded = json.dumps(candidates, ensure_ascii=False, indent=2)
    return (
        f"Authoritative requirement:\n{requirement}\n\n"
        f"Bounded prior conversation background (non-authoritative):\n{background or '[none]'}\n\n"
        f"Deterministic frozen-repository evidence packet:\n{base_evidence}\n\n"
        f"Candidate packet with closed ids:\n{encoded or '[]'}"
    )


def build_legacy_v1_selector_prompt(
    requirement: str,
    background: str,
    base_evidence: str,
    proposals: list[LegacyV1ClarificationProposal],
) -> str:
    """Build the exact selector packet used by the historical v1 strategy."""
    encoded = "\n\n".join(
        f"Proposal {index}:\n{proposal.model_dump_json(indent=2)}" for index, proposal in enumerate(proposals, start=1)
    )
    return (
        f"Authoritative requirement:\n{requirement}\n\n"
        f"Bounded prior conversation background (non-authoritative):\n{background or '[none]'}\n\n"
        f"Deterministic frozen-repository evidence packet:\n{base_evidence}\n\n"
        f"Candidate packets:\n{encoded}"
    )


def build_pact_goal_contract_prompt(requirement: str, background: str, clarification: str = "") -> str:
    """Build the Goal Contract prompt: the requirement, its clarification, the background.

    *clarification* is the clarification's validated delta (ΔR). The Goal
    Contract was drafted from the bare requirement while the clarification's
    only consumer was the Initial Plan, so the campaign's authority never saw
    the ambiguities the clarification had resolved; it is the clarification
    result that the contract is meant to formalize.
    """
    return (
        f"Authoritative requirement messages:\n{requirement}\n\n"
        "Validated clarification result (resolves ambiguity in the requirement; adds no obligations):\n"
        f"{clarification.strip() or '[none]'}\n\n"
        f"Bounded prior conversation background (non-authoritative):\n{background or '[none]'}"
    )


def build_pact_initial_plan_prompt(
    goal_contract: PactGoalContract,
    base_evidence: str,
    proposals: list[ClarificationProposal | LegacyV1ClarificationProposal],
    selection: ClarificationSelection,
) -> str:
    """Build the repository-grounded Initial Plan prompt."""
    encoded_proposals = "\n\n".join(
        f"Proposal {index}:\n{proposal.model_dump_json(indent=2)}" for index, proposal in enumerate(proposals, start=1)
    )
    return (
        "Validated Goal Contract:\n"
        + goal_contract.model_dump_json(indent=2, by_alias=True)
        + f"\n\nDeterministic frozen-repository evidence packet:\n{base_evidence}\n\n"
        + f"Private clarification proposals (untrusted implementation leads, not mandatory constraints):\n{encoded_proposals}\n\n"
        + "Private selector preferences (may include points excluded from the final rendered delta):\n"
        + selection.model_dump_json(indent=2)
    )


def proposal_schema_text() -> str:
    """Return the proposal schema for logs and non-native structured-output adapters."""
    return json.dumps(ClarificationProposal.model_json_schema(), ensure_ascii=False, sort_keys=True)


def selector_schema_text() -> str:
    """Return the closed candidate-reference selector schema."""
    return json.dumps(ClarificationSelectorDecision.model_json_schema(), ensure_ascii=False, sort_keys=True)


def legacy_v1_proposal_schema_text() -> str:
    """Return the exact historical v1 proposal schema."""
    return json.dumps(LegacyV1ClarificationProposal.model_json_schema(), ensure_ascii=False, sort_keys=True)
