# Copyright (c) 2026 Chrys. All rights reserved.
# Adapted from Chrys 3f78185e298800b3e3f735f932a7ee2abb7bc25f; all inference uses native Jiuwen.

"""Immutable stage boundaries with source, transcript and accepted-artifact hashes."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from jiuwenswarm.long_horizon.clarification.compat import Workspace
from jiuwenswarm.long_horizon.clarification.compat import atomic_write_owner_only_bytes, atomic_write_owner_only_text
from jiuwenswarm.long_horizon.clarification.compat import ensure_owner_only_directory
from jiuwenswarm.long_horizon.clarification.snapshot import WorkspaceSnapshot, WorkspaceSnapshotter


def save_stage_checkpoint(
    root: Path,
    *,
    stage: str,
    status: str,
    workspace: Workspace,
    history: dict[str, Any],
    metadata: dict[str, Any],
    snapshot: WorkspaceSnapshot | None = None,
) -> Path:
    """Publish completion last; a partial capture is never a resumable boundary."""
    folder = root / "checkpoints" / f"{stage}-{uuid4().hex}"
    ensure_owner_only_directory(folder)
    if snapshot is None:
        snapshot = WorkspaceSnapshotter().capture(
            workspace, folder / "workspace", snapshot_id=folder.name, include_git_history=True
        )
    artifact_hashes: dict[str, str] = {}
    for pattern in (
        "0*-*/**/*.json",
        "0*-*/**/*.md",
        "clarification.private.json",
        "h0.private.json",
        "recovery-origin.json",
    ):
        for path in sorted(root.glob(pattern)):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(root)
            data = path.read_bytes()
            target = folder / "artifacts" / relative
            ensure_owner_only_directory(target.parent)
            atomic_write_owner_only_bytes(target, data)
            artifact_hashes[str(relative)] = hashlib.sha256(data).hexdigest()
    # Use relative paths so a complete turn directory can move between hosts.
    snapshot_path = Path(snapshot.artifact_root).relative_to(root)
    snapshot_assets: dict[str, str] = {}
    for item in snapshot.roots:
        if item.history_bundle:
            bundle = Path(item.history_bundle)
            relative = bundle.relative_to(snapshot.artifact_root)
            with bundle.open("rb") as stream:
                snapshot_assets[str(relative)] = hashlib.file_digest(stream, "sha256").hexdigest()
    for reference in snapshot.references:
        snapshot_assets[f"blobs/{reference.entry.content_hash}"] = reference.entry.content_hash
    payload = {
        "schema": "chrys/stage-checkpoint/v1",
        "stage": stage,
        "status": status,
        "metadata": metadata,
        "snapshot": {"path": str(snapshot_path), "manifest_sha256": snapshot.manifest_hash},
        "snapshot_assets": snapshot_assets,
        "history": history,
        "artifacts": artifact_hashes,
    }
    record = folder / "checkpoint.json"
    atomic_write_owner_only_text(record, json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n")
    atomic_write_owner_only_text(folder / "checkpoint.sha256", hashlib.sha256(record.read_bytes()).hexdigest() + "\n")
    return record


def verify_stage_checkpoint(record: Path) -> tuple[dict[str, Any], WorkspaceSnapshot]:
    """Check every byte before permitting any restoration side effect."""
    raw = record.read_bytes()
    if hashlib.sha256(raw).hexdigest() != record.with_suffix(".sha256").read_text().strip():
        raise ValueError("stage checkpoint record hash mismatch")
    payload = json.loads(raw)
    if payload.get("schema") != "chrys/stage-checkpoint/v1":
        raise ValueError("unsupported stage checkpoint")
    root = record.parent.parent.parent.resolve()
    snapshot_path = (root / payload["snapshot"]["path"]).resolve()
    if not snapshot_path.is_relative_to(root):
        raise ValueError("checkpoint snapshot escapes its turn directory")
    snapshot = WorkspaceSnapshotter.load(snapshot_path)
    if snapshot.manifest_hash != payload["snapshot"]["manifest_sha256"]:
        raise ValueError("checkpoint snapshot manifest hash mismatch")
    for relative, digest in payload["snapshot_assets"].items():
        path = (snapshot_path / relative).resolve()
        if not path.is_relative_to(snapshot_path):
            raise ValueError("checkpoint snapshot asset escapes its directory")
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
                raise ValueError("checkpoint snapshot asset hash mismatch")
    for item in snapshot.roots:
        for entry in item.entries:
            relative = Path(entry.relative_path)
            if relative.is_absolute() or ".." in relative.parts or ".git" in relative.parts:
                raise ValueError("unsafe checkpoint source path")
            data = (snapshot_path / "blobs" / entry.content_hash).read_bytes()
            if hashlib.sha256(data).hexdigest() != entry.content_hash or len(data) != entry.size:
                raise ValueError("checkpoint source blob hash mismatch")
    for relative, digest in payload["artifacts"].items():
        path = (record.parent / "artifacts" / relative).resolve()
        if not path.is_relative_to((record.parent / "artifacts").resolve()):
            raise ValueError("checkpoint artifact escapes its directory")
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("checkpoint accepted artifact hash mismatch")
    return payload, snapshot


def restore_stage_to_empty_directory(record: Path, destination: Path) -> dict[str, Any]:
    """Restore to a new directory only, keeping the original run untouched."""
    payload, snapshot = verify_stage_checkpoint(record)
    if len(snapshot.roots) != 1 or snapshot.references:
        raise ValueError("portable restoration currently requires one source root and no external reference files")
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("checkpoint restoration requires an empty destination")
    ensure_owner_only_directory(destination)
    mapped = replace(snapshot, roots=(replace(snapshot.roots[0], source_root=str(destination.resolve())),))
    WorkspaceSnapshotter().restore(mapped)
    return payload
