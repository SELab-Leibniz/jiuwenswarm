# Copyright (c) 2026 Chrys. All rights reserved.

"""Advisory Worker guidance; no question classification or resolution authority."""

from __future__ import annotations

from .generalization_check import (  # noqa: F401
    GENERALIZATION_CHECK_GUIDANCE_VERSION,
    audit_generalization_check_guidance,
    reviewer_generalization_check_guidance,
    worker_generalization_check_guidance,
)
from .process_kill_guard import (  # noqa: F401
    PROCESS_KILL_GUIDANCE_VERSION,
    process_kill_guidance,
)
from .reference_client_spec import (  # noqa: F401
    REFERENCE_SPEC_GUIDANCE_VERSION,
    reviewer_reference_client_spec_guidance,
    worker_reference_client_spec_guidance,
)
from .reviewer_execution import REVIEWER_EXECUTION_GUIDANCE_VERSION, reviewer_execution_guidance  # noqa: F401

WORKER_EVIDENCE_GUIDANCE_VERSION = "worker-evidence-triage/v1"
ANYTIME_DELIVERABLE_GUIDANCE_VERSION = "anytime-deliverable/v1"
UI_STATE_EVIDENCE_GUIDANCE_VERSION = "ui-state-evidence/v1"
DELIVERY_HYGIENE_GUIDANCE_VERSION = "delivery-hygiene/v3"


def worker_anytime_deliverable_guidance() -> str:
    return (
        "\n\n## Anytime deliverable (anytime-deliverable/v1)\n"
        "Write a valid, complete deliverable to the delivery path as early as possible; then improve it while keeping it valid.\n"
        "For a long search or optimization, periodically atomically write (temporary file, then rename) the current best result; do not wait for program exit.\n"
        "Never let one command consume the remaining time; leave margin for finish and checks.\n"
    )


def worker_ui_state_evidence_guidance() -> str:
    return (
        "\n\n## UI state evidence (user-visible deliverables)\n"
        "When the deliverable has a user-visible interface (Web page, TUI, etc.), before finishing actually run "
        "the application and check fresh/empty data, a brand-new account or anonymous access, after restart, and "
        "zero counts or empty lists. Use a real browser when available; otherwise capture the rendered HTML.\n"
        "Every interface element named by the requirement (counts and badges, empty-state text, navigation items, "
        "error messages) must render correctly in these states, not only when data is present.\n"
        "In the handoff add a short \"UI state evidence\" section: states checked, command or URL, observed result.\n"
        "Before finishing, run the project's full test or check command in a clean state and report the result in "
        "the handoff.\n"
    )


def reviewer_ui_state_evidence_guidance() -> str:
    """Compatibility name: the Reviewer gate is now the executable-review gate."""
    return reviewer_execution_guidance()


def delivery_hygiene_guidance() -> str:
    return (
        "\n\n## Delivery hygiene (blocking acceptance requirement)\n"
        "Deliverable source must not hardcode host absolute paths, including a workspace-parent or "
        "reference-data directory. Take paths from task input, protocol parameters, or configuration instead.\n"
        "At run time do not read evaluation, reference-answer, or expected-output data.\n"
        "The deliverable must be self-contained. For a cloned or simulated external service, the deliverable tree "
        "(every part of the workspace that will be delivered) must not, at any location, import, vendor or depend "
        "on that service's official client SDK, and must not contain its production hostnames or endpoints. This "
        "applies to your own tests, fixtures and scripts as well as service code. To run end-to-end validation with "
        "the official client, put the validation script in a temporary directory outside the deliverable tree and "
        "delete it before finishing. Tests written inside the deliverable tree must use protocol-level HTTP requests "
        "only.\n"
        "Before finishing, remove your own copies of reference data and temporary files outside the workspace.\n"
    )


def reviewer_delivery_hygiene_guidance() -> str:
    return (
        "\n\n## Delivery hygiene gate (blocking)\n"
        "Check that deliverable source hardcodes no host absolute path, including a workspace-parent or "
        "reference-data directory; paths must come from task input, protocol parameters, or configuration. "
        "Check that runtime code does not read evaluation, reference-answer, or expected-output data. "
        "Check that the deliverable is self-contained. Inspect the actual deliverable tree copy and search every "
        "path, including tests, fixtures and scripts, for imports or vendored copies of a cloned or simulated "
        "service's official client SDK and for that service's production hostnames or endpoints. Any such import, "
        "vendored dependency, hostname or endpoint in the deliverable tree is blocking, even when only tests use it; "
        "return the candidate to the Worker. End-to-end validation with the official client belongs in a temporary "
        "directory outside the deliverable tree and must be deleted before finishing. Protocol-level HTTP tests "
        "inside the deliverable tree are allowed. "
        "Check that the Worker removed its own reference-data copies and temporary files outside the workspace. "
        "Any violation, including touching the real service or relying on an external production endpoint, is a "
        "blocking problem: return the candidate to the Worker instead of accepting it.\n"
        + reviewer_reference_client_spec_guidance()
    )


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
        + delivery_hygiene_guidance()
        + worker_generalization_check_guidance()
        + worker_reference_client_spec_guidance()
        + worker_ui_state_evidence_guidance()
        + worker_anytime_deliverable_guidance()
        + process_kill_guidance()
    )
