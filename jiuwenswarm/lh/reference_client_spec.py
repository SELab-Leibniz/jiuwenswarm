# Copyright (c) 2026 Chrys. All rights reserved.

"""Advisory guidance: an installed reference client is the interface authority."""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

PUBLIC_REFERENCE_COMMAND = shlex.join(
    [sys.executable, "-I", str(Path(__file__).with_name("public_reference.py").resolve())]
)

REFERENCE_SPEC_GUIDANCE_VERSION = "reference-client-spec/v2"


PUBLIC_REFERENCE_GUIDANCE = (
    "\nWhen the public task supplies an executable reference tool, probe its documented interface early "
    "in the actual check environment with a small authorized input. Record a valid observable response "
    "or the failure class (for example access denied, timeout, or malformed output). Tool presence, a "
    "successful connection, and exit zero alone do not establish availability. Check documented invocation "
    "and task-provided configuration before a bounded retry; preserve access controls. "
    "For stateful behavior start each side from the same fresh public input and seed, apply identical ordered "
    "actions in one continuous run per side, and compare the same public state fields at two or more specified "
    "checkpoints, including an intermediate step. Record the first differing step and field. "
    f"Invoke the bundled standalone helper as {PUBLIC_REFERENCE_COMMAND} with probe / compare. "
    "For controller checks use this exact absolute invocation with arguments quoted by Python shlex.join. Register the compare as a supplemental executable check so Core retains this candidate’s result. "
    "If an isolated task image lacks the helper, run the available public interface and report its observations "
    "and reproducible inputs as reviewer evidence. Only the bundled comparison recorded by Core can certify a "
    "match. Report missing tooling or an unreachable reference as unverified. "
    "Use public inputs and interfaces only; keep adapters and receipts outside the deliverable.\n"
)


def worker_reference_client_spec_guidance() -> str:
    return (
        "\n\n## Reference client as interface specification (when provided)\n"
        "When the task implements or is compatible with an existing service or protocol and the environment provides "
        "its official client SDK, specification files, or a reference implementation (for example an installed "
        "package, vendor directory, or documentation), read those materials first and treat them as the authoritative "
        "source for parameter names and structure, response fields, error codes, permission and scope semantics, and "
        "state machines; do not design interfaces from memory or guesswork.\n"
        "These materials are read-only: never import or copy them into the deliverable tree (delivery-hygiene/v3).\n"
        "If the environment provides no official client, specification, or reference implementation, this clause "
        "does not apply.\n" + PUBLIC_REFERENCE_GUIDANCE
    )


def reviewer_reference_client_spec_guidance() -> str:
    return (
        "\n\n## Official-client end-to-end validation (when provided)\n"
        "For these tasks, when the environment provides the official client or specification, use a temporary "
        "directory outside the deliverable tree to make end-to-end calls over every function class the task names, "
        "including permissions and scopes, revocation, and error paths; compare the actual responses with the fields "
        "and semantics defined in the client source.\n"
        "Any mismatch is a blocking gap: return the candidate to the Worker. "
        "Mark each checked item 'official-client-tested' or 'self-test-only'; an item validated only by self-tests "
        "cannot support a clean verdict.\n"
        "If no official client or specification is provided, this clause does not apply.\n" + PUBLIC_REFERENCE_GUIDANCE
    )
