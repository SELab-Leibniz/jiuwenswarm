"""Explicit, authority-bound recovery of a completed native Jiuwen P0.

Adapted from the stage-recovery policy in Chrys
0561cf0fa584fd56fdcd98c90cca6a5bc1890d95.  Source reconstruction reuses this
suite's snapshotter; model state is preserved as evidence, never resumed by ID.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

from .clarification.compat import (
    atomic_write_owner_only_bytes,
    atomic_write_owner_only_text,
    ensure_owner_only_directory,
    validate_link_free_directory,
)
from .clarification.snapshot import WorkspaceSnapshot, WorkspaceSnapshotter

SCHEMA = "jiuwenswarm/p0-recovery/v1"
RESTORATION_SCHEMA = "jiuwenswarm/p0-restoration/v1"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SHA1 = re.compile(r"[0-9a-f]{40}\Z")
_NATIVE_REQUIRED = ("request.json", "metadata.json", "events.jsonl", "terminal-result.json")


def _digest(path: Path) -> str:
    validate_link_free_directory(path.parent)
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"recovery asset must be a regular file: {path.name}")
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _safe(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative:
        raise ValueError("empty recovery asset path")
    item = Path(relative)
    if item.is_absolute() or ".." in item.parts:
        raise ValueError("recovery asset escapes package")
    path = root / item
    cursor = root
    for part in item.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError("recovery asset path contains a symlink")
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("recovery asset escapes package")
    return path


def _source_asset(workflow: Path, value: str) -> Path:
    """Rebind only the documented container root when exporting on the host."""
    path = Path(value)
    if path.is_absolute():
        for prefix in (workflow, Path("/agent_out/long-horizon-suite")):
            if path.is_relative_to(prefix):
                return _safe(workflow, str(path.relative_to(prefix)))
        raise ValueError("source native-call artifact is outside its workflow")
    return _safe(workflow, value)


def _copy(source: Path, destination: Path, root: Path, files: dict[str, str]) -> None:
    _digest(source)
    destination = _safe(root, str(destination.relative_to(root)))
    ensure_owner_only_directory(destination.parent)
    atomic_write_owner_only_bytes(destination, source.read_bytes())
    files[str(destination.relative_to(root))] = _digest(destination)


def _check_snapshot(snapshot: WorkspaceSnapshot, directory: Path, files: dict[str, str], prefix: str) -> None:
    if len(snapshot.roots) != 1 or snapshot.references:
        raise ValueError("P0 recovery supports one source repository and no external references")
    if not _SHA1.fullmatch(snapshot.roots[0].git_head):
        raise ValueError("snapshot has no complete Git source identity")
    for entry in snapshot.roots[0].entries:
        path = Path(entry.relative_path)
        if path.is_absolute() or not path.parts or ".." in path.parts or ".git" in path.parts:
            raise ValueError("unsafe recovery source path")
        if not _SHA256.fullmatch(entry.content_hash):
            raise ValueError("invalid source blob identity")
        relative = f"{prefix}/blobs/{entry.content_hash}"
        blob = _safe(directory, relative)
        if files.get(relative) != entry.content_hash or _digest(blob) != entry.content_hash or blob.stat().st_size != entry.size:
            raise ValueError("recovery source blob is absent or changed")
    index_hash = snapshot.roots[0].git_index_sha256
    if index_hash:
        relative = f"{prefix}/blobs/{index_hash}"
        if not _SHA256.fullmatch(index_hash) or files.get(relative) != index_hash:
            raise ValueError("recovery Git index evidence is absent")


def _copy_snapshot(workflow: Path, name: str, destination: Path, files: dict[str, str]) -> WorkspaceSnapshot:
    source = _safe(workflow, f"snapshots/{name}")
    snapshot = WorkspaceSnapshotter.load(source)
    if len(snapshot.roots) != 1 or snapshot.references:
        raise ValueError("P0 recovery requires one repository and no external references")
    for entry in snapshot.roots[0].entries:
        relative = Path(entry.relative_path)
        if (not _SHA256.fullmatch(entry.content_hash) or relative.is_absolute()
                or not relative.parts or ".." in relative.parts or ".git" in relative.parts):
            raise ValueError("unsafe source snapshot entry before export")
    if snapshot.roots[0].git_index_sha256 and not _SHA256.fullmatch(snapshot.roots[0].git_index_sha256):
        raise ValueError("unsafe source Git index identity before export")
    paths = [source / "manifest.json"]
    paths.extend(source / "blobs" / entry.content_hash for entry in snapshot.roots[0].entries)
    if snapshot.roots[0].git_index_sha256:
        paths.append(source / "blobs" / snapshot.roots[0].git_index_sha256)
    # Canonical location is stable even when the original manifest's absolute
    # model-view paths describe a container that no longer exists.
    paths.append(source / "roots" / "0" / "history.bundle")
    for path in dict.fromkeys(paths):
        relative = path.relative_to(source)
        _copy(_safe(source, str(relative)), destination / name / relative, destination, files)
    return snapshot


def _verify_native_call(directory: Path, call: dict[str, Any], files: dict[str, str]) -> None:
    relative = call.get("output_dir")
    root = _safe(directory, relative)
    if any(f"{relative}/{name}" not in files for name in _NATIVE_REQUIRED):
        raise ValueError("P0 recovery omits required native invocation receipts")
    request = json.loads(_safe(root, "request.json").read_bytes())
    metadata = json.loads(_safe(root, "metadata.json").read_bytes())
    terminal = json.loads(_safe(root, "terminal-result.json").read_bytes())
    if not all(isinstance(value, dict) for value in (request, metadata, terminal)):
        raise ValueError("P0 recovery native receipts must be objects")
    request_id = request.get("request_id")
    session_id = call.get("session_id")
    if (not isinstance(request_id, str) or not request_id
            or not isinstance(session_id, str) or not session_id
            or metadata.get("request_id") != request_id or terminal.get("request_id") != request_id
            or metadata.get("session_id") != session_id or terminal.get("session_id") != session_id
            or metadata.get("terminal_result_received") is not True
            or metadata.get("status") != "completed" or metadata.get("actual_exit_code") != 0
            or terminal.get("status") != "completed" or terminal.get("exit_code") != 0
            or call.get("status") != "completed" or call.get("exit_code") != 0):
        raise ValueError("source P0 native completion evidence is inconsistent")


def export_p0_recovery(workflow: Path, destination: Path) -> Path:
    """Export an old or new suite's completed P0, preserving its original evidence."""
    workflow = workflow.resolve(strict=True)
    destination = destination.absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError("recovery destination already exists")
    summary_path = _safe(workflow, "summary.json")
    summary = json.loads(summary_path.read_text())
    if summary.get("schema") != "jiuwenswarm-long-horizon/v1":
        raise ValueError("unsupported source workflow")
    if summary.get("stages", {}).get("p0", {}).get("status") != "completed":
        raise ValueError("export requires a directly completed P0; chained recovery export is unsupported")
    if summary.get("stages", {}).get("p0_snapshot", {}).get("status") != "completed":
        raise ValueError("source workflow has no completed P0 snapshot")
    ensure_owner_only_directory(destination)
    files: dict[str, str] = {}
    s0 = _copy_snapshot(workflow, "s0", destination, files)
    p0 = _copy_snapshot(workflow, "p0", destination, files)
    original = _safe(workflow, "requirement.md").read_bytes()
    requirement_sha256 = hashlib.sha256(original).hexdigest()
    identities = summary.get("identities", {})
    if identities.get("requirement_sha256") != requirement_sha256 or identities.get("workspace_base_commit") != s0.roots[0].git_head:
        raise ValueError("source requirement or baseline identity has changed")
    for name in ("s0", "p0"):
        if summary.get("snapshots", {}).get(name, {}).get("manifest_hash") != (s0 if name == "s0" else p0).manifest_hash:
            raise ValueError("source snapshot differs from recorded workflow identity")
    _copy(workflow / "requirement.md", destination / "requirement.md", destination, files)
    _copy(summary_path, destination / "origin" / "summary.json", destination, files)
    native_calls = []
    for index, call in enumerate(summary.get("worker_calls", [])):
        if call.get("stage") != "p0":
            continue
        call_root = _source_asset(workflow, call["output_dir"])
        target = destination / "origin" / "p0-native-calls" / str(index)
        for name in ("request.json", "metadata.json", "events.jsonl", "terminal-result.json", "usage.json", "final-response.json"):
            path = call_root / name
            if name in {"usage.json", "final-response.json"} and not path.exists():
                continue
            _copy(path, target / name, destination, files)
        copied_call = {**call, "output_dir": str(target.relative_to(destination))}
        _verify_native_call(destination, copied_call, files)
        native_calls.append(copied_call)
    if not native_calls:
        raise ValueError("source P0 has no native invocation evidence")
    for name in ("recovery-origin.json", "recovery-restoration.json"):
        source = workflow / name
        if source.is_file():
            _copy(source, destination / "origin" / name, destination, files)
    payload = {
        "schema": SCHEMA, "source_workflow": str(workflow),
        "source_summary_sha256": _digest(summary_path), "source_identities": identities,
        "source_config_fingerprints": summary.get("config_fingerprints", {}),
        "source_snapshot": p0.snapshot_id, "source_manifest_sha256": p0.manifest_hash,
        "s0_manifest_sha256": s0.manifest_hash, "base_commit": s0.roots[0].git_head,
        "requirement_messages": [original.decode("utf-8")], "requirement_sha256": requirement_sha256,
        "files": files, "native_calls": native_calls, "resume_stage": "clarification",
        "evaluation_kind": "recovered_attempt_not_fresh_pass_at_1",
        "native_session_resume": False,
    }
    manifest = destination / "recovery.json"
    atomic_write_owner_only_text(manifest, json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n")
    digest = _digest(manifest)
    atomic_write_owner_only_text(destination / "recovery.sha256", digest + "\n")
    load_p0_recovery(destination, requirement_bytes=original, expected_sha256=digest, require_publication=False)
    # Publication is last; partial exports cannot be selected by the loader.
    atomic_write_owner_only_text(destination / "completed.json", json.dumps({"schema": "jiuwenswarm/p0-recovery-publication/v1", "recovery_sha256": digest}) + "\n")
    return manifest


def load_p0_recovery(directory: Path, *, requirement_bytes: bytes, expected_sha256: str,
                     require_publication: bool = True) -> tuple[dict[str, Any], WorkspaceSnapshot, WorkspaceSnapshot]:
    """Validate every listed byte before touching a destination or admitting models."""
    directory = directory.resolve(strict=True)
    if not _SHA256.fullmatch(expected_sha256):
        raise ValueError("an explicit full recovery package SHA-256 is required")
    manifest = _safe(directory, "recovery.json")
    if _digest(manifest) != expected_sha256 or _safe(directory, "recovery.sha256").read_text().strip() != expected_sha256:
        raise ValueError("P0 recovery manifest hash mismatch")
    if require_publication:
        publication = json.loads(_safe(directory, "completed.json").read_text())
        if publication.get("schema") != "jiuwenswarm/p0-recovery-publication/v1" or publication.get("recovery_sha256") != expected_sha256:
            raise ValueError("P0 recovery package was not completely published")
    payload = json.loads(manifest.read_text())
    if (payload.get("schema") != SCHEMA or payload.get("resume_stage") != "clarification"
            or payload.get("evaluation_kind") != "recovered_attempt_not_fresh_pass_at_1"
            or payload.get("native_session_resume") is not False):
        raise ValueError("unsupported P0 recovery identity or entry stage")
    if payload.get("requirement_messages") != [requirement_bytes.decode("utf-8")] or payload.get("requirement_sha256") != hashlib.sha256(requirement_bytes).hexdigest():
        raise ValueError("P0 recovery belongs to another requirement")
    files = payload.get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError("P0 recovery has no file manifest")
    for relative, digest in files.items():
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest) or _digest(_safe(directory, relative)) != digest:
            raise ValueError("P0 recovery file hash mismatch")
    required = {"requirement.md", "s0/manifest.json", "p0/manifest.json", "s0/roots/0/history.bundle", "p0/roots/0/history.bundle", "origin/summary.json"}
    if not required.issubset(files) or _safe(directory, "requirement.md").read_bytes() != requirement_bytes:
        raise ValueError("P0 recovery omits required authority or history artifacts")
    s0 = WorkspaceSnapshotter.load(directory / "s0", expected_manifest_hash=payload["s0_manifest_sha256"])
    p0 = WorkspaceSnapshotter.load(directory / "p0", expected_manifest_hash=payload["source_manifest_sha256"])
    _check_snapshot(s0, directory, files, "s0")
    _check_snapshot(p0, directory, files, "p0")
    if not _SHA1.fullmatch(payload.get("base_commit", "")) or payload["base_commit"] != s0.roots[0].git_head:
        raise ValueError("P0 recovery baseline identity mismatch")
    origin = json.loads(_safe(directory, "origin/summary.json").read_text())
    if (_digest(directory / "origin/summary.json") != payload.get("source_summary_sha256")
            or origin.get("schema") != "jiuwenswarm-long-horizon/v1"
            or origin.get("stages", {}).get("p0", {}).get("status") != "completed"
            or origin.get("stages", {}).get("p0_snapshot", {}).get("status") != "completed"
            or origin.get("identities") != payload.get("source_identities")
            or origin.get("identities", {}).get("requirement_sha256") != payload["requirement_sha256"]
            or origin.get("identities", {}).get("workspace_base_commit") != payload["base_commit"]
            or origin.get("config_fingerprints", {}) != payload.get("source_config_fingerprints")
            or origin.get("snapshots", {}).get("s0", {}).get("manifest_hash") != s0.manifest_hash
            or payload.get("source_snapshot") != p0.snapshot_id
            or origin.get("snapshots", {}).get("p0", {}).get("manifest_hash") != p0.manifest_hash):
        raise ValueError("P0 recovery origin evidence changed")
    expected_calls = [{**call, "output_dir": f"origin/p0-native-calls/{index}"}
                      for index, call in enumerate(origin.get("worker_calls", [])) if call.get("stage") == "p0"]
    if not expected_calls or payload.get("native_calls") != expected_calls:
        raise ValueError("P0 recovery native invocation identities differ from their origin")
    for call in expected_calls:
        _verify_native_call(directory, call, files)
    return payload, s0, p0


def validate_recovery_target(workspace: Path, payload: dict[str, Any], s0: WorkspaceSnapshot) -> None:
    target = workspace.resolve(strict=True)
    for arguments, expected in ((["rev-parse", "--show-toplevel"], str(target)),
                                (["rev-parse", "HEAD"], payload["base_commit"]),
                                (["status", "--porcelain=v1", "--untracked-files=all"], "")):
        result = subprocess.run(["git", "--no-optional-locks", "-C", str(target), *arguments], stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, check=True, timeout=30)
        if result.stdout.strip() != expected:
            raise ValueError("P0 recovery requires a clean repository at the original base commit")
    mapped = replace(s0, roots=(replace(s0.roots[0], source_root=str(target)),))
    if not WorkspaceSnapshotter().matches(mapped):
        raise ValueError("clean destination source does not match the original S0 working set")


def restore_p0_recovery(directory: Path, *, workspace: Path, requirement_bytes: bytes,
                        expected_sha256: str) -> tuple[dict[str, Any], dict[str, Any]]:
    payload, s0, p0 = load_p0_recovery(directory, requirement_bytes=requirement_bytes,
                                    expected_sha256=expected_sha256, require_publication=True)
    validate_recovery_target(workspace, payload, s0)
    mapped = replace(p0, roots=(replace(p0.roots[0], source_root=str(workspace.resolve())),))
    snapshotter = WorkspaceSnapshotter()
    snapshotter.restore(mapped)
    if not snapshotter.matches(mapped):
        raise ValueError("restored P0 source does not match its immutable snapshot")
    receipt = {"schema": RESTORATION_SCHEMA, "status": "restored", "package_sha256": expected_sha256,
               "workspace_base_commit": payload["base_commit"], "source_manifest_sha256": p0.manifest_hash,
               "requirement_sha256": payload["requirement_sha256"],
               "resume_stage": "clarification", "evaluation_kind": payload["evaluation_kind"],
               "native_session_resume": False, "git_history_restored": False,
               "source_p0_head": p0.roots[0].git_head}
    return payload, receipt


def restore_snapshot_in_place(snapshot: WorkspaceSnapshot, workspace: Path, symbolic_head: str) -> dict[str, Any]:
    """Restore a locally captured P0's source, HEAD and index after failed repair.

    The caller must first preserve the failed candidate. Other Git refs are
    retained as recovery evidence; this never rewrites a remote repository.
    """
    if len(snapshot.roots) != 1 or snapshot.references:
        raise ValueError("repair fallback requires one local source snapshot")
    target = workspace.resolve(strict=True)
    root = snapshot.roots[0]
    if Path(root.source_root).resolve() != target or not _SHA1.fullmatch(root.git_head):
        raise ValueError("repair fallback snapshot belongs to another workspace")
    if symbolic_head and not symbolic_head.startswith("refs/heads/"):
        raise ValueError("invalid captured symbolic HEAD")

    def git(*args: str) -> str:
        return subprocess.run(["git", "-C", str(target), *args], stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, check=True, timeout=30).stdout.strip()

    git("cat-file", "-e", root.git_head + "^{commit}")
    if symbolic_head:
        git("check-ref-format", symbolic_head)
    for entry in root.entries:
        relative = Path(entry.relative_path)
        if relative.is_absolute() or not relative.parts or ".." in relative.parts or ".git" in relative.parts:
            raise ValueError("unsafe repair fallback source path")
    snapshotter = WorkspaceSnapshotter()
    snapshotter._validate_recovery_blobs(snapshot)
    index_path = Path(git("rev-parse", "--path-format=absolute", "--git-path", "index"))
    validate_link_free_directory(index_path.parent)
    if index_path.is_symlink():
        raise ValueError("index is a symlink")
    index_data = None
    if root.git_index_sha256:
        if not _SHA256.fullmatch(root.git_index_sha256):
            raise ValueError("invalid captured P0 index blob identity")
        blob = Path(snapshot.artifact_root) / "blobs" / root.git_index_sha256
        if _digest(blob) != root.git_index_sha256:
            raise ValueError("captured P0 index blob changed")
        index_data = blob.read_bytes()
    snapshotter.restore(snapshot)
    if symbolic_head:
        git("update-ref", symbolic_head, root.git_head)
        git("symbolic-ref", "HEAD", symbolic_head)
    else:
        git("update-ref", "--no-deref", "HEAD", root.git_head)
    if root.git_index_sha256:
        atomic_write_owner_only_bytes(index_path, index_data)
        if _digest(index_path) != root.git_index_sha256:
            raise ValueError("P0 index restoration failed")
    elif index_path.exists():
        if index_path.is_symlink():
            raise ValueError("index is a symlink")
        index_path.unlink()
    if git("rev-parse", "HEAD") != root.git_head or not snapshotter.matches(snapshot):
        raise ValueError("P0 restoration failed source or HEAD verification")
    return {"schema": "jiuwenswarm/repair-fallback/v1", "status": "restored",
            "head": root.git_head, "symbolic_head": symbolic_head,
            "index_sha256": root.git_index_sha256, "source_manifest_sha256": snapshot.manifest_hash,
            "source_restored": True, "head_restored": True, "index_restored": True}
