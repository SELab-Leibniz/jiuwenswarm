# Copyright (c) 2026 Chrys. All rights reserved.

"""Sealed candidate delivery into the task root, plus an explicit safety net.

The normal path applies only the controller's published selection export after
``verify_export``. When the controller or that normal path fails after a
candidate was already accepted by Core or passed the public candidate gate,
``fallback_delivery`` applies that sealed export instead of leaving the task
root undelivered. It never creates acceptance or public-check evidence: a
candidate qualifies only through a Core ``accepted`` export receipt or an
eligible public-gate attempt recorded by the controller for the exact same
candidate. Every fallback attempt is recorded, including a refusal to deliver.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from jiuwenswarm.pact_core.runtime import RuntimeStateError
from jiuwenswarm.pact_core.runtime.export import ExportReceipt, verify_export
from jiuwenswarm.pact_core.runtime.types import CandidateRef

from ..checkpoint_guard import sha256_file

FALLBACK_SCHEMA = "harbor-runtime/fallback-delivery/v1"
_ARCHIVE_ENTRY_SCHEMA = "harbor-runtime/candidate-archive-entry/v1"
EXPORT_START_FLOOR_SECONDS = 5.0
EXPORT_SAFETY_FACTOR = 1.5


class ExportStartRefused(RuntimeStateError):
    """The hard clock cannot safely fit another export copy."""

    def __init__(self, *, remaining_seconds: float, required_seconds: float) -> None:
        super().__init__(
            "insufficient time before the hard deadline to start another export: "
            f"{remaining_seconds:.3f}s remaining, {required_seconds:.3f}s required"
        )
        self.remaining_seconds = remaining_seconds
        self.required_seconds = required_seconds


def export_start_floor(observations: list[float] | None = None) -> float:
    """Require max(observed copy * 1.5, fixed floor) before a new export."""
    observed = max((float(value) for value in observations or () if value >= 0), default=0.0)
    return max(EXPORT_START_FLOOR_SECONDS, observed * EXPORT_SAFETY_FACTOR)


def receipt_from_payload(export: dict[str, Any]) -> ExportReceipt:
    """Rebuild the receipt published in ``selection.json``."""
    return ExportReceipt(
        artifact=Path(export["artifact"]),
        artifact_sha256=export["artifact_sha256"],
        work_id=export["work_id"],
        candidate=CandidateRef(**{**export["candidate"], "artifact": Path(export["candidate"]["artifact"])}),
        source_workspace=Path(export["source_workspace"]),
        source_basis=export["source_basis"],
        accepted=export["accepted"],
        snapshot=Path(export["snapshot"]),
        patch=Path(export["patch"]),
        artifact_hashes=export["artifact_hashes"],
        protected_roots=tuple(Path(p) for p in export["protected_roots"]),
    )


def receipt_from_artifact(artifact: Path) -> ExportReceipt:
    """Rebuild a receipt from a sealed Core ``export.json``; ``verify_export`` still decides."""
    raw = artifact.read_bytes()
    payload = json.loads(raw)
    root = artifact.parent
    return ExportReceipt(
        artifact=artifact,
        artifact_sha256=hashlib.sha256(raw).hexdigest(),
        work_id=payload["work_id"],
        candidate=CandidateRef(
            payload["work_id"], Path(payload["candidate_artifact"]), payload["checkpoint"], payload["tree"]
        ),
        source_workspace=Path(payload["source_workspace"]),
        source_basis=payload["source_basis"],
        accepted=payload["accepted"],
        snapshot=root / "snapshot",
        patch=root / "candidate.patch",
        artifact_hashes=payload["artifact_hashes"],
        protected_roots=tuple(Path(p) for p in payload["protected_roots"]),
    )


def _candidate_value(candidate: CandidateRef) -> dict[str, str]:
    return {
        "work_id": candidate.work_id,
        "artifact": str(candidate.artifact),
        "checkpoint": candidate.checkpoint,
        "tree": candidate.tree,
    }


def _candidate_key(value: dict[str, Any]) -> tuple[str, str, str, str] | None:
    try:
        work_id, artifact, checkpoint, tree = (
            value["work_id"],
            value["artifact"],
            value["checkpoint"],
            value["tree"],
        )
    except (KeyError, TypeError):
        return None
    if not all(isinstance(item, (str, Path)) and str(item) for item in (work_id, artifact, checkpoint, tree)):
        return None
    return str(work_id), str(artifact), str(checkpoint), str(tree)


def _same_candidate(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return _candidate_key(left) is not None and _candidate_key(left) == _candidate_key(right)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
            return None
        value = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _verified(receipt: ExportReceipt) -> bool:
    try:
        verify_export(receipt)
    except Exception:
        return False
    return True


def _digest(path: Path) -> str | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        return sha256_file(path)
    except OSError:
        return None


def _global_sequence(value: Any) -> int | None:
    """Return a persisted global order, rejecting booleans and legacy counters."""
    sequence = value.get("global_sequence") if isinstance(value, dict) else None
    return sequence if type(sequence) is int and sequence >= 0 else None


def _gate_attempts(output: Path) -> list[dict[str, Any]]:
    """Every recorded public-gate attempt in its persisted global order.

    Gate files are invocation-scoped, so a file name is not an order. New
    attempts carry the controller's global sequence. Legacy attempts without
    one keep their old persisted order and rank after sequenced attempts.
    """
    rows: list[tuple[int, int, int, int, dict[str, Any]]] = []
    paths = sorted((output / "candidate-gates").glob("*.json"), key=str)
    for position, path in enumerate(paths):
        gate = _read_json(path)
        if gate is None or gate.get("schema") != "long-horizon/public-candidate-gate/v1":
            continue
        gate_sequence = gate.get("sequence")
        for index, attempt in enumerate(gate.get("attempts") or []):
            if not isinstance(attempt, dict):
                continue
            location = position * 1_000_000 + index
            order = _global_sequence(attempt)
            if order is not None:
                rows.append((0, 0, order, location, attempt))
                continue
            legacy = attempt.get("sequence")
            if type(legacy) is not int:
                legacy = gate_sequence if type(gate_sequence) is int else None
            if legacy is None:
                rows.append((1, 0, 0, location, attempt))
            else:
                rows.append((1, 1, legacy, location, attempt))
    rows.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
    return [item[4] for item in rows]


def _gate_positions(
    attempts: list[dict[str, Any]],
) -> tuple[dict[tuple[str, str, str, str], int], dict[tuple[str, str, str, str], Any]]:
    """Map each exact candidate to its newest global gate order and stage."""
    positions: dict[tuple[str, str, str, str], int] = {}
    stages: dict[tuple[str, str, str, str], Any] = {}
    for attempt in attempts:
        candidate = attempt.get("candidate")
        key = _candidate_key(candidate) if isinstance(candidate, dict) else None
        if key is None:
            continue
        order = _global_sequence(attempt)
        if order is None:
            continue
        if key not in positions or order >= positions[key]:
            positions[key] = order
            stages[key] = attempt.get("stage")
    return positions, stages


def _legacy_gate_positions(attempts: list[dict[str, Any]]) -> dict[tuple[str, str, str, str], int]:
    """Keep the old gate/archive comparison inside the unsequenced group."""
    positions: dict[tuple[str, str, str, str], int] = {}
    for index, attempt in enumerate(attempts):
        candidate = attempt.get("candidate")
        key = _candidate_key(candidate) if isinstance(candidate, dict) else None
        if key is None:
            continue
        sequence = attempt.get("sequence")
        positions[key] = sequence if type(sequence) is int else index + 1
    return positions


def _newest_attempts(attempts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Yield attempts newest first, with legacy records below sequenced ones."""
    sequenced = [
        (order, index, attempt)
        for index, attempt in enumerate(attempts)
        if (order := _global_sequence(attempt)) is not None
    ]
    if sequenced:
        sequenced.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [item[2] for item in sequenced]
    return list(reversed(attempts))


def _checked_candidate(attempt: dict[str, Any]) -> tuple[dict[str, Any], str] | None:
    """Return the exact candidate and evidence digest only for a passed eligible attempt."""
    if attempt.get("eligible") is not True or attempt.get("status") != "passed":
        return None
    candidate = attempt.get("candidate")
    if not isinstance(candidate, dict):
        return None
    verification = attempt.get("verification")
    if not isinstance(verification, dict) or verification.get("status") != "passed":
        return None
    checked = verification.get("candidate")
    if not isinstance(checked, dict) or not _same_candidate(candidate, checked):
        return None
    recorded = attempt.get("verification_sha256")
    if not isinstance(recorded, str) or not recorded:
        # A missing recorded digest cannot be reconstructed safely: the current
        # bytes could already have been replaced.
        return None
    artifact = verification.get("artifact")
    if not isinstance(artifact, str):
        return None
    actual = _digest(Path(artifact))
    if actual is None or actual != recorded:
        return None
    return candidate, recorded


def _report_public_pass(report: dict[str, Any], candidate: CandidateRef) -> bool:
    """Only a full candidate plus its recorded passing evidence may qualify."""
    if report.get("generation_status") != "candidate_validated":
        return False
    reported = report.get("candidate")
    expected = _candidate_value(candidate)
    if not isinstance(reported, dict) or not _same_candidate(reported, expected):
        return False
    verification = report.get("public_verification")
    if not isinstance(verification, dict) or verification.get("status") != "passed":
        return False
    checked = verification.get("candidate")
    if not isinstance(checked, dict) or not _same_candidate(checked, expected):
        return False
    artifact = verification.get("artifact")
    digest = report.get("public_verification_sha256")
    if not isinstance(artifact, str) or not isinstance(digest, str) or not digest:
        return False
    return _digest(Path(artifact)) == digest


def _archive_sequence(receipt: ExportReceipt) -> int:
    sidecar = _read_json(receipt.artifact.parent / "archive.json")
    if sidecar is not None and sidecar.get("schema") == _ARCHIVE_ENTRY_SCHEMA:
        sequence = sidecar.get("sequence")
        if type(sequence) is int:
            return sequence
    return 0


def _archive_global_sequence(receipt: ExportReceipt) -> int | None:
    sidecar = _read_json(receipt.artifact.parent / "archive.json")
    if sidecar is not None and sidecar.get("schema") == _ARCHIVE_ENTRY_SCHEMA:
        return _global_sequence(sidecar)
    return None


def _collect_receipts(output: Path, archive_roots: tuple[Path, ...]) -> list[tuple[int, ExportReceipt]]:
    """Read only caller-supplied trusted roots; never infer a candidate from a tree."""
    paths: list[Path] = []
    for pattern in ("exports/*/export.json", "stages/*/export.json"):
        paths.extend(output.glob(pattern))
    for root in archive_roots:
        if root.is_symlink() or not root.is_dir():
            continue
        paths.extend(root.glob("*/export.json"))
    seen: set[Path] = set()
    receipts: list[tuple[int, ExportReceipt]] = []
    for artifact in paths:
        try:
            key = artifact.resolve(strict=True)
        except OSError:
            continue
        if artifact.is_symlink() or key in seen:
            continue
        seen.add(key)
        try:
            receipt = receipt_from_artifact(artifact)
        except (OSError, KeyError, TypeError, ValueError):
            continue
        if not _verified(receipt):
            continue
        # Archive sidecars carry their own durable sequence. Never reuse a gate
        # list index here: it is not a sequence and cannot be compared with an
        # archive counter.
        receipts.append((_archive_sequence(receipt), receipt))
    receipts.sort(key=lambda item: (item[0], str(item[1].artifact)))
    return receipts


def select_fallback(
    output: Path, report: dict[str, Any] | None, *, archive_roots: tuple[Path, ...] = ()
) -> tuple[ExportReceipt, dict[str, Any]] | None:
    """Choose the best already-qualified sealed export, or ``None``.

    Order: any Core-accepted sealed export (the published selection first when
    itself accepted), then a selection export bound to its publicly validated
    candidate, then the newest sealed export whose exact candidate passed the
    controller's public candidate gate. Gate evidence is matched by the full CandidateRef and
    its recorded verification digest, never by tree text alone.
    """
    chosen, mismatches = _select_fallback(output, report, archive_roots=archive_roots)
    if chosen is not None:
        chosen[1]["mismatches"] = mismatches
    return chosen


def _select_fallback(
    output: Path, report: dict[str, Any] | None, *, archive_roots: tuple[Path, ...] = ()
) -> tuple[tuple[ExportReceipt, dict[str, Any]] | None, list[dict[str, Any]]]:
    mismatches: list[dict[str, Any]] = []
    selection_receipt: ExportReceipt | None = None
    selection_public = False
    if report is not None and isinstance(report.get("export"), dict):
        try:
            receipt = receipt_from_payload(report["export"])
        except (KeyError, TypeError, ValueError):
            receipt = None
        if receipt is not None and _verified(receipt):
            if receipt.accepted:
                return (receipt, {"source": "selection_export", "basis": "core_accepted"}), mismatches
            selection_receipt = receipt
            selection_public = _report_public_pass(report, receipt.candidate)

    attempts = _gate_attempts(output)
    receipts = _collect_receipts(output, archive_roots)
    by_candidate: dict[tuple[str, str, str, str], list[tuple[int, ExportReceipt]]] = {}
    for item in receipts:
        key = _candidate_key(_candidate_value(item[1].candidate))
        if key is None:
            continue
        by_candidate.setdefault(key, []).append(item)

    checked: list[tuple[int, int, dict[str, Any], dict[str, Any], str]] = []
    for index, attempt in enumerate(attempts):
        value = _checked_candidate(attempt)
        if value is None:
            if attempt.get("eligible") is True:
                mismatches.append(
                    {
                        "reason": "gate_evidence_not_bound_to_passing_candidate",
                        "stage": attempt.get("stage"),
                        "candidate": attempt.get("candidate"),
                    }
                )
            continue
        order = _global_sequence(attempt)
        checked.append((order if order is not None else -1, index, attempt, value[0], value[1]))
    sequenced_checked = [item for item in checked if item[0] >= 0]
    if sequenced_checked:
        checked_newest = sorted(sequenced_checked, key=lambda item: (item[0], item[1]), reverse=True)
    else:
        checked_newest = list(reversed(checked))
    gate_positions, gate_stages = _gate_positions(attempts)
    legacy_gate_positions = _legacy_gate_positions(attempts)
    accepted: list[tuple[int | None, int, int, ExportReceipt]] = []
    for index, (legacy_sequence, receipt) in enumerate(receipts):
        if not receipt.accepted:
            continue
        key = _candidate_key(_candidate_value(receipt.candidate))
        orders: list[int] = []
        gate_position = gate_positions.get(key) if key is not None else None
        if gate_position is not None:
            orders.append(gate_position)
        archive_order = _archive_global_sequence(receipt)
        if archive_order is not None:
            orders.append(archive_order)
        if key is not None and key in legacy_gate_positions:
            legacy_order = legacy_gate_positions[key]
        else:
            legacy_order = legacy_sequence
        # One persisted global order covers every accepted export: a gate record
        # does not outrank a newer ungated export. A candidate's latest event
        # places it; legacy records without the field keep their old comparison.
        accepted.append((max(orders) if orders else None, legacy_order, index, receipt))
    sequenced = [item for item in accepted if item[0] is not None]
    if sequenced:
        # Every Core-accepted sealed export outranks a merely public-passing
        # selection report. Within that class, all candidates share this order.
        _order, _legacy, _index, receipt = max(sequenced, key=lambda item: (item[0], item[2]))
        basis: dict[str, Any] = {"source": "sealed_export", "basis": "core_accepted"}
        key = _candidate_key(_candidate_value(receipt.candidate))
        if key is not None and key in gate_positions:
            basis["gate_stage"] = gate_stages[key]
        return (receipt, basis), mismatches
    if accepted:
        _order, _legacy, _index, receipt = max(accepted, key=lambda item: (item[1], item[2]))
        basis = {"source": "sealed_export", "basis": "core_accepted"}
        key = _candidate_key(_candidate_value(receipt.candidate))
        if key is not None and key in gate_positions:
            basis["gate_stage"] = gate_stages[key]
        return (receipt, basis), mismatches
    if selection_receipt is not None and selection_public:
        return (
            selection_receipt,
            {"source": "selection_export", "basis": "public_check_passed"},
        ), mismatches

    for _order, _index, attempt, candidate, digest in checked_newest:
        key = _candidate_key(candidate)
        matches = by_candidate.get(key, []) if key is not None else []
        if not matches:
            mismatches.append(
                {
                    "reason": "checked_candidate_export_missing",
                    "stage": attempt.get("stage"),
                    "candidate": candidate,
                }
            )
            continue
        # The newest sealed copy of this exact candidate is equivalent; the
        # identity and evidence digest remain authoritative.
        receipt = matches[-1][1]
        return (
            receipt,
            {
                "source": "sealed_export",
                "basis": "public_check_passed",
                "gate_stage": attempt.get("stage"),
                "verification_sha256": digest,
            },
        ), mismatches
    return None, mismatches


def apply_export(
    receipt: ExportReceipt,
    task_root: Path,
    names: set[str],
    spec: dict,
    *,
    deadline_unix: float | None = None,
    observations: list[float] | None = None,
) -> tuple[set[str], set[str]]:
    """Copy a verified sealed snapshot over the task's managed regular files.

    A new export starts only when the hard clock can fit the observed copy time
    with a safety factor (or the fixed floor before the first observation).
    Copying itself stays atomic per file: temporary file first, then replace.
    """
    from jiuwenswarm.lh.workspace_files import copy_files, source_files

    if deadline_unix is not None:
        remaining = float(deadline_unix) - time.time()
        required = export_start_floor(observations)
        if remaining < required:
            raise ExportStartRefused(remaining_seconds=remaining, required_seconds=required)
    started = time.monotonic()
    try:
        selected, _ = source_files(receipt.snapshot)
        if "live_public_paths" in spec:
            from jiuwenswarm.lh.meridian_publication import publication_names

            selected = publication_names(selected, spec["live_public_paths"])
            names = publication_names(names, spec["live_public_paths"])
        copy_files(receipt.snapshot, task_root, selected, names)
        _verify_applied(receipt, task_root, selected, names)
        return selected, names
    finally:
        if observations is not None:
            observations.append(max(0.0, time.monotonic() - started))


def _verify_applied(receipt: ExportReceipt, task_root: Path, selected: set[str], managed: set[str]) -> None:
    """Check the delivered managed files, modes and removals against the receipt."""
    payload = _read_json(receipt.artifact)
    modes = payload.get("artifact_modes") if payload is not None else None
    if not isinstance(modes, dict):
        raise RuntimeStateError("delivered export has no recorded artifact modes")
    for name in sorted(selected):
        key = f"snapshot/{name}"
        expected = receipt.artifact_hashes.get(key)
        if not isinstance(expected, str):
            raise RuntimeStateError("delivered file is not declared by the export receipt")
        path = task_root / name
        if path.is_symlink() or not path.is_file():
            raise RuntimeStateError("delivered file is not a regular managed file")
        if sha256_file(path) != expected:
            raise RuntimeStateError("delivered file hash differs from the sealed snapshot")
        if path.stat().st_mode & 0o777 != modes.get(key):
            raise RuntimeStateError("delivered file mode differs from the sealed snapshot")
    for name in sorted(managed - selected):
        path = task_root / name
        if path.is_symlink() or path.exists():
            raise RuntimeStateError("a managed file removed from the snapshot still exists")


def fallback_delivery(
    *,
    output: Path,
    task_root: Path,
    names: set[str],
    spec: dict,
    evidence: Path,
    reason: str,
    deadline_unix: float,
    validate_arena: Any,
    archive_roots: tuple[Path, ...] = (),
    export_observations: list[float] | None = None,
) -> bool:
    """Apply the best qualified candidate after a terminal failure; always record the attempt."""
    record: dict[str, Any] = {
        "schema": FALLBACK_SCHEMA,
        "reason": reason,
        "delivered": False,
        "copied": False,
        "independent_reward": None,
        "acceptance_created": False,
    }
    try:
        if deadline_unix - time.time() <= 0:
            record["refused"] = "original_harbor_clock_expired"
            return False
        report = _read_json(output / "selection.json")
        chosen, mismatches = _select_fallback(output, report, archive_roots=archive_roots)
        if chosen is None:
            record["refused"] = "no_accepted_or_public_check_passed_candidate"
            record["mismatches"] = mismatches
            return False
        receipt, basis = chosen
        validate_arena("before_export")
        verify_export(receipt)
        selected, managed = apply_export(
            receipt,
            task_root,
            names,
            spec,
            deadline_unix=deadline_unix,
            observations=export_observations,
        )
        record.update(basis)
        record["mismatches"] = mismatches
        record.update(
            delivered=True,
            copied=True,
            verified_hashes=True,
            work_id=receipt.work_id,
            candidate_tree=receipt.candidate.tree,
            candidate_checkpoint=receipt.candidate.checkpoint,
            artifact_sha256=receipt.artifact_sha256,
            accepted=receipt.accepted,
            selected_files=len(selected),
            deleted_files=len(managed - selected),
        )
        record["harbor_backfill_delivery"] = {
            "stage": "fallback_delivery",
            "work_id": receipt.work_id,
            "checkpoint": receipt.candidate.checkpoint,
            "tree": receipt.candidate.tree,
            "artifact_sha256": receipt.artifact_sha256,
            "accepted": bool(receipt.accepted),
            "acceptance_created": False,
        }
        return True
    except ExportStartRefused as error:
        record["refused"] = "insufficient_time_for_export"
        record["remaining_seconds"] = round(error.remaining_seconds, 3)
        record["required_seconds"] = round(error.required_seconds, 3)
        return False
    except Exception as error:
        record["refused"] = "fallback_failed"
        record["failure_type"] = type(error).__name__[:128]
        return False
    finally:
        try:
            with (evidence / "fallback-delivery.json").open("x") as stream:
                json.dump(record, stream, sort_keys=True)
                stream.write("\n")
        except OSError:
            pass
