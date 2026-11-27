# Copyright (c) 2026 Chrys. All rights reserved.
"""Route current Core check evidence while preserving the original requirements."""

from __future__ import annotations

from .public_reference_evidence import direct_reference_failure


def method_diagnosis(state):
    verification = getattr(state, "verification", None)
    if (
        verification is None
        or getattr(verification, "candidate", None) != getattr(state, "candidate", None)
        or getattr(verification, "status", None) != "failed"
    ):
        return {}, False
    active = {c["name"]: c for c in getattr(state, "checks", ()) if c["disposition"] != "suspended"}
    methods = {}
    other = False
    seen = set()
    rows = getattr(verification, "check_results", ())
    if not rows:
        # With only a supplied --verify command, Core stores its sole result directly.
        required = getattr(getattr(state, "policy", None), "verify_command", None)
        if not required:
            return {}, False
        row = {"command": required, "exit_code": verification.exit_code, "stdout_tail": verification.stdout_tail}
        reason = direct_reference_failure(row)
        return ({"public-1": {"kind": "required_public_reference", "value": reason}}, False) if reason else ({}, False)
    for row in rows:
        definition = active.get(row.get("name"))
        derived = (
            row.get("source") == "derived"
            and definition
            and row.get("fingerprint")
            and row["fingerprint"] == definition.get("fingerprint")
            and row["command"] == definition["command"]
        )
        if derived:
            seen.add(row["name"])
        if row.get("status") == "passed":
            continue
        if row.get("source") == "supplied" and row.get("command") == getattr(
            getattr(state, "policy", None), "verify_command", None
        ):
            reason = direct_reference_failure(row)
            if reason:
                methods[row["name"]] = {"kind": "required_public_reference", "value": reason}
                continue
        witness = row.get("method_prerequisite")
        if derived and row.get("status") == "failed":
            if (
                isinstance(witness, dict)
                and witness.get("kind") in {"shell_option", "tool_lookup", "temp_directory"}
                and isinstance(witness.get("value"), str)
            ):
                methods[row["name"]] = witness
                other |= witness["kind"] == "temp_directory"
                continue
            reference_failure = direct_reference_failure(row)
            if reference_failure:
                methods[row["name"]] = {"kind": "public_reference", "value": reference_failure}
                continue
            # Both supported Core runners reserve a null code for an error
            # before shell launch. A subprocess printing this text has a code.
            if row.get("exit_code", 0) is None and any(
                row.get("stderr_tail") == f"definition_changed: {path}; register the revised check with its reason"
                for path in definition.get("files", {})
            ):
                methods[row["name"]] = {"kind": "definition_changed"}
                # The runner establishes a pin mismatch. The original task
                # may require the implementation to preserve the pinned file.
                other = True
                continue
        other = True
    return methods, other or seen != active.keys()


def only_method_failures(state):
    methods, other_failures = method_diagnosis(state)
    return bool(methods) and not other_failures
