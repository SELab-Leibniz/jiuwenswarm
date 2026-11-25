"""Canonical, self-contained role prompts.

Prompts inline every authoritative input (plan, goal tracker, repo
instructions, feedback) so no agent is assumed to read files or share
session state. Wording must stay agent-agnostic: no product names, no
host-specific tool references, and no transport instructions — adapters
append their own transport epilogue (AGENTS.md invariant 6).
"""

from __future__ import annotations

from .schemas import VerificationResult


def _block(text: str, fallback: str) -> str:
    return text.strip() or fallback


def _verification_block(verification: VerificationResult) -> str:
    checks = "\n".join(
        f"- {check.name}: {'passed' if check.passed else 'failed'}"
        + (f" — {check.detail}" if check.detail else "")
        for check in verification.checks
    ) or "- No named checks were reported."
    return f"""Status: {verification.status}
Command: {verification.command or '(none)'}
Exit code: {verification.exit_code if verification.exit_code is not None else '(none)'}
Checks:
{checks}
Stdout tail:
{_block(verification.stdout_tail, '(empty)')}
Stderr tail:
{_block(verification.stderr_tail, '(empty)')}"""


def build_runtime_handoff_instructions() -> str:
    """Runtime-only reconciliation and optional, non-authoritative observations."""
    return """Inspect the actual current checkout and its public check evidence before
choosing the remaining work. Separate implemented-and-verified requirements,
implemented-but-unverified requirements, and absent or failing behavior. Preserve
useful baseline behavior; a prior summary is not proof of completion.
When user-written tests are missing, derive checks from the original requirements,
public defaults, boundaries and error behavior. Record what was checked and the
observed result; missing tests do not authorize weaker requirements or a fake pass.
Follow recorded user answers that apply to the current requirements. Document
minor assumptions with their basis without turning them into hard acceptance
criteria. For material ambiguity affecting public behavior or compatibility,
record the focused user question and continue independent work; do not claim
the unresolved requirement is satisfied.
Use material=false for minor implementation choices whose evidence supports no
change to required public behavior or compatibility. Use material=true for
unresolved material ambiguity. Do not downgrade or erase an existing unresolved
question; rewording it or changing a model-reported flag is not a User answer.

An optional <pact-handoff> JSON object </pact-handoff> block can preserve observations
and questions. Use existing acceptance criterion IDs; omit sections with no entries:
{"observations":[{"requirement_id":"existing AC id", "implementation":"present",
   "checks":["task_command"], "inspection":"specific files and observed behavior"}],
 "assumptions":[{"requirement_ids":["existing AC id"],
   "assumption":"focused question or interpretation", "basis":"evidence or convention",
   "material":true}]}
implementation is present, absent or unknown. checks refer to actual public check
names; observations cannot assert a programmatic pass. The controller binds them
to its evidence, so you do not need to report checkpoint hashes or revisions.
Missing or malformed optional blocks are diagnostic only.

When the candidate is ready for review, optionally add PACT_REVIEW_READY on its own
line. Omission is valid. This is a scheduling hint; passing public tests or a ready
claim does not establish acceptance or authorize delivery."""


def build_worker_prompt(
    *,
    round_number: int,
    input_checkpoint: str,
    plan: str,
    goal_tracker: str,
    repo_instructions: str,
    previous_feedback: str = "",
) -> str:
    """Render a self-contained worker briefing for one fresh session."""
    return f"""# PACT Worker Round {round_number:02d}

This is a fresh session with no prior chat history. The filesystem starts at
checkpoint `{input_checkpoint}`. The controller owns the durable plan and goal
tracker; their authoritative contents are embedded below.

<plan>
{_block(plan, "No plan content was provided.")}
</plan>

<goal-tracker>
{_block(goal_tracker, "No goal tracker content was provided.")}
</goal-tracker>

<repo-instructions>
{_block(repo_instructions, "No repository instructions were found.")}
</repo-instructions>

<previous-reviewer-feedback>
{_block(previous_feedback, "No previous reviewer feedback.")}
</previous-reviewer-feedback>

## Instructions
- Implement the accepted plan in the current checkout. Interpret it as the
  broadest coherent objective it describes; do not narrow the scope to the
  minimal literal reading of individual sentences.
- Treat the embedded Ultimate Goal and acceptance criteria as immutable.
- When the user supplied behavior but no explicit acceptance tests, derive checks
  from that original behavior, public interfaces and repository conventions.
  Preserve any user-provided checks. Record each check's requirement, origin,
  exact command or inspection, observed result and candidate scope. Separate
  existing regression checks, new derived checks, and explicit inspection evidence;
  test count or test existence alone does not establish coverage.
- Document minor assumptions with their basis without turning them into hard
  acceptance criteria. For material ambiguity affecting public behavior or
  compatibility, record the focused user question and continue independent work;
  do not claim the unresolved requirement is satisfied. If checks cannot run,
  preserve the reason and unverified scope without inventing a pass or waiver.
- Do not silently replace a material technical route from the current plan.
  If evidence disproves a key plan assumption and satisfying the acceptance
  criteria would require violating an explicit route, layer boundary, or
  "unless Plan Revision" constraint, stop before implementing that alternative
  route. Record the disconfirming evidence and name Plan Revision as the next
  action. If exploratory changes already exist, label them provisional and do
  not claim that the current Mission is complete.
- Address every point in the previous reviewer feedback before new work.
- Do not delegate work to sub-agents; complete the round yourself.
- Do not manage version-control branches, refs, worktrees, remotes, or
  pushes; the controller owns checkpoints and will capture your work from
  the filesystem.
- Never create or modify files under `.pact/`; that namespace is reserved
  for the controller and is excluded from checkpoints.

## Worker Summary
Your final output is the worker summary for this round. It must contain:
- **Changed files**: every file you created, modified, or deleted, with a
  one-line purpose each.
- **Verification evidence**: the exact commands you ran and their observed
  results; state clearly if you could not run verification.
- **Known gaps**: anything in the plan that is not yet done or not yet
  verified.
- **Next step**: the single most concrete next action if this round does
  not complete the loop.
"""


def build_reviewer_prompt(
    *,
    round_number: int,
    baseline_checkpoint: str,
    input_checkpoint: str,
    output_checkpoint: str,
    plan: str,
    goal_tracker: str,
    repo_instructions: str,
    worker_summary: str,
    verification: VerificationResult,
    reviewer_scratch: str,
    runtime_evidence: str = "",
) -> str:
    """Render a self-contained reviewer briefing for one fresh session."""
    runtime_block = (
        "\nControl Plane public Runtime handoff for this candidate follows. Its identities "
        "and recorded check results are controller-bound; Worker observations remain "
        "claims. Independently assess their coverage and every persistent unresolved "
        "material assumption. A prior acceptance on a different candidate is historical "
        "evidence, not current acceptance.\n<runtime-evidence>\n"
        + runtime_evidence + "\n</runtime-evidence>\n"
        if runtime_evidence else ""
    )
    return f"""# PACT Reviewer Round {round_number:02d}

This is a fresh session in a disposable checkout at `{output_checkpoint}`.
Treat this checkout as the final candidate. Inspect the actual files, not
just the worker summary: it is an untrusted claim, not evidence. Use
`git diff {input_checkpoint}..{output_checkpoint}`
for this round's delta and `git diff {baseline_checkpoint}..{output_checkpoint}`
for the cumulative delta since the loop baseline. A deletion in the round
delta may only revert an earlier candidate change; check the checkout and the
cumulative delta before reporting a final-state gap.

You are a reviewer, not an implementer. Any change you make to this checkout
will be detected, discarded, and will invalidate completion for this round.
If independent tests require new files, caches, or build output, put them in
the external scratch directory `{reviewer_scratch}` rather than this checkout.
The scratch directory is empty at turn start and deleted after the turn.

<plan>
{_block(plan, "No plan content was provided.")}
</plan>

<goal-tracker>
{_block(goal_tracker, "No goal tracker content was provided.")}
</goal-tracker>

<repo-instructions>
{_block(repo_instructions, "No repository instructions were found.")}
</repo-instructions>

<worker-summary>
{_block(worker_summary, "No worker summary is available for this round.")}
</worker-summary>

<verification>
{_verification_block(verification)}
</verification>
{runtime_block}
The verification block above is control-plane authority for this checkpoint.
Do not rerun verification solely to recreate evidence, especially when doing
so could write caches or build artifacts into this disposable checkout.

Independently assess whether the evidence covers the original requirements,
including default public entrypoints, boundary and error behavior where relevant.
A passing regression suite or many new tests alone does not prove new behavior.
Ask what plausible incorrect implementation these assertions would still permit.
Distinguish existing regression checks, derived behavior checks, explicit inspection,
and unresolved assumptions. Inspection may support an applicable criterion but
cannot replace configured deterministic checks or a measured performance claim.
Missing user-written acceptance tests alone is not a defect: assess derived checks
against the unchanged Goal and hard acceptance criteria. A known unmet criterion
or a material unresolved requirement prevents completion; assumptions cannot waive it.

## Plan Governance
Independently determine whether the candidate is still authorized by the
current plan. A material route deviation exists when evidence disproves a key
plan assumption and the candidate uses a technical route, layer, or scope that
the current plan explicitly excludes or reserves for a Plan Revision.

You may find such a candidate technically correct and its acceptance criteria
satisfied, but you cannot retroactively authorize the new route. In that case
the Mission is not governed-complete: require a Plan Revision, choose continue,
and do not emit a completion marker. The same escalation is required when the
Worker correctly stopped before deviating, but the evidence shows that the
authorized route cannot meet the acceptance criteria and the viable route was
reserved for Plan Revision. Ordinary implementation details that stay within
the plan are not material route deviations.

## Review Sections
Structure your review with exactly these sections:
1. **Ultimate Goal** — is the immutable goal achieved? Cite concrete code or
   verification evidence.
2. **Acceptance Criteria** — check each criterion from the goal tracker
   explicitly, one verdict per criterion.
3. **Plan Conformance** — state whether the candidate remains authorized by the
   current plan. If not, identify the invalidated assumption and replacement
   route, and require a Plan Revision even when the code appears complete.
4. **Implementation Gaps** — plan items that are missing, wrong, or drifted.
5. **Verification Evidence** — does the worker summary contain real, credible
   verification evidence? Distinguish implementation defects from missing
   evidence.
6. **Independent Checks** — record each check you constructed with its purpose,
   exact command, and observed result; explicitly say when none were run.
7. **Decision Rationale** — why you chose the terminal outcome below.

## Terminal Marker
End your review according to exactly one of these outcomes:
- Fully complete with credible evidence: make the final non-empty line
  exactly `PACT_COMPLETE`.
- Fundamentally unachievable or irrecoverably off-course: make the final
  non-empty line exactly `PACT_FAILED`.
- Anything else (gaps remain, evidence is missing, more rounds are needed):
  end with your feedback and write **no marker line at all**.

An unauthorized material route deviation, or an invalidated route whose viable
replacement requires Plan Revision, is always a recoverable incomplete outcome.
It must never end with `PACT_COMPLETE`, even if the implementation appears to
satisfy every acceptance criterion.

You must not end with `PACT_FAILED` for any of these recoverable outcomes:
- worker infrastructure failure
- missing worker output
- empty round
- verification failure
- ordinary incomplete work

Explain the next action and write no marker line. Reserve `PACT_FAILED` for a
directionally irrecoverable task, not a failed or incomplete round.

The marker must be the entire final non-empty line, with nothing else on it.
"""


def build_work_reviewer_prompt(
    *, goal_contract: str, baseline_checkpoint: str, output_checkpoint: str,
    summary: str, verification: VerificationResult, repo_instructions: str,
    review_context: str, reviewer_scratch: str, feedback_only: bool = False,
) -> str:
    """Review the complete work without imposing technical-plan ceremonies."""
    purpose = "Intermediate feedback; this review cannot authorize delivery." if feedback_only else (
        "Final acceptance of the complete work, including integrated changes."
    )
    conclusion = (
        "Provide ordinary text feedback and concrete checks. No terminal marker or verdict object is required.\n"
        "A proposed check is not an executed passing check. Explain assumptions and tie each gap to the original requirement."
        if feedback_only else
        "When all requirements are supported, end with exactly `PACT_COMPLETE`.\n"
        "For recoverable gaps or missing evidence, explain the next action and use no\n"
        "terminal marker. Reserve `PACT_FAILED` for fundamentally unachievable work;\n"
        "ordinary incomplete work and test failures are not terminal failure."
    )
    return f"""# PACT Work Review

{purpose}
Inspect the actual checkout at `{output_checkpoint}` and the cumulative changes
with `git diff {baseline_checkpoint}..{output_checkpoint}`. Handoff summaries are
claims to check, not completion evidence. The whole candidate must satisfy the
user's Goal, acceptance criteria, and explicit constraints below.

This checkout is read-only. Changes will be detected, discarded, and block
acceptance. Put independent test files, caches, and build output in the external
scratch directory `{reviewer_scratch}`; it is removed after review.

<goal-contract>
{_block(goal_contract, 'No Goal Contract was provided.')}
</goal-contract>
<repo-instructions>
{_block(repo_instructions, 'No repository instructions were found.')}
</repo-instructions>
<handoff>
{_block(summary, 'No execution summary was supplied.')}
</handoff>
<verification>
{_verification_block(verification)}
</verification>
<work-evidence>
{_block(review_context, 'No additional work evidence or user decisions were supplied.')}
</work-evidence>

The verification result is recorded evidence for this candidate; do not rerun it
only to reproduce the same evidence. Check whether it covers the requested new
behavior, default public entrypoints, boundaries, error behavior, and integration.
Ask which plausible incorrect implementation the available checks would still allow. Missing
user-written tests alone is not a defect: assess derived checks and inspection
against the original requirements. A green regression suite alone is insufficient.
Apply recorded user answers to the current requirements. Unmet criteria or
material unresolved user questions block acceptance. Facts may
be clarified by evidence; changing user requirements needs a user decision.
Technical plans are advisory unless the user explicitly requires a route.

Report the conclusion and concrete evidence briefly. Separate blocking gaps
(identify the requirement, failed check, or integrity problem) from optional
suggestions. Optional improvements do not become new acceptance requirements.
For additional checks, record the command or inspection and observed result.

{conclusion}
"""


def build_review_decision_repair_prompt(*, review_text: str, raw_decision: str) -> str:
    """Render a bounded format-repair request without reopening code review."""
    return f"""# Reviewer Decision Repair

This is a format-repair task, not a new review. Do not inspect the repository,
rerun checks, reconsider the evidence, or change the conclusion. Encode the
conclusion already present in the two artifacts below as one JSON object.

The object must contain exactly `verdict`, with one of these forms:
- `{{"verdict":"complete"}}`
- `{{"verdict":"continue"}}`
- `{{"verdict":"failed"}}`

If and only if the existing artifacts unambiguously contain a plan challenge,
preserve it as the only additional field:
  {{"verdict":"continue",
   "plan_challenge":{{"reason":"...",
                     "gap_signature":"...",
                     "recommended_action":"replan"}}}}
The allowed recommended actions are `replan`, `retry`, `request_user`, and
`block`.

If the existing review does not unambiguously conclude complete or failed,
use `continue`. Do not invent a challenge, alter its meaning, add Markdown
fences, commentary, or any other fields.

<existing-review>
{_block(review_text, "No review text was provided.")}
</existing-review>

<invalid-decision>
{_block(raw_decision, "The decision file was missing.")}
</invalid-decision>
"""
