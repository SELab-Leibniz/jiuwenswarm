"""Self-contained stage evidence, published only after every copied byte verifies.

Source recovery uses the local clarification snapshot format. Native receipts
and session files are immutable evidence, not a promise that a live SDK session
can be resumed. Raw runtime configuration is fingerprinted but never copied.
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import shutil
import stat
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from .clarification.compat import (
    Workspace,
    _open_posix_directory,
    atomic_write_owner_only_bytes,
    ensure_owner_only_directory,
    validate_link_free_directory,
)
from .clarification.snapshot import (
    SnapshotEntry,
    WorkspaceSnapshot,
    WorkspaceSnapshotter,
)

SCHEMA = "jiuwenswarm/stage-checkpoint/v1"
_CONTROL_FILES = frozenset({"checkpoint.json", "checkpoint.sha256", "completed.json"})
_NATIVE_RECEIPTS = (
    "request.json", "metadata.json", "events.jsonl", "terminal-result.json",
    "final-response.json", "usage.json",
)
_NATIVE_CONFIG = ("native-config.before.yaml", "native-config.after.yaml")
_RAW_CONFIG_NAMES = frozenset({"config", "config.yaml", "config.yml", "credentials.json", ".env", *_NATIVE_CONFIG})
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
                       allow_nan=False) + "\n").encode("utf-8")


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _relative(raw: str) -> Path:
    if not isinstance(raw, str) or not raw or "\0" in raw:
        raise ValueError("invalid checkpoint relative path")
    path = Path(raw)
    if path.is_absolute() or any(part in {"..", ".git"} for part in path.parts) or path == Path("."):
        raise ValueError("unsafe checkpoint relative path")
    if path.as_posix() != raw:
        raise ValueError("checkpoint paths must be canonical relative paths")
    return path


def _read_regular(path: Path) -> bytes:
    """Refuse links, special files and files changing during capture/validation."""
    parent_fd = _open_posix_directory(path.parent, create=False)
    try:
        before = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"checkpoint input must be a regular non-symlink file: {path.name}")
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), dir_fd=parent_fd)
        with os.fdopen(fd, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise ValueError("checkpoint input changed before capture")
            data = stream.read()
        after = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
    finally:
        os.close(parent_fd)
    def identity(item):
        return item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_mode
    if identity(before) != identity(after) or len(data) != before.st_size:
        raise ValueError("checkpoint input changed during capture")
    return data


def _write(path: Path, data: bytes) -> None:
    ensure_owner_only_directory(path.parent)
    if path.exists() or path.is_symlink():
        if _read_regular(path) != data:
            raise ValueError("conflicting checkpoint copies")
        return
    atomic_write_owner_only_bytes(path, data)


def _inside(root: Path, path: Path) -> Path:
    path = Path(os.path.abspath(path))
    if not path.is_relative_to(root) or path == root:
        raise ValueError("checkpoint input escapes its run directory")
    validate_link_free_directory(path.parent)
    return path


def _copy_tree(source: Path, destination: Path, *, skip_state: bool) -> dict[str, str]:
    """Copy only plain files; caller artifacts never pull in raw runtime state."""
    copied: dict[str, str] = {}

    def visit(path: Path, target: Path, relative: str) -> None:
        if path.name in _RAW_CONFIG_NAMES or path.name.startswith(".env."):
            return
        if path.name in {".git", "checkpoints"} or (skip_state and path.name == "state"):
            return
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ValueError("checkpoint artifacts must not contain symlinks")
        if stat.S_ISDIR(info.st_mode):
            validate_link_free_directory(path)
            for child in sorted(path.iterdir()):
                visit(child, target / child.name, f"{relative}/{child.name}" if relative else child.name)
        elif stat.S_ISREG(info.st_mode):
            data = _read_regular(path)
            _write(target, data)
            copied[relative] = _digest(data)
        else:
            raise ValueError("checkpoint artifacts must not contain special files")

    visit(source, destination, "")
    return copied


def _checked_entry(entry: SnapshotEntry) -> Path:
    relative = _relative(entry.relative_path)
    if not _DIGEST.fullmatch(entry.content_hash) or entry.kind not in {"file", "symlink"}:
        raise ValueError("invalid checkpoint source entry")
    if entry.size < 0 or entry.mode < 0 or entry.mode > 0o7777:
        raise ValueError("invalid checkpoint source metadata")
    return relative


def _link_target(relative: Path, data: bytes, original_root: str) -> str:
    target = os.fsdecode(data)
    if not target or "\0" in target:
        raise ValueError("invalid checkpoint source symlink")
    if os.path.isabs(target):
        try:
            target = str(Path(target).relative_to(original_root))
        except ValueError as exc:
            raise ValueError("checkpoint model view symlink escapes source root") from exc
    else:
        target = posixpath.join(relative.parent.as_posix(), target)
    normalized = posixpath.normpath(target)
    if normalized == ".." or normalized.startswith(("../", "/")):
        raise ValueError("checkpoint model view symlink escapes source root")
    if ".git" in Path(normalized).parts:
        raise ValueError("checkpoint model view symlink enters Git metadata")
    return posixpath.relpath(normalized, relative.parent.as_posix())


def _copy_snapshot(snapshot: WorkspaceSnapshot, destination: Path) -> tuple[WorkspaceSnapshot, dict]:
    source = Path(snapshot.artifact_root)
    validate_link_free_directory(source)
    original = WorkspaceSnapshotter.load(source, expected_manifest_hash=snapshot.manifest_hash)
    ensure_owner_only_directory(destination)
    roots, references = [], []

    def copy_blob(entry: SnapshotEntry) -> bytes:
        _checked_entry(entry)
        data = _read_regular(source / "blobs" / entry.content_hash)
        if _digest(data) != entry.content_hash or len(data) != entry.size:
            raise ValueError("checkpoint source blob hash mismatch")
        _write(destination / "blobs" / entry.content_hash, data)
        return data

    def model_entry(view: Path, entry: SnapshotEntry, original_root: str) -> None:
        data = copy_blob(entry)
        if not entry.model_visible:
            return
        relative = _checked_entry(entry)
        target = view / relative
        ensure_owner_only_directory(target.parent)
        if entry.kind == "file":
            _write(target, data)
            target.chmod(0o500 if entry.mode & 0o111 else 0o400)
        else:
            os.symlink(_link_target(relative, data, original_root), target,
                       target_is_directory=entry.symlink_target_is_dir)

    for index, item in enumerate(original.roots):
        view_relative = f"roots/{index}/view"
        view = destination / view_relative
        ensure_owner_only_directory(view)
        seen: set[str] = set()
        # Source blobs are authoritative. Mutable original model views, their
        # Git config and any symlinks into the original run are never copied.
        for entry in sorted(item.entries, key=lambda entry: (entry.kind == "symlink", entry.relative_path)):
            if entry.relative_path in seen:
                raise ValueError("duplicate checkpoint source path")
            seen.add(entry.relative_path)
            model_entry(view, entry, item.source_root)
        bundle = ""
        if item.history_bundle:
            # The original manifest can retain a path from a different host.
            # Canonical content lives beside the manifest, never at that path.
            path = source / "roots" / str(index) / "history.bundle"
            bundle = f"roots/{index}/history.bundle"
            _write(destination / bundle, _read_regular(path))
        index_blob = ""
        if item.git_index_sha256 or item.git_index_blob:
            if not _DIGEST.fullmatch(item.git_index_sha256):
                raise ValueError("invalid checkpoint Git index digest")
            data = _read_regular(source / "blobs" / item.git_index_sha256)
            if _digest(data) != item.git_index_sha256:
                raise ValueError("checkpoint Git index hash mismatch")
            index_blob = f"blobs/{item.git_index_sha256}"
            _write(destination / index_blob, data)
        roots.append(replace(item, view_root=view_relative, history_bundle=bundle, git_index_blob=index_blob))
    for index, reference in enumerate(original.references):
        if reference.managed_by_root:
            match = next((i for i, item in enumerate(original.roots)
                          if Path(reference.view_path).is_relative_to(item.view_root)), None)
            if match is None:
                raise ValueError("managed checkpoint reference has no source root")
            relative = _relative(Path(reference.view_path).relative_to(original.roots[match].view_root).as_posix())
            view_path = f"roots/{match}/view/{relative.as_posix()}"
            copy_blob(reference.entry)
        else:
            view_root = Path(f"roots/references/{index}")
            model_entry(destination / view_root, reference.entry, str(Path(reference.source_path).parent))
            view_path = (view_root / _checked_entry(reference.entry)).as_posix()
        references.append(replace(reference, view_path=view_path))
    manifest = {"snapshot_id": original.snapshot_id, "total_bytes": original.total_bytes,
                "entry_count": original.entry_count, "roots": [asdict(item) for item in roots],
                "references": [asdict(item) for item in references]}
    _write(destination / "manifest.json", _json_bytes(manifest))
    copied = WorkspaceSnapshotter.load(destination)
    return copied, {"original_manifest_sha256": original.manifest_hash,
                    "source_heads": [item.git_head for item in original.roots]}


def _relocate_snapshot(snapshot: WorkspaceSnapshot) -> WorkspaceSnapshot:
    folder = Path(snapshot.artifact_root)
    roots = tuple(replace(item, view_root=str(folder / _relative(item.view_root)),
                          history_bundle=str(folder / _relative(item.history_bundle)) if item.history_bundle else "",
                          git_index_blob=str(folder / _relative(item.git_index_blob)) if item.git_index_blob else "")
                  for item in snapshot.roots)
    references = tuple(replace(item, view_path=str(folder / _relative(item.view_path)))
                       for item in snapshot.references)
    return replace(snapshot, roots=roots, references=references)


def copy_snapshot(snapshot: WorkspaceSnapshot, destination: Path) -> tuple[WorkspaceSnapshot, dict]:
    """Copy portable source evidence independently of a checkpoint publication.

    The caller must publish its own completion record. Returned ``files`` hashes
    cover every byte-bearing asset; original host paths are never read.
    """
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError("snapshot copy requires a new destination")
    copied, evidence = _copy_snapshot(snapshot, destination)
    evidence["files"] = {name: asset["sha256"] for name, asset in
                         _inventory(destination, snapshot_only=True).items()}
    return _relocate_snapshot(copied), evidence


def _capture_native(root: Path, folder: Path, calls: list[dict]) -> list[dict]:
    captured = []
    for index, call in enumerate(calls):
        output = _inside(root, Path(call["output_dir"]))
        destination = folder / "native" / str(index)
        copied: dict[str, str] = {}
        config_fingerprints = {}
        receipt = {}
        for name in _NATIVE_RECEIPTS:
            source = output / name
            if source.exists() or source.is_symlink():
                data = _read_regular(source)
                _write(destination / name, data)
                copied[name] = _digest(data)
                if name == "metadata.json":
                    receipt = json.loads(data)
        # Native can change its effective config during execution. Fingerprint
        # even nominally sanitized snapshots rather than trusting their names
        # or accidentally exporting newly resolved credential values.
        for name in _NATIVE_CONFIG:
            source = output / name
            if source.exists() or source.is_symlink():
                config_fingerprints[name] = _digest(_read_regular(source))
        state_path = receipt.get("state_dir")
        state_files: dict[str, str] = {}
        config_digest = None
        if state_path:
            state = _inside(root, Path(state_path))
            validate_link_free_directory(state)
            config = state / "config/config.yaml"
            if config.exists() or config.is_symlink():
                config_digest = _digest(_read_regular(config))
            session_id = receipt.get("session_id") or call.get("session_id")
            if session_id:
                if not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", session_id) or session_id in {".", ".."}:
                    raise ValueError("unsafe native checkpoint session id")
                for relative in (f"agent/sessions/{session_id}",
                                 f"agent/workspace/todo/{session_id}",
                                 f"agent/workspace/messages/{session_id}"):
                    source = state / relative
                    if source.exists() or source.is_symlink():
                        entries = _copy_tree(source, destination / "state" / relative, skip_state=False)
                        state_files.update({f"{relative}/{key}" if key else relative: value
                                            for key, value in entries.items()})
        captured.append({"call": call, "path": f"native/{index}", "receipts": copied,
                         "state_files": state_files, "raw_config_sha256": config_digest,
                         "config_fingerprints": config_fingerprints,
                         "state_policy": "matching session evidence only; raw config excluded"})
    return captured


def _inventory(folder: Path, *, snapshot_only: bool = False) -> dict[str, dict]:
    assets = {}

    def walk(directory: Path) -> None:
        validate_link_free_directory(directory)
        for path in sorted(directory.iterdir()):
            relative = path.relative_to(folder).as_posix()
            if relative in _CONTROL_FILES:
                continue
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode):
                walk(path)
                continue
            if stat.S_ISLNK(info.st_mode):
                # Only rebuilt model views may contain links. Never follow
                # them to collect bytes or allow a link outside its view.
                parts = Path(relative).parts
                prefix = () if snapshot_only else ("workspace",)
                offset = len(prefix)
                if (len(parts) < offset + 4 or parts[:offset + 1] != (*prefix, "roots")
                        or parts[offset + 2] != "view"):
                    raise ValueError("unexpected symlink in checkpoint assets")
                view = folder.joinpath(*parts[:offset + 3])
                data = os.fsencode(os.readlink(path))
                if os.path.isabs(os.fsdecode(data)) or not path.resolve().is_relative_to(view):
                    raise ValueError("checkpoint view symlink escapes its directory")
                kind = "symlink"
            else:
                data = _read_regular(path)
                kind = "file"
            assets[relative] = {"kind": kind, "sha256": _digest(data), "size": len(data),
                                "mode": stat.S_IMODE(info.st_mode)}

    walk(folder)
    return assets


def save_stage_checkpoint(
    root: Path, *, stage: str, status: str, workspace: Path, metadata: dict,
    native_calls: list[dict], artifact_paths: list[Path], snapshot: WorkspaceSnapshot | None = None,
) -> Path:
    """Capture an immutable boundary; an interrupted capture has no completion marker."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", stage):
        raise ValueError("invalid checkpoint stage")
    root = Path(root).resolve()
    folder = root / "checkpoints" / f"{stage}-{uuid4().hex}"
    ensure_owner_only_directory(folder)
    temporary = folder / "source-capture"
    if snapshot is None:
        snapshot = WorkspaceSnapshotter().capture(
            Workspace.from_cwd(str(workspace)), temporary, snapshot_id=folder.name, include_git_history=True,
        )
    copied_snapshot, source_evidence = _copy_snapshot(snapshot, folder / "workspace")
    if temporary.exists():
        shutil.rmtree(temporary)
    artifacts = {}
    for raw_path in artifact_paths:
        path = _inside(root, Path(raw_path))
        relative = _relative(path.relative_to(root).as_posix())
        if relative.parts[0] == "checkpoints":
            raise ValueError("a checkpoint cannot capture another checkpoint")
        copied = _copy_tree(path, folder / "artifacts" / relative, skip_state=True)
        artifacts.update({f"{relative.as_posix()}/{key}" if key else relative.as_posix(): digest
                          for key, digest in copied.items()})
    native = _capture_native(root, folder, native_calls)
    assets = _inventory(folder)
    payload = {"schema": SCHEMA, "stage": stage, "status": status, "metadata": metadata,
               "snapshot": {"path": "workspace", "manifest_sha256": copied_snapshot.manifest_hash,
                            **source_evidence}, "native_calls": native, "artifacts": artifacts,
               "assets": assets}
    record = folder / "checkpoint.json"
    raw = _json_bytes(payload)
    _write(record, raw)
    _write(folder / "checkpoint.sha256", (_digest(raw) + "\n").encode("ascii"))
    _verify(record, require_completed=False)
    # This is the publication point. A valid-looking record alone is never
    # accepted after an interrupted/failed capture.
    _write(folder / "completed.json", _json_bytes({"schema": SCHEMA, "checkpoint_sha256": _digest(raw)}))
    return record


def _verify(record: Path, *, require_completed: bool) -> tuple[dict, WorkspaceSnapshot]:
    record = Path(record)
    if record.name != "checkpoint.json":
        raise ValueError("stage checkpoint record must be checkpoint.json")
    raw = _read_regular(record)
    digest = _digest(raw)
    if _read_regular(record.with_suffix(".sha256")).decode("ascii").strip() != digest:
        raise ValueError("stage checkpoint record hash mismatch")
    if require_completed:
        completed = json.loads(_read_regular(record.parent / "completed.json"))
        if completed != {"schema": SCHEMA, "checkpoint_sha256": digest}:
            raise ValueError("stage checkpoint completion marker mismatch")
    payload = json.loads(raw)
    if payload.get("schema") != SCHEMA or payload.get("snapshot", {}).get("path") != "workspace":
        raise ValueError("unsupported stage checkpoint")
    if not isinstance(payload.get("assets"), dict) or _inventory(record.parent) != payload["assets"]:
        raise ValueError("checkpoint asset byte manifest mismatch")
    folder = record.parent / "workspace"
    snapshot = WorkspaceSnapshotter.load(folder, expected_manifest_hash=payload["snapshot"]["manifest_sha256"])
    roots = []
    for index, item in enumerate(snapshot.roots):
        if item.view_root != f"roots/{index}/view":
            raise ValueError("unsafe checkpoint snapshot view")
        for entry in item.entries:
            _checked_entry(entry)
        bundle = str(folder / _relative(item.history_bundle)) if item.history_bundle else ""
        if bundle and f"workspace/{item.history_bundle}" not in payload["assets"]:
            raise ValueError("checkpoint history bundle is absent")
        index_blob = str(folder / _relative(item.git_index_blob)) if item.git_index_blob else ""
        if index_blob and _digest(_read_regular(Path(index_blob))) != item.git_index_sha256:
            raise ValueError("checkpoint Git index hash mismatch")
        roots.append(replace(item, view_root=str(folder / item.view_root), history_bundle=bundle,
                             git_index_blob=index_blob))
    references = tuple(replace(item, view_path=str(folder / _relative(item.view_path)))
                       for item in snapshot.references)
    for item in references:
        _checked_entry(item.entry)
    snapshot = replace(snapshot, roots=tuple(roots), references=references)
    WorkspaceSnapshotter._validate_recovery_blobs(snapshot)
    expected_artifacts = {key.removeprefix("artifacts/"): value["sha256"]
                          for key, value in payload["assets"].items() if key.startswith("artifacts/")}
    if payload.get("artifacts") != expected_artifacts:
        raise ValueError("checkpoint artifact manifest mismatch")
    return payload, snapshot


def verify_stage_checkpoint(record: Path) -> tuple[dict, WorkspaceSnapshot]:
    """Verify all assets without consulting original source or mutable run paths."""
    return _verify(record, require_completed=True)


def restore_stage_to_empty_directory(record: Path, destination: Path) -> dict:
    """Restore frozen source files into one new directory, never original paths.

    Git history/index are evidence only, matching WorkspaceSnapshotter.restore.
    Native sessions are not started and no evaluation/grading is performed.
    """
    payload, snapshot = verify_stage_checkpoint(record)
    if len(snapshot.roots) != 1 or snapshot.references:
        raise ValueError("portable restoration requires one source root and no reference files")
    destination = Path(os.path.abspath(destination))
    validate_link_free_directory(destination.parent)
    if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
        raise ValueError("checkpoint restoration requires an empty directory")
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("checkpoint restoration requires an empty destination")
    # Validate links before creating anything. Relative in-tree links preserve
    # exact source bytes; absolute/outbound links cannot be restored portably.
    for entry in snapshot.roots[0].entries:
        if entry.kind == "symlink":
            data = _read_regular(Path(snapshot.artifact_root) / "blobs" / entry.content_hash)
            if os.path.isabs(os.fsdecode(data)):
                raise ValueError("portable restoration does not support absolute source symlinks")
            _link_target(_checked_entry(entry), data, snapshot.roots[0].source_root)
    ensure_owner_only_directory(destination)
    mapped = replace(snapshot, roots=(replace(snapshot.roots[0], source_root=str(destination)),))
    WorkspaceSnapshotter().restore(mapped)
    return payload
