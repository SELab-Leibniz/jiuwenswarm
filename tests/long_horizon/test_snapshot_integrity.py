"""Frozen model views bind link targets and inventory without changing recovery."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from jiuwenswarm.long_horizon.clarification.budget import SideCallIntegrityError
from jiuwenswarm.long_horizon.clarification.event_evidence import validate_model_view
from jiuwenswarm.long_horizon.clarification.snapshot import (
    SNAPSHOT_VIEW_MANIFEST_NAME,
    WorkspaceSnapshotter,
    capture_workspace,
)


@pytest.fixture
def frozen_links(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    for key, value in (("user.name", "Snapshot Test"), ("user.email", "test@localhost")):
        subprocess.run(["git", "-C", str(repo), "config", key, value], check=True)
    (repo / ".gitignore").write_text("ignored/\n*.hidden\n")
    (repo / "a.py").write_text("original a\n")
    (repo / "b.py").write_text("original b\n")
    (repo / "src").mkdir()
    (repo / "src" / "module.py").write_text("original module\n")
    (repo / "other").mkdir()
    (repo / "other" / "module.py").write_text("other module\n")
    (repo / "alias.py").symlink_to("a.py")
    (repo / "absolute.py").symlink_to(repo / "a.py")
    (repo / "directory").symlink_to("src", target_is_directory=True)
    (repo / "chain.py").symlink_to("directory/module.py")
    (repo / "external").symlink_to(tmp_path, target_is_directory=True)
    (repo / "binary").write_bytes(b"\x00binary")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
    snapshot = capture_workspace(repo, tmp_path / "snapshot")
    return repo, snapshot, Path(snapshot.roots[0].view_root)


def test_frozen_internal_links_bind_materialized_targets_and_reload(frozen_links):
    repo, snapshot, view = frozen_links
    validate_model_view(snapshot)
    assert os.readlink(view / "absolute.py") == "a.py"
    assert os.readlink(view / "chain.py") == "src/module.py"
    assert not (view / "external").exists()
    assert not (view / "binary").exists()
    entries = {entry.relative_path: entry for entry in snapshot.roots[0].entries}
    original_target = os.fsencode(os.readlink(repo / "absolute.py"))
    assert entries["absolute.py"].content_hash == hashlib.sha256(original_target).hexdigest()
    assert entries["absolute.py"].model_symlink_target == "a.py"
    assert entries["chain.py"].model_symlink_target == "src/module.py"
    # Validation must use captured identities, never the now-mutable live tree.
    (repo / "absolute.py").unlink()
    (repo / "absolute.py").symlink_to(repo / "b.py")
    (repo / "a.py").write_text("live changed\n")
    validate_model_view(snapshot)
    loaded = WorkspaceSnapshotter.load(Path(snapshot.artifact_root), expected_manifest_hash=snapshot.manifest_hash)
    assert loaded == snapshot
    validate_model_view(loaded)


@pytest.mark.parametrize("name,target", [
    ("alias.py", "b.py"),
    ("absolute.py", "b.py"),
    ("chain.py", "other/module.py"),
    ("directory", "other"),
    ("alias.py", "./a.py"),
    ("alias.py", "alias.py"),
])
def test_existing_symlink_retarget_is_rejected(frozen_links, name, target):
    _, snapshot, view = frozen_links
    (view / name).unlink()
    (view / name).symlink_to(target)
    with pytest.raises(SideCallIntegrityError, match="captured evidence"):
        validate_model_view(snapshot)


@pytest.mark.parametrize("name", ["leak", "ignored", ".venv", "node_modules", "nested/.git"])
def test_new_directory_symlinks_are_not_hidden_by_walk_or_exclusions(frozen_links, name):
    repo, snapshot, view = frozen_links
    target = view / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(repo, target_is_directory=True)
    with pytest.raises(SideCallIntegrityError, match="unexpected source"):
        validate_model_view(snapshot)


@pytest.mark.parametrize("name", ["empty", "ignored", ".venv", "node_modules", "src/.git"])
def test_new_empty_directories_are_rejected_even_when_source_rules_ignore_them(frozen_links, name):
    _, snapshot, view = frozen_links
    (view / name).mkdir()
    with pytest.raises(SideCallIntegrityError, match="directory inventory"):
        validate_model_view(snapshot)


@pytest.mark.parametrize("name", ["new.hidden", "ignored/new.py", ".venv/new.py", "binary", "external"])
def test_new_files_and_hidden_metadata_entries_cannot_enter_the_model_view(frozen_links, name):
    _, snapshot, view = frozen_links
    path = view / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("unexpected model-visible content\n")
    with pytest.raises(SideCallIntegrityError, match="unexpected source"):
        validate_model_view(snapshot)


def test_marker_cannot_be_replaced_by_an_external_link(frozen_links):
    repo, snapshot, view = frozen_links
    marker = view / SNAPSHOT_VIEW_MANIFEST_NAME
    marker.unlink()
    marker.symlink_to(repo / "a.py")
    with pytest.raises(SideCallIntegrityError, match="unexpected source"):
        validate_model_view(snapshot)


def test_directory_replacement_cannot_redirect_frozen_entries(frozen_links):
    repo, snapshot, view = frozen_links
    (view / "src").rename(view / "old-src")
    (view / "src").symlink_to(repo / "src", target_is_directory=True)
    with pytest.raises(SideCallIntegrityError):
        validate_model_view(snapshot)


def test_directory_scan_errors_fail_closed(frozen_links, monkeypatch):
    _, snapshot, _ = frozen_links

    def failed_walk(*args, **kwargs):
        kwargs["onerror"](PermissionError("unreadable frozen directory"))
        return iter(())

    monkeypatch.setattr("jiuwenswarm.long_horizon.clarification.event_evidence.os.walk", failed_walk)
    with pytest.raises(SideCallIntegrityError, match="incomplete frozen snapshot scan"):
        validate_model_view(snapshot)


def test_legacy_snapshot_load_and_live_restore_remain_compatible(frozen_links):
    repo, snapshot, _ = frozen_links
    manifest = Path(snapshot.artifact_root) / "manifest.json"
    payload = json.loads(manifest.read_text())
    for root in payload["roots"]:
        root.pop("model_view_directories")
        for entry in root["entries"]:
            entry.pop("model_symlink_target")
    manifest.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    legacy = WorkspaceSnapshotter.load(Path(snapshot.artifact_root))
    with pytest.raises(SideCallIntegrityError, match="directory inventory"):
        validate_model_view(legacy)
    original_link = os.readlink(repo / "absolute.py")
    (repo / "absolute.py").unlink()
    (repo / "absolute.py").symlink_to("b.py")
    (repo / "a.py").write_text("live edit\n")
    (repo / "created.py").write_text("repair-created\n")
    snapshotter = WorkspaceSnapshotter()
    snapshotter.restore(legacy)
    assert snapshotter.matches(legacy)
    assert (repo / "a.py").read_text() == "original a\n"
    assert os.readlink(repo / "absolute.py") == original_link
    assert not (repo / "created.py").exists()


def test_missing_legacy_symlink_identity_fails_closed(frozen_links):
    _, snapshot, _ = frozen_links
    manifest = Path(snapshot.artifact_root) / "manifest.json"
    payload = json.loads(manifest.read_text())
    for root in payload["roots"]:
        for entry in root["entries"]:
            entry.pop("model_symlink_target")
    manifest.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    legacy = WorkspaceSnapshotter.load(Path(snapshot.artifact_root))
    with pytest.raises(SideCallIntegrityError, match="captured evidence"):
        validate_model_view(legacy)
