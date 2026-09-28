# Copyright (c) 2026 Chrys. All rights reserved.

"""Immutable, owner-only workspace snapshots for clarification and repair.

Ported from Chrys ``src/chrys/service/requirement_clarification/snapshot.py``
at revision 6fcfb3321892735631b49f33236b1e2a988ded2d. The compatibility
boundary is local to this suite; no Chrys runtime or model is imported.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import stat
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final, Literal

from .compat import (
    DEFAULT_EXCLUDES,
    EncodingDetector,
    GitignoreFilter,
    WorkingDir,
    Workspace,
    atomic_write_owner_only_bytes,
    atomic_write_owner_only_text,
    ensure_owner_only_directory,
    get_platform,
    validate_link_free_directory,
)

SNAPSHOT_MAX_ENTRIES_PER_ROOT: Final[int] = 50_000
SNAPSHOT_MAX_FILE_BYTES_FOR_MODEL: Final[int] = 50 * 1024 * 1024
SNAPSHOT_MAX_TOTAL_BYTES: Final[int] = 512 * 1024 * 1024
SNAPSHOT_SAMPLE_BYTES: Final[int] = 8192
SNAPSHOT_MANIFEST_NAME: Final[str] = "manifest.json"
SNAPSHOT_VIEW_MANIFEST_NAME: Final[str] = ".jiuwenswarm_snapshot_manifest.json"

_PATTERN_EXCLUDES = (".egg-info",)


class WorkspaceSnapshotError(OSError):
    """Raised when a complete, stable snapshot cannot be produced or restored."""


@dataclass(frozen=True, slots=True)
class SnapshotEntry:
    """One source entry captured in the content-addressed recovery layer."""

    relative_path_b64: str
    kind: Literal["file", "symlink"]
    mode: int
    size: int
    content_hash: str
    model_visible: bool
    metadata_reason: str = ""
    symlink_target_is_dir: bool = False
    # The recovery digest above binds the original live link. Model views
    # remap internal links, so their exact target needs a separate identity.
    model_symlink_target: str | None = None

    @property
    def relative_path(self) -> str:
        return os.fsdecode(base64.b64decode(self.relative_path_b64.encode("ascii")))


@dataclass(frozen=True, slots=True)
class SnapshotRoot:
    """Mapping between one live workspace root and its frozen model view."""

    source_root: str
    view_root: str
    label: str
    is_primary: bool
    entries: tuple[SnapshotEntry, ...]
    git_head: str = ""
    history_bundle: str = ""
    git_index_sha256: str = ""
    git_index_blob: str = ""
    tracked_paths: tuple[str, ...] = ()
    ignore_text: str = ""
    # None identifies legacy recovery snapshots, which remain loadable and
    # restorable but cannot attest to a complete model-view inventory.
    model_view_directories: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class SnapshotReference:
    """One explicitly configured reference file and its frozen view."""

    source_path: str
    view_path: str
    entry: SnapshotEntry
    managed_by_root: bool


@dataclass(frozen=True, slots=True)
class WorkspaceSnapshot:
    """A complete recovery snapshot plus a bounded clarification view."""

    snapshot_id: str
    artifact_root: str
    roots: tuple[SnapshotRoot, ...]
    manifest_hash: str
    total_bytes: int
    entry_count: int
    references: tuple[SnapshotReference, ...] = ()

    def clarification_workspace(self) -> Workspace:
        if not self.roots:
            raise WorkspaceSnapshotError("snapshot has no workspace roots")
        working_dirs = [
            WorkingDir(path=root.view_root, label=root.label, is_primary=root.is_primary) for root in self.roots
        ]
        primary = next((root.view_root for root in self.roots if root.is_primary), self.roots[0].view_root)
        return Workspace(
            primary_cwd=primary,
            working_dirs=working_dirs,
            reference_files=[reference.view_path for reference in self.references if reference.entry.model_visible],
        )


def _encoded_relative_path(path: str) -> str:
    return base64.b64encode(os.fsencode(path)).decode("ascii")


def _is_excluded_name(name: str) -> bool:
    return name in DEFAULT_EXCLUDES or any(name.endswith(suffix) for suffix in _PATTERN_EXCLUDES)


def _is_excluded_relative(relative_path: str) -> bool:
    return any(_is_excluded_name(part) for part in Path(relative_path).parts)


def _stable_file_bytes(path: Path, expected: os.stat_result) -> bytes:
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as file:
            opened = os.fstat(file.fileno())
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
                expected.st_dev, expected.st_ino
            ):
                raise WorkspaceSnapshotError(f"workspace entry changed before capture: {path}")
            data = file.read()
        after = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise WorkspaceSnapshotError(f"failed to capture {path}: {exc}") from exc
    before_identity = (expected.st_dev, expected.st_ino, expected.st_size, expected.st_mtime_ns)
    after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if before_identity != after_identity or len(data) != expected.st_size:
        raise WorkspaceSnapshotError(f"workspace entry changed while being snapshotted: {path}")
    return data


def _write_blob(blob_root: Path, digest: str, data: bytes) -> None:
    path = blob_root / digest
    if path.exists():
        return
    atomic_write_owner_only_bytes(path, data)


def _safe_view_destination(view_root: Path, relative_path: str) -> Path:
    if not relative_path or Path(relative_path).is_absolute() or ".." in Path(relative_path).parts:
        raise WorkspaceSnapshotError(f"invalid snapshot relative path: {relative_path}")
    destination = view_root / relative_path
    root_real = os.path.realpath(view_root)
    parent_real = os.path.realpath(destination.parent)
    try:
        common = os.path.commonpath((root_real, parent_real))
    except ValueError as exc:
        raise WorkspaceSnapshotError(f"snapshot path escapes its root: {relative_path}") from exc
    if os.path.normcase(common) != os.path.normcase(root_real):
        raise WorkspaceSnapshotError(f"snapshot path escapes its root: {relative_path}")
    return destination


def _ensure_restore_parent(directory: Path) -> None:
    """Create a parent inside the live workspace without touching its mode.

    ``ensure_owner_only_directory`` is right for the snapshot's private tree
    and wrong here: restoring a file must not chmod the user's own source
    directories to 0700 as a side effect.
    """
    validate_link_free_directory(directory, create=True)


def _restore_parent_tree(source_root: Path, relative_path: str) -> None:
    """Restore parent directory types without following repair-created links."""
    if not relative_path or Path(relative_path).is_absolute() or ".." in Path(relative_path).parts:
        raise WorkspaceSnapshotError(f"invalid snapshot relative path: {relative_path}")
    parent = source_root
    for name in Path(relative_path).parts[:-1]:
        parent = parent / name
        if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
            parent.unlink()
        parent.mkdir(exist_ok=True)


def _materialize_model_entry(
    *,
    view_root: Path,
    relative_path: str,
    data: bytes,
    mode: int,
) -> None:
    destination = _safe_view_destination(view_root, relative_path)
    ensure_owner_only_directory(destination.parent)
    atomic_write_owner_only_bytes(destination, data)
    if not get_platform().is_windows:
        readable_mode = 0o500 if mode & 0o111 else 0o400
        destination.chmod(readable_mode)


def _git_output(source_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    git = shutil.which("git")
    if git is None:
        raise WorkspaceSnapshotError("git executable is unavailable")
    return subprocess.run(  # noqa: S603 - executable and arguments are code-owned
        [git, "-C", str(source_root), *args],
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        timeout=60,
    )


def _git_evidence(source_root: Path, blob_root: Path) -> tuple[str, str, str, tuple[str, ...], int]:
    """Record HEAD, index bytes and tracked scope without updating source refs.

    The index blob is evidence, not an automatic Git rollback mechanism. A
    split index can refer to a shared index in the live Git directory.
    """
    if shutil.which("git") is None:
        return "", "", "", (), 0
    inside = _git_output(source_root, "rev-parse", "--is-inside-work-tree")
    if inside.returncode or inside.stdout.strip() != "true":
        return "", "", "", (), 0
    head = _git_output(source_root, "rev-parse", "--verify", "HEAD^{commit}")
    inventory = subprocess.run(
        [shutil.which("git"), "-C", str(source_root), "ls-files", "--cached", "-z"],
        stdin=subprocess.DEVNULL, capture_output=True, check=True, timeout=60,
    )
    tracked = tuple(sorted({os.fsdecode(item) for item in inventory.stdout.split(b"\0") if item}))
    index_result = _git_output(source_root, "rev-parse", "--path-format=absolute", "--git-path", "index")
    if index_result.returncode:
        raise WorkspaceSnapshotError(f"failed to locate Git index: {index_result.stderr.strip()}")
    index_path = Path(index_result.stdout.strip())
    if not index_path.exists():
        return head.stdout.strip() if head.returncode == 0 else "", "", "", tracked, 0
    data = _stable_file_bytes(index_path, index_path.lstat())
    digest = hashlib.sha256(data).hexdigest()
    _write_blob(blob_root, digest, data)
    return head.stdout.strip() if head.returncode == 0 else "", digest, str(blob_root / digest), tracked, len(data)


def _read_ignore_text(source_root: Path) -> str:
    path = source_root / ".gitignore"
    if path.is_symlink():
        raise WorkspaceSnapshotError(f"ignore file must not be a symlink: {path}")
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""


def _model_view_directories(view_root: Path) -> tuple[str, ...]:
    """Record materialized directories without following links or ignore rules.

    The frozen Git metadata keeps its existing separate policy. This only
    scans directory names; it does not read or hash repository history.
    """
    directories = ["."]

    def scan_error(error: OSError) -> None:
        raise WorkspaceSnapshotError(f"incomplete model-view scan: {error}") from error

    for current, dirs, _files in os.walk(view_root, followlinks=False, onerror=scan_error):
        current_path = Path(current)
        descend = []
        for name in dirs:
            path = current_path / name
            relative = path.relative_to(view_root).as_posix()
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                continue
            if not stat.S_ISDIR(info.st_mode):
                raise WorkspaceSnapshotError(f"model-view directory changed during capture: {relative}")
            if relative == ".git":
                continue
            directories.append(relative)
            descend.append(name)
        dirs[:] = descend
    return tuple(sorted(directories))


def _freeze_git_history(source_root: Path, view_root: Path, root_artifact: Path) -> tuple[str, str, int]:
    inside = _git_output(source_root, "rev-parse", "--is-inside-work-tree")
    if inside.returncode or inside.stdout.strip() != "true":
        ensure_owner_only_directory(view_root)
        return "", "", 0
    head_result = _git_output(source_root, "rev-parse", "--verify", "HEAD^{commit}")
    if head_result.returncode:
        raise WorkspaceSnapshotError(f"failed to resolve frozen HEAD for {source_root}: {head_result.stderr.strip()}")
    head = head_result.stdout.strip()
    bundle = root_artifact / "history.bundle"
    bundle_result = _git_output(source_root, "bundle", "create", str(bundle), "HEAD")
    if bundle_result.returncode:
        raise WorkspaceSnapshotError(
            f"failed to freeze HEAD ancestors for {source_root}: {bundle_result.stderr.strip()}"
        )
    bundle_size = bundle.stat().st_size
    git = shutil.which("git")
    if git is None:
        raise WorkspaceSnapshotError("git executable is unavailable")
    clone = subprocess.run(  # noqa: S603 - executable and arguments are code-owned
        [git, "clone", "--no-checkout", "--quiet", str(bundle), str(view_root)],
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        timeout=120,
    )
    if clone.returncode:
        raise WorkspaceSnapshotError(f"failed to materialize frozen Git history: {clone.stderr.strip()}")
    remove_remote = _git_output(view_root, "remote", "remove", "origin")
    if remove_remote.returncode:
        raise WorkspaceSnapshotError(f"failed to seal frozen Git view: {remove_remote.stderr.strip()}")
    # --no-checkout leaves the index absent. Seed only its HEAD tree, without
    # materializing filtered files, so read-only Git inspection of the S0 view
    # does not falsely report every committed path as deleted.
    index = _git_output(view_root, "read-tree", head)
    if index.returncode:
        raise WorkspaceSnapshotError(f"failed to materialize frozen Git index: {index.stderr.strip()}")
    return head, str(bundle), bundle_size


class WorkspaceSnapshotter:
    """Capture and restore deterministic workspace checkpoints."""

    def __init__(
        self,
        *,
        max_entries_per_root: int = SNAPSHOT_MAX_ENTRIES_PER_ROOT,
        max_model_file_bytes: int = SNAPSHOT_MAX_FILE_BYTES_FOR_MODEL,
        max_total_bytes: int = SNAPSHOT_MAX_TOTAL_BYTES,
    ) -> None:
        self._max_entries_per_root = max_entries_per_root
        self._max_model_file_bytes = max_model_file_bytes
        self._max_total_bytes = max_total_bytes

    def capture(
        self,
        workspace: Workspace,
        artifact_root: Path,
        *,
        snapshot_id: str,
        include_git_history: bool,
        committed_git_head_only: bool = False,
    ) -> WorkspaceSnapshot:
        """Capture every in-scope workspace entry or fail without a partial result."""
        if committed_git_head_only and not include_git_history:
            raise WorkspaceSnapshotError("committed-head capture requires Git history")
        if artifact_root.exists() or artifact_root.is_symlink():
            raise WorkspaceSnapshotError(f"snapshot artifact already exists: {artifact_root}")
        # Resolve the caller-selected root once. All writes *within* this
        # private tree subsequently reject symlinked parent components.
        artifact_root = artifact_root.expanduser().resolve(strict=False)
        ensure_owner_only_directory(artifact_root)
        blob_root = artifact_root / "blobs"
        roots_root = artifact_root / "roots"
        ensure_owner_only_directory(blob_root)
        ensure_owner_only_directory(roots_root)
        total_bytes = 0
        total_entries = 0
        snapshots: list[SnapshotRoot] = []
        references: list[SnapshotReference] = []
        try:
            roots = self._workspace_roots(workspace)
            for index, working_dir in enumerate(roots):
                source_root = Path(working_dir.path).resolve()
                if not source_root.is_dir():
                    raise WorkspaceSnapshotError(f"workspace root is unavailable: {source_root}")
                root_artifact = roots_root / str(index)
                view_root = root_artifact / "view"
                ensure_owner_only_directory(root_artifact)
                git_head, index_hash, index_blob, tracked_paths, index_bytes = _git_evidence(source_root, blob_root)
                total_bytes += index_bytes
                self._check_total_bytes(total_bytes)
                bundle_path = ""
                if include_git_history:
                    git_head, bundle_path, history_bytes = _freeze_git_history(source_root, view_root, root_artifact)
                    total_bytes += history_bytes
                    self._check_total_bytes(total_bytes)
                else:
                    ensure_owner_only_directory(view_root)
                capture_root = source_root
                view_is_source = False
                if committed_git_head_only:
                    if not git_head:
                        raise WorkspaceSnapshotError(f"committed-head capture requires a Git workspace: {source_root}")
                    checkout = _git_output(view_root, "checkout", "--detach", "--force", git_head)
                    if checkout.returncode:
                        raise WorkspaceSnapshotError(
                            f"failed to materialize committed HEAD for {source_root}: {checkout.stderr.strip()}"
                        )
                    capture_root = view_root
                    view_is_source = True
                    # HEAD-only scope must not accidentally depend on the live index.
                    _head, _digest, _blob, tracked_paths, _size = _git_evidence(view_root, blob_root)
                    total_bytes += _size
                    self._check_total_bytes(total_bytes)
                ignore_text = _read_ignore_text(capture_root)
                entries, root_bytes = self._capture_root(
                    capture_root,
                    view_root,
                    blob_root,
                    excluded_roots=(artifact_root,),
                    view_is_source=view_is_source,
                    tracked_paths=tracked_paths,
                    ignore_text=ignore_text,
                )
                total_bytes += root_bytes
                total_entries += len(entries)
                self._check_total_bytes(total_bytes)
                snapshots.append(
                    SnapshotRoot(
                        source_root=str(source_root),
                        view_root=str(view_root),
                        label=working_dir.label,
                        is_primary=working_dir.is_primary,
                        entries=tuple(entries),
                        git_head=git_head,
                        history_bundle=bundle_path,
                        git_index_sha256=index_hash,
                        git_index_blob=index_blob,
                        tracked_paths=tracked_paths,
                        ignore_text=ignore_text,
                        model_view_directories=_model_view_directories(view_root),
                    )
                )
                view_manifest = {
                    "snapshot_id": snapshot_id,
                    "source_root_hash": hashlib.sha256(os.fsencode(str(source_root))).hexdigest(),
                    "git_head": git_head,
                    "entries": [
                        {
                            "path": entry.relative_path,
                            "kind": entry.kind,
                            "size": entry.size,
                            "model_visible": entry.model_visible,
                            "metadata_reason": entry.metadata_reason,
                        }
                        for entry in entries
                    ],
                }
                atomic_write_owner_only_text(
                    view_root / SNAPSHOT_VIEW_MANIFEST_NAME,
                    json.dumps(view_manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                )
            references, reference_bytes = self._capture_references(
                workspace.reference_files,
                snapshots,
                roots_root,
                blob_root,
            )
            if committed_git_head_only and any(not reference.managed_by_root for reference in references):
                raise WorkspaceSnapshotError("committed-head capture does not support external reference files")
            total_bytes += reference_bytes
            total_entries += sum(not reference.managed_by_root for reference in references)
            self._check_total_bytes(total_bytes)
            payload = {
                "snapshot_id": snapshot_id,
                "total_bytes": total_bytes,
                "entry_count": total_entries,
                "roots": [asdict(root) for root in snapshots],
                "references": [asdict(reference) for reference in references],
            }
            encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
            manifest_hash = hashlib.sha256(encoded).hexdigest()
            atomic_write_owner_only_bytes(artifact_root / SNAPSHOT_MANIFEST_NAME, encoded + b"\n")
            return WorkspaceSnapshot(
                snapshot_id=snapshot_id,
                artifact_root=str(artifact_root),
                roots=tuple(snapshots),
                manifest_hash=manifest_hash,
                total_bytes=total_bytes,
                entry_count=total_entries,
                references=tuple(references),
            )
        except BaseException:
            shutil.rmtree(artifact_root, ignore_errors=True)
            raise

    @staticmethod
    def load(artifact_root: Path, *, expected_manifest_hash: str | None = None) -> WorkspaceSnapshot:
        """Load a manifest; verify identity when its trusted expected hash is supplied."""
        manifest_path = artifact_root / SNAPSHOT_MANIFEST_NAME
        try:
            if manifest_path.is_symlink() or not manifest_path.is_file():
                raise WorkspaceSnapshotError("snapshot manifest must be a regular non-symlink file")
            raw = manifest_path.read_bytes().rstrip(b"\n")
            if expected_manifest_hash is not None and hashlib.sha256(raw).hexdigest() != expected_manifest_hash:
                raise WorkspaceSnapshotError("snapshot manifest digest differs from its frozen identity")
            payload = json.loads(raw)
            roots = tuple(
                SnapshotRoot(
                    source_root=item["source_root"],
                    view_root=item["view_root"],
                    label=item.get("label", ""),
                    is_primary=bool(item.get("is_primary", False)),
                    entries=tuple(SnapshotEntry(**entry) for entry in item.get("entries", [])),
                    git_head=item.get("git_head", ""),
                    history_bundle=item.get("history_bundle", ""),
                    git_index_sha256=item.get("git_index_sha256", ""),
                    git_index_blob=item.get("git_index_blob", ""),
                    tracked_paths=tuple(item.get("tracked_paths", ())),
                    ignore_text=item.get("ignore_text", ""),
                    model_view_directories=(
                        tuple(item["model_view_directories"])
                        if item.get("model_view_directories") is not None else None
                    ),
                )
                for item in payload.get("roots", [])
            )
            references = tuple(
                SnapshotReference(
                    source_path=item["source_path"],
                    view_path=item["view_path"],
                    entry=SnapshotEntry(**item["entry"]),
                    managed_by_root=bool(item.get("managed_by_root", False)),
                )
                for item in payload.get("references", [])
            )
            return WorkspaceSnapshot(
                snapshot_id=payload["snapshot_id"],
                artifact_root=str(artifact_root),
                roots=roots,
                manifest_hash=hashlib.sha256(raw).hexdigest(),
                total_bytes=int(payload["total_bytes"]),
                entry_count=int(payload["entry_count"]),
                references=references,
            )
        except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError) as exc:
            raise WorkspaceSnapshotError(f"invalid snapshot manifest at {manifest_path}: {exc}") from exc

    def restore(self, snapshot: WorkspaceSnapshot) -> None:
        """Restore file contents/types/modes in the frozen scope, not HEAD or index.

        Git refs and staging remain controller-owned; captured HEAD/index fields
        provide evidence for its separate Git checkpoint/restore protocol.
        """
        blob_root = Path(snapshot.artifact_root) / "blobs"
        if blob_root.is_symlink():
            raise WorkspaceSnapshotError("snapshot blob directory must not be a symlink")
        self._validate_recovery_blobs(snapshot)
        for root in snapshot.roots:
            source_root = Path(root.source_root)
            validate_link_free_directory(source_root)
            if source_root.is_symlink() or not source_root.is_dir():
                raise WorkspaceSnapshotError(f"workspace root disappeared before restore: {source_root}")
            expected = {entry.relative_path: entry for entry in root.entries}
            for relative, entry in expected.items():
                _restore_parent_tree(source_root, relative)
                destination = _safe_view_destination(source_root, relative)
                _ensure_restore_parent(destination.parent)
                data = (blob_root / entry.content_hash).read_bytes()
                if entry.kind == "symlink":
                    if destination.exists() or destination.is_symlink():
                        if destination.is_dir() and not destination.is_symlink():
                            shutil.rmtree(destination)
                        else:
                            destination.unlink()
                    os.symlink(
                        os.fsdecode(data),
                        destination,
                        target_is_directory=entry.symlink_target_is_dir,
                    )
                    continue
                if destination.is_symlink() or (destination.exists() and not destination.is_file()):
                    if destination.is_dir() and not destination.is_symlink():
                        shutil.rmtree(destination)
                    else:
                        destination.unlink()
                atomic_write_owner_only_bytes(destination, data)
                if not get_platform().is_windows:
                    destination.chmod(entry.mode)
            # Only now: what counts as in scope is decided by the repository's
            # own ignore rules, and the repair may have changed them. Scanning
            # before the snapshot's `.gitignore` was written back asked the
            # repair which of its files should be rolled back -- a repair that
            # created `generated/output.txt` and added `generated/` to
            # `.gitignore` kept the file, and the later `matches()` then failed
            # as a conflict over the workspace it had just "restored".
            current = {
                relative
                for relative, _path, _info in self._iter_entries(
                    source_root,
                    excluded_roots=(Path(snapshot.artifact_root),),
                    tracked_paths=root.tracked_paths,
                    ignore_text=root.ignore_text,
                )
            }
            emptied: list[Path] = []
            for relative in sorted(current - expected.keys(), key=lambda item: item.count(os.sep), reverse=True):
                path = source_root / relative
                try:
                    if path.is_symlink() or path.is_file():
                        path.unlink()
                        emptied.append(path.parent)
                except OSError as exc:
                    raise WorkspaceSnapshotError(f"failed to remove repair-created path {path}: {exc}") from exc
            self._prune_dirs_emptied_by_restore(source_root, emptied)
        for reference in snapshot.references:
            if reference.managed_by_root:
                continue
            self._restore_reference(reference, blob_root)

    def matches(self, snapshot: WorkspaceSnapshot) -> bool:
        """Compare frozen-scope files, including modes; this does not compare Git state."""
        for root in snapshot.roots:
            source_root = Path(root.source_root)
            if source_root.is_symlink() or not source_root.is_dir():
                return False
            try:
                validate_link_free_directory(source_root)
                live = {
                    relative: (path, info)
                    for relative, path, info in self._iter_entries(
                        source_root,
                        excluded_roots=(Path(snapshot.artifact_root),),
                        tracked_paths=root.tracked_paths,
                        ignore_text=root.ignore_text,
                    )
                }
            except OSError:
                return False
            expected = {entry.relative_path: entry for entry in root.entries}
            if live.keys() != expected.keys():
                return False
            for relative, entry in expected.items():
                path, info = live[relative]
                if stat.S_IMODE(info.st_mode) != entry.mode:
                    return False
                try:
                    data = os.fsencode(os.readlink(path)) if entry.kind == "symlink" else _stable_file_bytes(path, info)
                except (WorkspaceSnapshotError, OSError):
                    return False
                if hashlib.sha256(data).hexdigest() != entry.content_hash:
                    return False
        for reference in snapshot.references:
            if reference.managed_by_root:
                continue
            path = Path(reference.source_path)
            try:
                validate_link_free_directory(path.parent)
                info = path.lstat()
                data = (
                    os.fsencode(os.readlink(path))
                    if reference.entry.kind == "symlink"
                    else _stable_file_bytes(path, info)
                )
            except (WorkspaceSnapshotError, OSError):
                return False
            if stat.S_IMODE(info.st_mode) != reference.entry.mode:
                return False
            if hashlib.sha256(data).hexdigest() != reference.entry.content_hash:
                return False
        return True

    @staticmethod
    def _validate_recovery_blobs(snapshot: WorkspaceSnapshot) -> None:
        """Validate all restoration bytes before changing any live path."""
        WorkspaceSnapshotter.load(Path(snapshot.artifact_root), expected_manifest_hash=snapshot.manifest_hash)
        blob_root = Path(snapshot.artifact_root) / "blobs"
        entries = [entry for root in snapshot.roots for entry in root.entries]
        entries.extend(reference.entry for reference in snapshot.references)
        for entry in entries:
            if len(entry.content_hash) != 64 or any(c not in "0123456789abcdef" for c in entry.content_hash):
                raise WorkspaceSnapshotError("invalid snapshot blob digest")
            if entry.kind not in ("file", "symlink"):
                raise WorkspaceSnapshotError("invalid snapshot entry type")
            path = blob_root / entry.content_hash
            data = _stable_file_bytes(path, path.lstat())
            if hashlib.sha256(data).hexdigest() != entry.content_hash or len(data) != entry.size:
                raise WorkspaceSnapshotError(f"corrupt recovery blob: {entry.content_hash}")

    @staticmethod
    def discard(snapshot: WorkspaceSnapshot) -> None:
        shutil.rmtree(snapshot.artifact_root, ignore_errors=True)

    def _capture_root(
        self,
        source_root: Path,
        view_root: Path,
        blob_root: Path,
        *,
        excluded_roots: tuple[Path, ...] = (),
        view_is_source: bool = False,
        tracked_paths: tuple[str, ...] | None = None,
        ignore_text: str | None = None,
    ) -> tuple[list[SnapshotEntry], int]:
        entries: list[SnapshotEntry] = []
        total_bytes = 0
        for relative, path, info in self._iter_entries(
            source_root, excluded_roots=excluded_roots, tracked_paths=tracked_paths, ignore_text=ignore_text
        ):
            if len(entries) >= self._max_entries_per_root:
                raise WorkspaceSnapshotError(
                    f"workspace root exceeds {self._max_entries_per_root} snapshot entries: {source_root}"
                )
            mode = stat.S_IMODE(info.st_mode)
            if stat.S_ISLNK(info.st_mode):
                try:
                    data = os.fsencode(os.readlink(path))
                except OSError as exc:
                    raise WorkspaceSnapshotError(f"failed to read workspace symlink {path}: {exc}") from exc
                target_is_dir = path.is_dir()
                visible = self._symlink_stays_inside(source_root, path)
                reason = "" if visible else "symlink_target_outside_snapshot"
                digest = hashlib.sha256(data).hexdigest()
                _write_blob(blob_root, digest, data)
                if visible:
                    # Absolute links into the live source must never be copied
                    # verbatim: remap their resolved target into the frozen view.
                    target_relative = path.resolve(strict=False).relative_to(source_root)
                    destination = _safe_view_destination(view_root, relative)
                    frozen_target = view_root / target_relative
                    frozen_link = os.path.relpath(frozen_target, destination.parent)
                    if view_is_source:
                        path.unlink()
                    ensure_owner_only_directory(destination.parent)
                    os.symlink(frozen_link, destination, target_is_directory=target_is_dir)
                elif view_is_source:
                    path.unlink()
                entries.append(
                    SnapshotEntry(
                        relative_path_b64=_encoded_relative_path(relative),
                        kind="symlink",
                        mode=mode,
                        size=len(data),
                        content_hash=digest,
                        model_visible=visible,
                        metadata_reason=reason,
                        symlink_target_is_dir=target_is_dir,
                        model_symlink_target=frozen_link if visible else None,
                    )
                )
                total_bytes += len(data)
                self._check_total_bytes(total_bytes)
                continue
            data = _stable_file_bytes(path, info)
            total_bytes += len(data)
            self._check_total_bytes(total_bytes)
            digest = hashlib.sha256(data).hexdigest()
            _write_blob(blob_root, digest, data)
            too_large = len(data) > self._max_model_file_bytes
            binary = EncodingDetector.looks_binary(data[:SNAPSHOT_SAMPLE_BYTES])
            visible = not too_large and not binary
            reason = "too_large" if too_large else "binary" if binary else ""
            if visible:
                if view_is_source:
                    if not get_platform().is_windows:
                        path.chmod(0o500 if mode & 0o111 else 0o400)
                else:
                    _materialize_model_entry(view_root=view_root, relative_path=relative, data=data, mode=mode)
            elif view_is_source:
                path.unlink()
            entries.append(
                SnapshotEntry(
                    relative_path_b64=_encoded_relative_path(relative),
                    kind="file",
                    mode=mode,
                    size=len(data),
                    content_hash=digest,
                    model_visible=visible,
                    metadata_reason=reason,
                )
            )
        return entries, total_bytes

    def _capture_references(
        self,
        paths: list[str],
        roots: list[SnapshotRoot],
        roots_root: Path,
        blob_root: Path,
    ) -> tuple[list[SnapshotReference], int]:
        references: list[SnapshotReference] = []
        total_bytes = 0
        for index, raw_path in enumerate(paths):
            source = Path(raw_path).expanduser().absolute()
            source = source.parent.resolve(strict=True) / source.name
            if not source.is_file() and not source.is_symlink():
                raise WorkspaceSnapshotError(f"reference file is unavailable: {source}")
            managed = self._managed_reference(source, roots)
            if managed is not None:
                references.append(managed)
                continue
            try:
                info = source.lstat()
            except OSError as exc:
                raise WorkspaceSnapshotError(f"failed to stat reference file {source}: {exc}") from exc
            view_root = roots_root / "references" / str(index)
            ensure_owner_only_directory(view_root)
            view_path = view_root / source.name
            entry, size = self._capture_single_entry(
                source,
                source.name,
                view_root,
                blob_root,
                info,
            )
            total_bytes += size
            self._check_total_bytes(total_bytes)
            references.append(
                SnapshotReference(
                    source_path=str(source),
                    view_path=str(view_path),
                    entry=entry,
                    managed_by_root=False,
                )
            )
        return references, total_bytes

    @staticmethod
    def _managed_reference(source: Path, roots: list[SnapshotRoot]) -> SnapshotReference | None:
        for root in roots:
            source_root = Path(root.source_root)
            try:
                relative = os.path.relpath(source, source_root)
            except ValueError:
                continue
            if relative == os.pardir or relative.startswith(os.pardir + os.sep):
                continue
            entry = next((candidate for candidate in root.entries if candidate.relative_path == relative), None)
            if entry is None:
                continue
            return SnapshotReference(
                source_path=str(source),
                view_path=str(Path(root.view_root) / relative),
                entry=entry,
                managed_by_root=True,
            )
        return None

    def _capture_single_entry(
        self,
        path: Path,
        relative: str,
        view_root: Path,
        blob_root: Path,
        info: os.stat_result,
    ) -> tuple[SnapshotEntry, int]:
        mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISLNK(info.st_mode):
            data = os.fsencode(os.readlink(path))
            # A standalone reference view does not include the symlink's
            # sibling/parent tree, so materializing it could point outside
            # the frozen view even when the live target shares a directory.
            visible = False
            digest = hashlib.sha256(data).hexdigest()
            _write_blob(blob_root, digest, data)
            if visible:
                os.symlink(os.fsdecode(data), view_root / relative, target_is_directory=path.is_dir())
            return (
                SnapshotEntry(
                    relative_path_b64=_encoded_relative_path(relative),
                    kind="symlink",
                    mode=mode,
                    size=len(data),
                    content_hash=digest,
                    model_visible=visible,
                    metadata_reason="" if visible else "symlink_target_outside_snapshot",
                    symlink_target_is_dir=path.is_dir(),
                ),
                len(data),
            )
        if not stat.S_ISREG(info.st_mode):
            raise WorkspaceSnapshotError(f"unsupported reference file type: {path}")
        data = _stable_file_bytes(path, info)
        digest = hashlib.sha256(data).hexdigest()
        _write_blob(blob_root, digest, data)
        too_large = len(data) > self._max_model_file_bytes
        binary = EncodingDetector.looks_binary(data[:SNAPSHOT_SAMPLE_BYTES])
        visible = not too_large and not binary
        if visible:
            _materialize_model_entry(view_root=view_root, relative_path=relative, data=data, mode=mode)
        return (
            SnapshotEntry(
                relative_path_b64=_encoded_relative_path(relative),
                kind="file",
                mode=mode,
                size=len(data),
                content_hash=digest,
                model_visible=visible,
                metadata_reason="too_large" if too_large else "binary" if binary else "",
            ),
            len(data),
        )

    @staticmethod
    def _restore_reference(reference: SnapshotReference, blob_root: Path) -> None:
        destination = Path(reference.source_path)
        entry = reference.entry
        _ensure_restore_parent(destination.parent)
        data = (blob_root / entry.content_hash).read_bytes()
        if destination.exists() or destination.is_symlink():
            if destination.is_dir() and not destination.is_symlink():
                shutil.rmtree(destination)
            else:
                destination.unlink()
        if entry.kind == "symlink":
            os.symlink(os.fsdecode(data), destination, target_is_directory=entry.symlink_target_is_dir)
            return
        atomic_write_owner_only_bytes(destination, data)
        if not get_platform().is_windows:
            destination.chmod(entry.mode)

    def _iter_entries(
        self, source_root: Path, *, excluded_roots: tuple[Path, ...] = (),
        tracked_paths: tuple[str, ...] | None = None, ignore_text: str | None = None,
    ):
        excluded = {
            os.path.normcase(os.path.realpath(path)) for path in excluded_roots if self._is_within(source_root, path)
        }
        ignore = GitignoreFilter.from_text(_read_ignore_text(source_root) if ignore_text is None else ignore_text)
        tracked = set(tracked_paths or ())
        tracked_dirs = {str(parent) for item in tracked for parent in Path(item).parents if str(parent) != "."}

        def scan_error(error: OSError) -> None:
            raise WorkspaceSnapshotError(f"incomplete snapshot scan: {error}") from error

        for current, dirs, files in os.walk(source_root, topdown=True, followlinks=False, onerror=scan_error):
            current_path = Path(current)
            relative_dir = os.path.relpath(current_path, source_root)
            if relative_dir == ".":
                relative_dir = ""
            dirs[:] = [
                name
                for name in dirs
                if os.path.normcase(os.path.realpath(current_path / name)) not in excluded
                and name != ".git"
                and (
                    os.path.join(relative_dir, name) in tracked_dirs
                    or os.path.join(relative_dir, name) in tracked
                    or (
                        not _is_excluded_relative(os.path.join(relative_dir, name))
                        and not ignore.is_ignored(os.path.join(relative_dir, name), is_dir=True)
                    )
                )
            ]
            dirs.sort()
            names = sorted([*files, *(name for name in dirs if (current_path / name).is_symlink())])
            # os.walk must not later recurse into a directory link that the
            # committed-head capture may remove while processing this yield.
            dirs[:] = [name for name in dirs if not (current_path / name).is_symlink()]
            for name in names:
                relative = os.path.join(relative_dir, name) if relative_dir else name
                path = source_root / relative
                if name == ".git" or (
                    relative not in tracked and name != ".gitignore"
                    and (_is_excluded_relative(relative) or ignore.is_ignored(relative, is_dir=False))
                ):
                    continue
                try:
                    info = path.lstat()
                except OSError as exc:
                    raise WorkspaceSnapshotError(f"failed to stat workspace entry {path}: {exc}") from exc
                if stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                    yield relative, path, info
                else:
                    raise WorkspaceSnapshotError(f"unsupported workspace entry type: {path}")

    @staticmethod
    def _workspace_roots(workspace: Workspace) -> list[WorkingDir]:
        roots = [WorkingDir(path=workspace.primary_cwd, is_primary=True)]
        roots.extend(workspace.working_dirs)
        unique: list[WorkingDir] = []
        seen: set[str] = set()
        for root in roots:
            normalized = os.path.normcase(os.path.realpath(os.path.abspath(root.path)))
            if normalized in seen:
                if root.is_primary:
                    for index, existing in enumerate(unique):
                        if os.path.normcase(os.path.realpath(os.path.abspath(existing.path))) == normalized:
                            unique[index] = WorkingDir(path=existing.path, label=existing.label, is_primary=True)
                continue
            seen.add(normalized)
            unique.append(WorkingDir(path=root.path, label=root.label, is_primary=root.is_primary))
        if unique and not any(root.is_primary for root in unique):
            unique[0].is_primary = True
        return unique

    @staticmethod
    def _symlink_stays_inside(source_root: Path, path: Path) -> bool:
        try:
            resolved = path.resolve(strict=False)
            common = os.path.commonpath((str(source_root), str(resolved)))
        except (OSError, ValueError, RuntimeError):
            return False
        return os.path.normcase(common) == os.path.normcase(str(source_root))

    @staticmethod
    def _is_within(root: Path, path: Path) -> bool:
        try:
            common = os.path.commonpath((os.path.realpath(root), os.path.realpath(path)))
        except (OSError, ValueError):
            return False
        return os.path.normcase(common) == os.path.normcase(os.path.realpath(root))

    def _check_total_bytes(self, total_bytes: int) -> None:
        if total_bytes > self._max_total_bytes:
            raise WorkspaceSnapshotError(f"workspace snapshot exceeds {self._max_total_bytes} bytes")

    def _prune_dirs_emptied_by_restore(self, source_root: Path, emptied: list[Path]) -> None:
        """Remove the directories this restore emptied, and only those.

        Walking the tree for empty directories cannot tell a repair-created one
        from a pre-existing one -- ``_iter_entries`` captures files and
        symlinks, so an empty directory is never in the snapshot to compare
        against -- and the walk also descended into excluded trees, rmdir-ing
        leaves like ``.git/refs/tags``. The parents of the files just deleted
        are the only directories this restore can be responsible for.
        """
        candidates = sorted(set(emptied), key=lambda item: len(item.parts), reverse=True)
        for candidate in candidates:
            path = candidate
            while path != source_root and self._is_within(source_root, path):
                try:
                    if any(path.iterdir()):
                        break
                    path.rmdir()
                except OSError:
                    break
                path = path.parent


def capture_workspace(workspace: Path, output_dir: Path) -> WorkspaceSnapshot:
    """Freeze S0's in-scope files and reachable Git history without moving refs.

    ``output_dir`` must not exist. Capture is local filesystem/Git work only;
    it neither starts an agent nor invokes a model.
    """
    workspace = workspace.expanduser().resolve(strict=True)
    output_dir = output_dir.expanduser().absolute()
    return WorkspaceSnapshotter().capture(
        Workspace.from_cwd(str(workspace)),
        output_dir,
        snapshot_id=output_dir.name,
        include_git_history=True,
    )
