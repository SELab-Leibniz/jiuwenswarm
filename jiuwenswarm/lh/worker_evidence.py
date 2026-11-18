# Copyright (c) 2026 Chrys. All rights reserved.

"""Advisory Worker guidance; no question classification or resolution authority."""

from __future__ import annotations

WORKER_EVIDENCE_GUIDANCE_VERSION = "worker-evidence-triage/v1"


def worker_evidence_guidance() -> str:
    return (
        "\n\n## Worker evidence triage (advisory; not user authority)\n"
        "Before adding a NEW handoff assumption, locate the exact original requirement and relevant public inputs. "
        "Distinguish an evidence-resolvable factual claim, a permitted implementation choice, and an unresolved "
        "requirement conflict or user-only permission decision. Investigate facts using those inputs and executable "
        "behavioral checks; record the exact basis, observed result and missing evidence. A convention, your own "
        "interpretation, or passing health/self-written checks alone cannot settle required public behavior.\n"
        "Only a supported implementation choice that leaves every required public behavior unchanged may be "
        "documented as a nonmaterial assumption. Preserve unresolved material ambiguity when public evidence does "
        "not settle behavior, requirements conflict, or user authority is needed. No interactive user does not grant "
        "permission to choose an unsupported answer or weaken a hard requirement. Continue independent work.\n"
        "Existing unresolved questions, their IDs and material flags remain unchanged without an explicit user "
        "answer or Core-supported authority. Do not delete, reword, downgrade or mark them resolved in a handoff. "
        "A later Reviewer statement is not a user decision. Evidence and implementation choices remain advisory; "
        "Core still owns questions, verification, acceptance and delivery.\n"
    )
