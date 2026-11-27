# Copyright (c) 2026 Chrys. All rights reserved.

"""Shared check-authoring instructions for clarification and supplemental review."""

from .reference_client_spec import PUBLIC_REFERENCE_GUIDANCE

CONFIRMED_BEHAVIOR_CHECK = (
    "\n\nFor a behavior criterion, look up relevant answers already confirmed by the user or product owner, "
    "including earlier public answers in the supplied inputs. Cite the source and apply its conditions, "
    "using the latest confirmed answer if it supersedes an earlier one. A candidate-authored claim alone "
    "cannot establish the expected behavior. Run the documented user-facing entry point with an input "
    "that triggers the rule and assert the observable result. If a neighboring input distinguishes a "
    "plausible wrong implementation, exercise it as a control and assert its expected result too. "
    "During review, repair a proposed check that would pass both the correct and the wrong implementation. "
    "Keep content checks for documentation-only requirements. When the necessary public evidence is "
    "unavailable, identify what is missing."
    + PUBLIC_REFERENCE_GUIDANCE
)
