# Copyright (c) 2026 Chrys. All rights reserved.

"""Read public reference evidence attached to an existing Core verification."""

from __future__ import annotations

import json
import shlex

from .public_reference import SCHEMA
from .reference_client_spec import PUBLIC_REFERENCE_COMMAND


def _receipt(row):
    try:
        value = json.loads(row.get("stdout_tail") or "")
    except (TypeError, ValueError, RecursionError):
        return None
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        return None
    status = value.get("status")
    if status in ("available", "match"):
        code = 0
    elif status == "mismatch":
        code = 1
    elif status in ("invalid_request", "reference_unavailable", "candidate_unavailable", "comparison_unavailable"):
        code = 2
    else:
        return None
    return value if type(row.get("exit_code")) is int and row["exit_code"] == code else None


def recorded_reference_checks(state) -> tuple[dict, ...]:
    """Accept only current Core results for the same active definition and literal bundled helper."""
    verification = state.verification
    if verification is None or verification.candidate != state.candidate:
        return ()
    active = {row["name"]: row for row in state.checks if row["disposition"] != "suspended"}
    supplied = [state.policy.verify_command, getattr(state.policy, "closeout_verify_command", None)]
    rows = verification.check_results or (
        {
            "source": "supplied",
            "command": verification.command,
            "exit_code": verification.exit_code,
            "stdout_tail": verification.stdout_tail,
        },
    )
    receipts = []
    for row in rows:
        command = row.get("command")
        if not _literal_helper(command):
            continue
        definition = active.get(row.get("name"))
        supplied_check = row.get("source") == "supplied" and command in supplied
        derived_check = (
            row.get("source") == "derived"
            and definition
            and definition["command"] == command
            and row.get("fingerprint")
            and row["fingerprint"] == definition["fingerprint"]
        )
        if (supplied_check or derived_check) and (receipt := _receipt(row)) is not None:
            receipts.append(receipt)
    return tuple(receipts)


METHOD_REASONS = (
    "access_denied",
    "timeout",
    "service_unavailable",
    "nonzero_exit",
    "invalid_json",
    "incomplete_trace",
    "invalid_observation",
    "output_too_large",
    "execution_error",
    "read_error",
    "invalid_request",
    "request_too_large",
    "invalid_checkpoints",
    "identical_adapters",
    "helper_error",
    "nesting_limit",
)


def method_reason(receipt):
    status = receipt.get("status")
    if status in ("reference_unavailable", "invalid_request", "comparison_unavailable") or (
        status == "candidate_unavailable" and receipt.get("reason") == "execution_error"
    ):
        reason = receipt.get("reason")
        return reason if reason in METHOD_REASONS else "unverified"
    return None


def _literal_helper(command):
    if not isinstance(command, str):
        return False
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    prefix = shlex.split(PUBLIC_REFERENCE_COMMAND)
    return (
        argv[: len(prefix)] == prefix
        and argv[len(prefix) : len(prefix) + 1] in (["probe"], ["compare"])
        and shlex.join(argv) == command
    )


def direct_reference_failure(row):
    """Only a literal invocation of the bundled runner may change the failure route."""
    if not _literal_helper(row.get("command")):
        return None
    receipt = _receipt(row)
    return method_reason(receipt) if receipt else None
