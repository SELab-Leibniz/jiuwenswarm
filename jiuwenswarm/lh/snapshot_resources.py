# Copyright (c) 2026 Chrys. All rights reserved.

"""Host-owned, per-invocation snapshot capacity shared by built-in artifact ports.

The native stage bridge preserves the sealed-export/checkpoint protocol from
Jiuwen 6cfe902's long_horizon_evidence.stage_record. It supplies the snapshotter's
existing capacity argument rather than changing native module globals.
"""

from __future__ import annotations

import shutil
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from pact_core.runtime import RuntimeContractError

from .capacity import capacity_receipt
from .evidence import contained_in_roots

DEFAULT_SNAPSHOT_MAX_TOTAL_BYTES = 1024 * 1024 * 1024
MAX_SNAPSHOT_TOTAL_BYTES = 2 * 1024 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class SnapshotResources:
    max_total_bytes: int = DEFAULT_SNAPSHOT_MAX_TOTAL_BYTES

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_total_bytes, bool)
            or not isinstance(self.max_total_bytes, int)
            or not 1 <= self.max_total_bytes <= MAX_SNAPSHOT_TOTAL_BYTES
        ):
            raise RuntimeContractError(
                f"snapshot_max_total_bytes must be an integer from 1 to {MAX_SNAPSHOT_TOTAL_BYTES}"
            )

    def check_total(self, total: int) -> None:
        if total > self.max_total_bytes:
            raise RuntimeContractError(f"workspace snapshot exceeds {self.max_total_bytes} bytes")

    def record(self) -> dict[str, object]:
        return {"policy": "bounded-snapshot-bytes-v1", "max_total_bytes": self.max_total_bytes}


def native_stage_record(
    evidence: Any,
    resources: SnapshotResources,
    *,
    output_root: Path,
    turn_root: Path,
    stage: str,
    receipt: Any,
    metadata: dict[str, object],
    archive_roots: tuple[Path, ...] = (),
) -> Any:
    """Freeze the exact Core export, retaining native symlink and checkpoint checks."""
    payload = evidence.verify_export(receipt)
    opaque = payload["opaque_symlinks"]
    artifact = turn_root / "snapshots" / f"{stage}-{uuid4().hex}"
    snapshotter = evidence.WorkspaceSnapshotter(max_total_bytes=resources.max_total_bytes)

    def capture(source: Path) -> Any:
        return snapshotter.capture(
            evidence.Workspace.from_cwd(str(source)),
            artifact,
            snapshot_id=f"{stage}-{receipt.candidate.tree[:16]}",
            include_git_history=False,
        )

    if opaque:
        evidence.ensure_owner_only_directory(turn_root)
        with tempfile.TemporaryDirectory(prefix="fixture-capture-", dir=turn_root) as directory:
            source = Path(directory) / "snapshot"
            shutil.copytree(receipt.snapshot, source, symlinks=True)
            if any(unicodedata.normalize("NFC", name).casefold() == ".gitignore" for name in opaque):
                raise RuntimeContractError("external ignore-file fixtures cannot be captured")
            for name, item in opaque.items():
                (source / name).symlink_to(item["target"])
            ignore_file = source / ".gitignore"
            if ignore_file.is_symlink() or (ignore_file.exists() and not ignore_file.is_file()):
                raise RuntimeContractError("external ignore-file fixtures cannot be captured")
            snapshot = capture(source)
            entries = {entry.relative_path: entry for entry in snapshot.roots[0].entries}
            for name in opaque:
                entry = entries.get(name)
                if entry is None or entry.kind != "symlink" or entry.model_visible:
                    raise RuntimeContractError("opaque fixture was omitted or exposed by snapshot capture")
    else:
        snapshot = capture(receipt.snapshot)
    resources.check_total(snapshot.total_bytes)
    checkpoint = evidence.save_stage_checkpoint(
        turn_root,
        stage=stage,
        status="sealed",
        workspace=snapshot.clarification_workspace(),
        history={},
        metadata={
            **metadata,
            "snapshot_resources": resources.record(),
            "capacity_profile": capacity_receipt(),
            "candidate_checkpoint": receipt.candidate.checkpoint,
            "candidate_tree": receipt.candidate.tree,
            "export_receipt_sha256": receipt.artifact_sha256,
            "patch_sha256": evidence.export_patch_sha256(receipt),
        },
        snapshot=snapshot,
    )
    export_location = contained_in_roots(receipt.artifact, output_root=output_root, archive_roots=archive_roots)
    return (
        {
            "stage": stage,
            "candidate_checkpoint": receipt.candidate.checkpoint,
            "candidate_tree": receipt.candidate.tree,
            "export": export_location["path"],
            "export_root": export_location["root"],
            "export_sha256": receipt.artifact_sha256,
            "patch_sha256": evidence.export_patch_sha256(receipt),
            "patch_empty": receipt.patch.stat().st_size == 0,
            "snapshot": evidence.contained(output_root, Path(snapshot.artifact_root)),
            "snapshot_manifest_sha256": snapshot.manifest_hash,
            "snapshot_resources": resources.record(),
            "checkpoint": evidence.contained(output_root, checkpoint),
            "checkpoint_sha256": evidence.sha256_file(checkpoint),
        },
        snapshot,
    )
