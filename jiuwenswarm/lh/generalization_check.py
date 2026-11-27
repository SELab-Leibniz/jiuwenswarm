# Copyright (c) 2026 Chrys. All rights reserved.

"""Advisory guidance: validate behavior on inputs beyond the public fixtures."""

from __future__ import annotations

GENERALIZATION_CHECK_GUIDANCE_VERSION = "generalization-self-check/v1"


def worker_generalization_check_guidance() -> str:
    return (
        "\n\n## Generalization self-check (hidden inputs)\n"
        "Evaluation normally uses hidden inputs that are not the public examples. Do not special-case, table lookup "
        "or hardcode public fixtures, golden files or expected outputs.\n"
        "Before finishing, construct several new inputs absent from the public fixtures (new source files, modules, "
        "data and combinations covering cross-file, boundary and rare structures) and confirm the behavior is "
        "equally correct on them.\n"
    )


def reviewer_generalization_check_guidance() -> str:
    return (
        "\n\n## Novel-input generalization gate (blocking)\n"
        "Beyond the public tests, in a temporary directory outside the deliverable tree, construct several new "
        "inputs absent from the public fixtures and cover every function class the task names, especially "
        "cross-file and cross-module relational behavior (references, call relationships, dependencies). Judge "
        "results by the task specification; any mismatch is a blocking gap.\n"
        "Mark each checked item 'novel-input-tested' or 'public-fixture-only'; a function class validated only on "
        "public fixtures cannot alone support a clean verdict.\n"
        "Special-casing, table lookup or hardcoding public examples is a blocking gap: return the candidate to the "
        "Worker.\n"
    )


def audit_generalization_check_guidance() -> str:
    """The bounded coverage audit applies the same novel-input gate as review."""
    return reviewer_generalization_check_guidance()
