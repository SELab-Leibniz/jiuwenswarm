"""Candidate-bound handoff evidence; role claims never grant acceptance.

The Control Plane supplies all checkpoint, artifact and revision identities. The
optional Worker block contains semantic observations only and is diagnostic on
failure. These helpers neither inspect git nor commit canonical state.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import cast

from .._role_output import decode_role_object

_OPEN = "<pact-handoff>"
_CLOSE = "</pact-handoff>"


def parse_handoff_observations(
    text: str, *, requirement_ids: Sequence[str], input_checkpoint: str | None = None,
) -> dict[str, object]:
    """Read one optional semantic block, retaining diagnostics without raising.

An explicit stale input identity invalidates the block; omission is normal. The
caller must bind the selected summary artifact to the actual output candidate.
No model-provided result is returned as authoritative verification or acceptance.
"""
    observations: list[dict[str, object]] = []
    assumptions: list[dict[str, object]] = []
    diagnostics: list[str] = []
    result: dict[str, object] = {
        "observations": observations, "assumptions": assumptions, "diagnostics": diagnostics,
    }
    if _OPEN not in text and _CLOSE not in text:
        return result
    if text.count(_OPEN) != 1 or text.count(_CLOSE) != 1:
        diagnostics.append("handoff requires one complete optional block")
        return result
    start, end = text.index(_OPEN) + len(_OPEN), text.index(_CLOSE)
    if end < start or end - start > 128_000:
        diagnostics.append("handoff block is malformed or exceeds 128000 characters")
        return result
    try:
        payload = decode_role_object(text[start:end])
        # JSON permits escaped lone surrogates, but they cannot be persisted as
        # UTF-8. Optional model observations must not break the CP artifact write.
        json.dumps(payload, ensure_ascii=False).encode("utf-8")
    except (ValueError, RecursionError) as exc:
        diagnostics.append(f"invalid handoff JSON: {str(exc)[:1000]}")
        return result
    if set(payload) - {"observations", "assumptions", "input_checkpoint"}:
        diagnostics.append("handoff contains unsupported fields")
        return result
    if "input_checkpoint" in payload and (
        input_checkpoint is None or payload["input_checkpoint"] != input_checkpoint
    ):
        diagnostics.append("handoff input checkpoint does not match the current turn")
        return result
    raw_observations = payload.get("observations", [])
    raw_assumptions = payload.get("assumptions", [])
    if not isinstance(raw_observations, list) or not isinstance(raw_assumptions, list):
        diagnostics.append("handoff observations and assumptions must be arrays")
        return result
    known = set(requirement_ids)
    seen: set[str] = set()
    for index, raw in enumerate(raw_observations):
        prefix = f"observations[{index}]"
        if not isinstance(raw, dict) or set(raw) - {
            "requirement_id", "implementation", "checks", "inspection", "claimed_result",
        }:
            diagnostics.append(f"{prefix} must be an observation with supported fields")
            continue
        requirement = raw.get("requirement_id")
        if not isinstance(requirement, str) or requirement not in known:
            diagnostics.append(f"{prefix} refers to an unknown requirement")
            continue
        if requirement in seen:
            # Conflicting observations must not choose a convenient last value.
            observations[:] = [item for item in observations if item["requirement_id"] != requirement]
            diagnostics.append(f"{prefix} duplicates requirement {requirement}")
            continue
        seen.add(requirement)
        implementation = raw.get("implementation", "unknown")
        checks = raw.get("checks", [])
        inspection = raw.get("inspection", "")
        if (implementation not in ("present", "absent", "unknown")
                or not isinstance(inspection, str)
                or not _string_list(checks)):
            diagnostics.append(f"{prefix} has invalid observation fields")
            continue
        observations.append({
            "requirement_id": requirement,
            "status": {"present": "implemented_unverified", "absent": "missing", "unknown": "unknown"}[
                cast(str, implementation)
            ],
            "check_names": checks, "inspection": inspection,
            "source": "worker_claim",
        })
    for index, raw in enumerate(raw_assumptions):
        prefix = f"assumptions[{index}]"
        if not isinstance(raw, dict) or set(raw) != {"requirement_ids", "assumption", "basis", "material"}:
            diagnostics.append(f"{prefix} must include requirement_ids, assumption, basis and material")
            continue
        ids = raw["requirement_ids"]
        if (not _string_list(ids) or not ids or any(item not in known for item in cast(list[str], ids))
                or not isinstance(raw["assumption"], str) or not raw["assumption"].strip()
                or not isinstance(raw["basis"], str) or not raw["basis"].strip()
                or not isinstance(raw["material"], bool)):
            diagnostics.append(f"{prefix} has invalid assumptions or unknown requirements")
            continue
        assumptions.append({
            "requirement_ids": list(cast(list[str], ids)), "assumption": raw["assumption"],
            "basis": raw["basis"], "material": raw["material"], "source": "worker_claim",
            "resolution": "unresolved", "changes_hard_acceptance": False,
        })
    return result


def _string_list(value: object) -> bool:
    return (isinstance(value, list)
            and all(isinstance(item, str) and bool(item.strip()) for item in value)
            and len(value) == len(set(value)))
