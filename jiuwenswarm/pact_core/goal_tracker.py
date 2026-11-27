"""PACT durable goal tracker helpers.

Ported from chrys ``goal_tracker.py``. The immutable block hash covers only
the marked region, so controller-owned mutable progress never invalidates
the plan anchor.
"""

from __future__ import annotations

import hashlib

IMMUTABLE_START = "<!-- PACT:IMMUTABLE:START -->"
IMMUTABLE_END = "<!-- PACT:IMMUTABLE:END -->"


def sha256_text(text: str) -> str:
    """Return the SHA-256 digest for UTF-8 text."""
    return hashlib.sha256(text.encode("utf-8", errors="surrogateescape")).hexdigest()


def build_initial_goal_tracker(plan: str) -> str:
    """Render the initial goal tracker with immutable and mutable sections."""
    plan_hash = sha256_text(plan)
    return f"""# Goal Tracker

{IMMUTABLE_START}
## IMMUTABLE

### Ultimate Goal
Implement the accepted plan without drifting from its stated objective.

### Accepted Plan
SHA256: {plan_hash}

### Acceptance Criteria
- [AC1] The observable behavior described by the accepted plan is implemented.
- [AC2] Verification evidence is recorded in the worker summary.
{IMMUTABLE_END}

## MUTABLE

### Round Progress
- Round 01: Not started.
"""


def immutable_goal_block(text: str) -> str:
    """Extract the immutable goal block, including its markers."""
    start_index = text.find(IMMUTABLE_START)
    end_index = text.find(IMMUTABLE_END)
    if start_index < 0 or end_index < 0 or end_index < start_index:
        raise ValueError("goal tracker is missing immutable markers")
    return text[start_index : end_index + len(IMMUTABLE_END)]


def immutable_goal_sha256(text: str) -> str:
    """Return the checksum for the immutable goal block only."""
    return sha256_text(immutable_goal_block(text))


def record_round_progress(
    text: str,
    *,
    round_number: int,
    checkpoint: str,
    gate_status: str,
    summary_ref: str,
    review_ref: str,
) -> str:
    """Record controller-owned round progress without changing the immutable block."""
    pending = f"- Round {round_number:02d}: Not started."
    completed = (
        f"- Round {round_number:02d}: checkpoint `{checkpoint}`; gate `{gate_status}`; "
        f"summary `{summary_ref}`; review `{review_ref}`."
    )
    updated = text.replace(pending, completed, 1) if pending in text else f"{text.rstrip()}\n{completed}\n"
    if gate_status == "forced_continue":
        next_pending = f"- Round {round_number + 1:02d}: Not started."
        if next_pending not in updated:
            updated = f"{updated.rstrip()}\n{next_pending}\n"
    return updated
