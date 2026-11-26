# Copyright (c) 2026 Chrys. All rights reserved.

"""Carry bounded public failure evidence between Works in the same repository."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from collections.abc import Iterable, Iterator, Mapping
from copy import deepcopy
from itertools import islice
from pathlib import PurePosixPath
from typing import Any

from jiuwenswarm.pact_core.runtime.types import WorkSnapshot

from .public_reference_evidence import _literal_helper, _receipt
from .reference_client_spec import PUBLIC_REFERENCE_COMMAND

SCHEMA = "long-horizon/public-regression-handoff/v1"
BEHAVIOR_SCHEMA = "long-horizon/public-regression-check/v1"
MAX_REGRESSIONS = 32
MAX_NATIVE_REGRESSIONS = 12
_MAX_HINTS_PER_MERGE = 128
_MAX_CONTEXT_ENTRIES = 8
_LABEL = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:/@,\[\]-]{0,159}")
_CHECKPOINT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:/-]{0,47}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_GIT_ID = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_REFERENCE_ARGC = len(shlex.split(PUBLIC_REFERENCE_COMMAND))


def _digest(*parts: object) -> str:
    return hashlib.sha256(json.dumps(parts, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def _matches(pattern: re.Pattern[str], value: object) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _named_observation(pattern: re.Pattern[str], value: object) -> bool:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        return False
    return all(part not in (".", "..") for part in value.split("::", 1)[0].split("/"))


def _case_identity(case: object, checkpoint: object) -> tuple[str, str | None]:
    if not isinstance(case, str) or not _named_observation(_LABEL, case):
        raise ValueError("public regression case must be a bounded identifier")
    if checkpoint is None or checkpoint == "":
        return case, None
    if not isinstance(checkpoint, str) or not _named_observation(_CHECKPOINT, checkpoint):
        raise ValueError("public regression checkpoint must be a bounded observation label")
    if checkpoint.isdecimal():
        checkpoint = checkpoint.lstrip("0") or "0"
    return case, checkpoint


def regression_case_key(case: str, checkpoint: str | None = None) -> str:
    """Bind a public case and optional checkpoint without trusting log prose."""
    return _digest("case", *_case_identity(case, checkpoint))


def _origin(state: WorkSnapshot | None) -> dict[str, Any] | None:
    if state is None or state.candidate is None:
        return None
    candidate = state.candidate
    if (
        candidate.work_id != state.work_id
        or not _matches(_LABEL, state.work_id)
        or not _matches(_GIT_ID, candidate.tree)
        or not _matches(_GIT_ID, candidate.checkpoint)
        or type(state.revision) is not int
        or state.revision < 0
    ):
        return None
    return {
        "work_id": state.work_id,
        "revision": state.revision,
        "candidate_tree": candidate.tree,
        "candidate_checkpoint": candidate.checkpoint,
    }


def _bound_rows(state: WorkSnapshot) -> Iterator[tuple[Mapping[str, Any], Mapping[str, Any] | None]]:
    verification = state.verification
    if verification is None or verification.candidate != state.candidate or verification.status not in ("passed", "failed"):
        return
    definitions = {check["name"]: check for check in state.checks if check["disposition"] in ("pending", "admitted")}
    supplied = (state.policy.verify_command, state.policy.closeout_verify_command)
    rows = verification.check_results or (
        {
            "name": "public-1",
            "source": "supplied",
            "command": verification.command,
            "status": verification.status,
            "exit_code": verification.exit_code,
            "stdout_tail": verification.stdout_tail,
        },
    )
    for row in rows:
        command = row.get("command")
        if not isinstance(command, str) or row.get("method_prerequisite") is not None:
            continue
        if row.get("source") == "supplied" and command in supplied:
            yield row, None
            continue
        definition = definitions.get(row.get("name"))
        if (
            row.get("source") == "derived"
            and definition is not None
            and _matches(_SHA256, definition.get("fingerprint"))
            and definition["fingerprint"] == row.get("fingerprint")
            and definition["command"] == command
        ):
            yield row, definition


def _oracle_observation(row: Mapping[str, Any], definition: Mapping[str, Any] | None) -> dict[str, Any] | None:
    command = row["command"]
    argv = shlex.split(command)
    if argv[_REFERENCE_ARGC] != "compare":
        return None
    receipt = _receipt(row)
    if not receipt or receipt["status"] not in ("match", "mismatch"):
        return None
    fixture = receipt.get("request_sha256")
    reference = receipt.get("reference_argv_sha256")
    candidate = receipt.get("candidate_argv_sha256")
    if not all(_matches(_SHA256, digest) for digest in (fixture, reference, candidate)) or reference == candidate:
        return None
    mismatch = receipt["status"] == "mismatch"
    step = receipt.get("step")
    checkpoint = str(step) if type(step) is int and 0 <= step < 10**20 else None
    count = receipt.get("checkpoints")
    if (mismatch and checkpoint is None) or (not mismatch and (type(count) is not int or count < 2)):
        return None
    method = _digest(command, definition["fingerprint"] if definition else None, reference, candidate)
    name = row.get("name")
    fixture_label = PurePosixPath(argv[_REFERENCE_ARGC + 1]).name if len(argv) > _REFERENCE_ARGC + 1 else ""
    return {
        "key": _digest("oracle", method, fixture),
        "case": name if _named_observation(_LABEL, name) else "public-reference",
        "checkpoint": checkpoint,
        "source": "core:public-reference",
        "evidence": {"kind": "oracle", "method_sha256": method, "fixture_sha256": fixture},
        **({"fixture_label": fixture_label} if _matches(_LABEL, fixture_label) else {}),
    }


def _behavior_observation(row: Mapping[str, Any], definition: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if definition is None or definition["disposition"] != "admitted":
        return None
    review = definition.get("method_review")
    if not isinstance(review, dict) or not review.get("review_ref") or not isinstance(review.get("reason"), str):
        return None
    output = row.get("stdout_tail")
    if not isinstance(output, str) or not output.strip():
        return None
    try:
        receipt = json.loads(output.rstrip().rsplit("\n", maxsplit=1)[-1])
    except (ValueError, RecursionError):
        return None
    if not isinstance(receipt, dict) or receipt.get("schema") != BEHAVIOR_SCHEMA:
        return None
    key, fixture = receipt.get("key"), receipt.get("fixture_sha256")
    if not _matches(_SHA256, key) or not _matches(_SHA256, fixture):
        return None
    files = definition.get("files")
    if (
        row["name"] != f"regression-{key}"
        or review["reason"].split(maxsplit=3)[:3] != ["public-regression-v1", key, fixture]
        or not isinstance(files, dict)
        or fixture not in files.values()
    ):
        return None
    expected = "match" if row["exit_code"] == 0 else "mismatch"
    if receipt.get("outcome") != expected:
        return None
    # A known key already identifies the case. An independently discovered
    # failure needs the public case label and checkpoint to open a new entry.
    case = receipt.get("case")
    checkpoint = receipt.get("checkpoint")
    if case is not None:
        try:
            case, checkpoint = _case_identity(case, checkpoint)
        except ValueError:
            return None
        if _digest("case", case, checkpoint) != key:
            return None
    elif checkpoint is not None:
        return None
    return {
        "key": key,
        "case": case,
        "checkpoint": checkpoint,
        "source": "core:reviewed-behavior",
        "evidence": {"kind": "behavior", "method_sha256": definition["fingerprint"], "fixture_sha256": fixture},
    }


def _core_observations(state: WorkSnapshot) -> Iterator[tuple[int, dict[str, Any]]]:
    for row, definition in _bound_rows(state):
        code = row.get("exit_code")
        if type(code) is not int or (row.get("status"), code) not in (("passed", 0), ("failed", 1)):
            continue
        if _literal_helper(row["command"]):
            observation = _oracle_observation(row, definition)
        else:
            observation = _behavior_observation(row, definition)
        if observation is not None:
            yield code, observation


def _native_entry(hint: Mapping[str, object], origin: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(hint, Mapping) or not _matches(_LABEL, hint.get("source")):
        return None
    try:
        case, checkpoint = _case_identity(hint.get("case"), hint.get("checkpoint"))
    except ValueError:
        return None
    return {
        "key": _digest("case", case, checkpoint),
        "case": case,
        "checkpoint": checkpoint,
        "source": hint["source"],
        "origin": origin,
        "evidence": {"kind": "native"},
    }


def _read_previous(previous: dict[str, Any] | None) -> tuple[dict[str, dict[str, Any]], int]:
    if previous is None:
        return {}, 0
    if not isinstance(previous, dict):
        raise ValueError("invalid public regression ledger")
    data = previous.get("items")
    omitted = previous.get("omitted")
    if (
        previous.get("schema") != SCHEMA
        or not isinstance(data, list)
        or len(data) > MAX_REGRESSIONS
        or type(omitted) is not int
        or omitted < 0
    ):
        raise ValueError("invalid public regression ledger")
    items = {}
    for entry in data:
        if (
            not isinstance(entry, dict)
            or not _matches(_SHA256, entry.get("key"))
            or not _named_observation(_LABEL, entry.get("case"))
            or not _matches(_LABEL, entry.get("source"))
            or (entry.get("checkpoint") is not None and not _named_observation(_CHECKPOINT, entry["checkpoint"]))
            or not isinstance(entry.get("evidence"), dict)
            or entry["evidence"].get("kind") not in ("native", "oracle", "behavior")
            or entry["key"] in items
        ):
            raise ValueError("invalid public regression entry")
        items[entry["key"]] = deepcopy(entry)
    return items, omitted


def _resolved(entry: dict[str, Any], passes: list[dict[str, Any]], failures: set[str]) -> bool:
    if entry["key"] in failures:
        return False
    evidence = entry["evidence"]
    return any(
        proof["key"] == entry["key"]
        and (evidence == proof["evidence"] or evidence["kind"] == "native" and proof["evidence"]["kind"] == "behavior")
        for proof in passes
    )


def merge_regressions(
    previous: dict[str, Any] | None = None,
    *,
    state: WorkSnapshot | None = None,
    native_hints: Iterable[Mapping[str, object]] = (),
    native_work_id: str | None = None,
    allow_resolution: bool = True,
) -> dict[str, Any]:
    """Merge current Core evidence with a JSON ledger, without executing any checks.

    The caller owns trial identity, persistence and any validated Core snapshot.
    Native observations from a live turn can bear only the caller-verified
    Work ID. They make no claim about a committed candidate checkpoint.
    When the live repository has moved past the Core candidate, its results
    can still add historical failures with resolution disabled.
    Omitted counts retain the largest observed overflow in one merge.
    """
    items, old_omitted = _read_previous(previous)
    origin = _origin(state)
    native_origin = origin
    if native_work_id is not None:
        native_origin = (
            {"work_id": native_work_id}
            if _matches(_LABEL, native_work_id) and (state is None or state.work_id == native_work_id)
            else None
        )

    observations = list(_core_observations(state)) if state is not None and origin is not None else []
    passes = [row for code, row in observations if code == 0] if allow_resolution else []
    failures = {row["key"] for code, row in observations if code == 1}
    items = {key: item for key, item in items.items() if not _resolved(item, passes, failures)}
    omitted: set[str] = set()
    incoming: dict[str, dict[str, Any]] = {}
    if native_origin is not None:
        for index, hint in enumerate(islice(native_hints, _MAX_HINTS_PER_MERGE + 1)):
            if index == _MAX_HINTS_PER_MERGE:
                omitted.add("input-limit")
                break
            entry = _native_entry(hint, native_origin)
            if entry is not None and not _resolved(entry, passes, failures):
                incoming.setdefault(entry["key"], entry)
    native_count = sum(item["evidence"]["kind"] == "native" for item in items.values())
    for code, observation in observations:
        if code != 1:
            continue
        key = observation["key"]
        native = incoming.pop(key, None)
        known = items.get(key, native)
        if known is not None:
            if known["evidence"]["kind"] != "native":
                continue
            if key in items:
                native_count -= 1
            verified = {**known, "evidence": observation["evidence"], "verified_origin": origin}
        elif observation["case"] is not None:
            verified = {**observation, "origin": origin}
        else:
            continue
        if key in items or len(items) < MAX_REGRESSIONS:
            items[key] = verified
        else:
            omitted.add(key)

    for key, entry in incoming.items():
        if key in items:
            continue
        if len(items) >= MAX_REGRESSIONS or native_count >= MAX_NATIVE_REGRESSIONS:
            omitted.add(key)
        else:
            items[key] = entry
            native_count += 1
    return {"schema": SCHEMA, "items": list(items.values()), "omitted": max(old_omitted, len(omitted))}


def worker_regression_context(ledger: dict[str, Any], *, limit: int = 4, offset: int = 0) -> str:
    """Project bounded labels, allowing the caller to rotate deferred cases."""
    entries, omitted = _read_previous(ledger)
    if not entries and not omitted:
        return ""
    items = list(entries.values())
    start = offset % len(items) if items else 0
    ordered = items[start:] + items[:start]
    selected = ordered[: max(0, min(limit, _MAX_CONTEXT_ENTRIES))]
    lines = [
        "Earlier public regression evidence (advisory)",
        "Use independently confirmed public inputs and the normal check path. The current user requirements govern.",
    ]
    if selected:
        lines.append(
            "During implementation, use a bounded check to replay the same public input and method. Inspect the first "
            "divergence and nearby checkpoints. If reproduced, repair and rerun the case. If you cannot recover "
            "the same input or locate the first divergence, keep it unresolved and proceed with the current Work."
        )
    for entry in selected:
        evidence = entry["evidence"]
        label = "unverified native clue" if evidence["kind"] == "native" else "Core mismatch"
        fixture = evidence.get("fixture_sha256")
        fields = [label, f"id {entry['key']}"]
        if entry["checkpoint"] is not None:
            fields.insert(0, f"checkpoint {entry['checkpoint']}")
        origin = entry.get("origin")
        if isinstance(origin, dict) and _matches(_LABEL, origin.get("work_id")):
            fields.append(f"origin Work {origin['work_id']}")
        fields.append(f"source {entry['source']}")
        if _matches(_SHA256, fixture):
            fields.append(f"public fixture SHA-256 {fixture}")
        if _matches(_LABEL, entry.get("fixture_label")):
            fields.append(f"fixture {entry['fixture_label']}")
        lines.append(f"- {entry['case']} [{', '.join(fields)}]")
    if any(item["evidence"]["kind"] == "oracle" for item in selected):
        lines.append("Recheck a Core oracle mismatch with the same public request and comparison method.")
    if any(item["evidence"]["kind"] in ("native", "behavior") for item in selected):
        lines.extend((
            "Independent Reviewer confirmation of a named case uses the normal Core check workflow. "
            "Register regression-<id> with an executable check of that exact case and checkpoint. "
            "Quote the applicable original requirement and include the public fixture in the Core check files "
            "so Core pins its SHA-256.",
            "Review and admit that exact definition version. Start the admission reason with "
            "public-regression-v1 <id> <fixture_sha256> and then give the evidence for the chosen method.",
            "After the actual behavioral assertion, emit this final JSON line "
            f'{{"schema":"{BEHAVIOR_SCHEMA}","key":"<id>",'
            '"fixture_sha256":"<fixture_sha256>","outcome":"match"}. '
            'For a demonstrated mismatch use "outcome":"mismatch". '
            "Exit 0 for match and 1 for mismatch. Report setup and transport failures separately.",
            "If a newer public answer changes the expected behavior, cite it through the existing "
            "requirements and check-review process. Keep the current Work moving if a prior case is unresolved.",
        ))
    if len(selected) < len(items):
        lines.append(f"{len(items) - len(selected)} more tracked cases can be shown in a later Work.")
    if omitted:
        lines.append(f"At least {omitted} incoming cases were omitted by the handoff bounds.")
    return "\n".join(lines)
