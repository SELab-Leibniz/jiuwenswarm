"""Model-free recovery tests using real Git histories and native receipt files."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from jiuwenswarm.long_horizon import recovery
from jiuwenswarm.long_horizon.clarification.compat import Workspace
from jiuwenswarm.long_horizon.clarification.snapshot import WorkspaceSnapshotter


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def json_write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=True, sort_keys=True) + "\n")


@pytest.fixture
def workflow(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-q", "-b", "codex/fixture")
    git(source, "config", "user.name", "Recovery Test")
    git(source, "config", "user.email", "test@localhost")
    git(source, "config", "maintenance.auto", "false")
    (source / "source.py").write_text("BASE = 1\n")
    (source / "removed.txt").write_text("remove in P0\n")
    (source / "script.sh").write_text("#!/bin/sh\nprintf 'base\\n'\n")
    (source / "script.sh").chmod(0o755)
    git(source, "add", ".")
    git(source, "commit", "-qm", "base")
    base = git(source, "rev-parse", "HEAD")
    root = tmp_path / "workflow"
    root.mkdir()
    requirement = "保留原始需求字节。\r\nReturn the public value.\r\n".encode()
    (root / "requirement.md").write_bytes(requirement)
    snapshotter = WorkspaceSnapshotter()
    s0 = snapshotter.capture(Workspace.from_cwd(str(source)), root / "snapshots/s0",
                             snapshot_id="s0", include_git_history=True)
    (source / "source.py").write_text("P0_COMMITTED = 2\n")
    (source / "removed.txt").unlink()
    (source / "committed.txt").write_text("committed P0 addition\n")
    git(source, "add", "-A")
    git(source, "commit", "-qm", "P0 real agent commit")
    p0_head = git(source, "rev-parse", "HEAD")
    (source / "source.py").write_text("P0_WORKING = 3\n")
    (source / "staged.txt").write_text("P0 staged addition\n")
    git(source, "add", "staged.txt")
    (source / "untracked.bin").write_bytes(b"\x00P0 binary\xff\n")
    p0 = snapshotter.capture(Workspace.from_cwd(str(source)), root / "snapshots/p0",
                             snapshot_id="p0", include_git_history=True)
    native = root / "02-p0/native"
    native.mkdir(parents=True)
    for name, payload in {
        "request.json": {"schema_version": "0.1", "type": "run", "request_id": "request-p0", "prompt": requirement.decode()},
        "metadata.json": {"status": "completed", "actual_exit_code": 0, "terminal_result_received": True,
                          "request_id": "request-p0", "session_id": "session-p0"},
        "terminal-result.json": {"schema_version": "0.1", "type": "result", "status": "completed", "exit_code": 0,
                                 "request_id": "request-p0", "session_id": "session-p0", "output": "native final"},
        "usage.json": {"input_tokens": 123, "output_tokens": 45},
    }.items():
        json_write(native / name, payload)
    (native / "events.jsonl").write_bytes(
        b'{"type":"event","event_type":"chat.tool_call","payload":{"name":"read_file"}}\n'
    )
    summary = {
        "schema": "jiuwenswarm-long-horizon/v1", "status": "completed",
        "identities": {"requirement_sha256": hashlib.sha256(requirement).hexdigest(), "workspace_base_commit": base,
                       "agent_base_commit": "49c9f5b4c66d193e4168bfa9c82c2df025c773f0"},
        "stages": {"p0": {"status": "completed"}, "p0_snapshot": {"status": "completed"}},
        "snapshots": {"s0": {"manifest_hash": s0.manifest_hash}, "p0": {"manifest_hash": p0.manifest_hash}},
        "config_fingerprints": {"config_template_sha256": "f" * 64, "tool_policy": "minimal"},
        "worker_calls": [{"stage": "p0", "status": "completed", "exit_code": 0,
                          "session_id": "session-p0", "output_dir": str(native)}],
    }
    json_write(root / "summary.json", summary)
    return SimpleNamespace(root=root, source=source, base=base, p0_head=p0_head, s0=s0, p0=p0,
                           requirement=requirement, summary=summary, native=native, tmp=tmp_path)


def export(workflow):
    directory = workflow.tmp / "package"
    manifest = recovery.export_p0_recovery(workflow.root, directory)
    return directory, digest(manifest)


def target(workflow):
    path = workflow.tmp / "target"
    subprocess.run(["git", "clone", "--quiet", "--no-local", str(workflow.source), str(path)], check=True)
    git(path, "checkout", "--detach", "-q", workflow.base)
    git(path, "status", "--porcelain=v1", "--untracked-files=all")
    return path


def repo_state(path):
    """No Git status call: inspecting failures must not refresh the real index."""
    return {
        "head": git(path, "rev-parse", "HEAD"),
        "index": (path / ".git/index").read_bytes(),
        "source": {item.relative_to(path).as_posix(): item.read_bytes()
                   for item in path.rglob("*") if item.is_file() and ".git" not in item.relative_to(path).parts},
    }


def rewrite_package(directory, transform):
    payload = json.loads((directory / "recovery.json").read_bytes())
    transform(payload)
    json_write(directory / "recovery.json", payload)
    identity = digest(directory / "recovery.json")
    (directory / "recovery.sha256").write_text(identity + "\n")
    json_write(directory / "completed.json", {
        "schema": "jiuwenswarm/p0-recovery-publication/v1", "recovery_sha256": identity,
    })
    return identity


def load(directory, workflow, identity):
    return recovery.load_p0_recovery(directory, requirement_bytes=workflow.requirement, expected_sha256=identity)


def test_export_preserves_original_bytes_source_identity_bundles_and_native_calls(workflow):
    directory, identity = export(workflow)
    payload, s0, p0 = load(directory, workflow, identity)
    assert payload["base_commit"] == workflow.base == s0.roots[0].git_head
    assert p0.roots[0].git_head == workflow.p0_head != workflow.base
    assert payload["source_config_fingerprints"] == workflow.summary["config_fingerprints"]
    assert payload["requirement_messages"] == [workflow.requirement.decode()]
    assert (directory / "requirement.md").read_bytes() == workflow.requirement
    for name in ("s0", "p0"):
        assert (directory / name / "manifest.json").read_bytes() == (workflow.root / "snapshots" / name / "manifest.json").read_bytes()
        for entry in getattr(workflow, name).roots[0].entries:
            assert (directory / name / "blobs" / entry.content_hash).read_bytes() == (
                workflow.root / "snapshots" / name / "blobs" / entry.content_hash
            ).read_bytes()
        bundle = directory / name / "roots/0/history.bundle"
        assert digest(bundle) == digest(workflow.root / "snapshots" / name / "roots/0/history.bundle")
        assert git(workflow.source, "bundle", "list-heads", str(bundle)).split()[0] == getattr(workflow, name).roots[0].git_head
    native = directory / payload["native_calls"][0]["output_dir"]
    for name in ("request.json", "metadata.json", "events.jsonl", "terminal-result.json", "usage.json"):
        assert (native / name).read_bytes() == (workflow.native / name).read_bytes()
    assert payload["native_session_resume"] is False
    assert payload["evaluation_kind"] == "recovered_attempt_not_fresh_pass_at_1"
    assert json.loads((directory / "completed.json").read_bytes())["recovery_sha256"] == identity


def test_publication_is_last_and_default_loader_rejects_incomplete_export(workflow):
    directory = workflow.tmp / "package"
    real_write = recovery.atomic_write_owner_only_text
    seen = []

    def stop_before_publication(path, text, **kwargs):
        if path.name == "completed.json":
            identity = digest(directory / "recovery.json")
            recovery.load_p0_recovery(directory, requirement_bytes=workflow.requirement,
                                      expected_sha256=identity, require_publication=False)
            seen.append(identity)
            raise OSError("interrupted before publication")
        return real_write(path, text, **kwargs)

    with mock.patch.object(recovery, "atomic_write_owner_only_text", side_effect=stop_before_publication), \
            pytest.raises(OSError, match="interrupted before publication"):
        recovery.export_p0_recovery(workflow.root, directory)
    assert len(seen) == 1
    assert not (directory / "completed.json").exists()
    with pytest.raises((ValueError, FileNotFoundError)):
        load(directory, workflow, seen[0])


@pytest.mark.parametrize("field,value", [("metadata_status", "timed_out"), ("actual_exit_code", 137),
                                         ("terminal_status", "failed"), ("terminal_exit_code", 1)])
def test_export_requires_actual_successful_native_completion(workflow, field, value):
    name = "metadata.json" if field in {"metadata_status", "actual_exit_code"} else "terminal-result.json"
    receipt = json.loads((workflow.native / name).read_bytes())
    key = {"metadata_status": "status", "terminal_status": "status", "terminal_exit_code": "exit_code"}.get(field, field)
    receipt[key] = value
    json_write(workflow.native / name, receipt)
    with pytest.raises(ValueError, match="native completion"):
        export(workflow)
    assert not (workflow.tmp / "package/completed.json").exists()


@pytest.mark.parametrize("name,key,value", [
    ("metadata.json", "request_id", "unrelated-request"),
    ("terminal-result.json", "request_id", "unrelated-request"),
    ("terminal-result.json", "session_id", "unrelated-session"),
    ("metadata.json", "terminal_result_received", False),
])
def test_export_rejects_unbound_or_missing_terminal_native_evidence(workflow, name, key, value):
    path = workflow.native / name
    receipt = json.loads(path.read_bytes())
    receipt[key] = value
    json_write(path, receipt)
    with pytest.raises(ValueError):
        export(workflow)
    assert not (workflow.tmp / "package/completed.json").exists()


def test_restored_source_cannot_be_reexported_as_direct_p0(workflow):
    workflow.summary["stages"]["p0"]["status"] = "restored_source"
    json_write(workflow.root / "summary.json", workflow.summary)
    with pytest.raises(ValueError, match="directly completed P0"):
        export(workflow)
    assert not (workflow.tmp / "package").exists()


def test_source_requirement_change_and_missing_native_asset_cannot_publish(workflow):
    (workflow.root / "requirement.md").write_bytes(workflow.requirement + b"changed")
    with pytest.raises(ValueError, match="requirement or baseline"):
        export(workflow)
    assert not (workflow.tmp / "package/completed.json").exists()
    shutil.rmtree(workflow.tmp / "package")
    (workflow.root / "requirement.md").write_bytes(workflow.requirement)
    (workflow.native / "events.jsonl").unlink()
    with pytest.raises(ValueError, match="regular file"):
        export(workflow)
    assert not (workflow.tmp / "package/completed.json").exists()


def test_export_rejects_snapshot_directory_symlink_escape(workflow):
    original = workflow.root / "snapshots/s0"
    outside = workflow.tmp / "outside-s0"
    original.rename(outside)
    original.symlink_to(outside, target_is_directory=True)
    with pytest.raises((ValueError, OSError)):
        export(workflow)
    assert not (workflow.tmp / "package/completed.json").exists()


def test_export_rejects_forged_blob_path_before_any_outside_write(workflow):
    manifest_path = workflow.root / "snapshots/s0/manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["roots"][0]["entries"][0]["content_hash"] = "../../../leak"
    json_write(manifest_path, manifest)
    workflow.summary["snapshots"]["s0"]["manifest_hash"] = hashlib.sha256(
        manifest_path.read_bytes().rstrip(b"\n")
    ).hexdigest()
    json_write(workflow.root / "summary.json", workflow.summary)
    (workflow.root / "leak").write_text("synthetic private bytes that must stay in source\n")
    with pytest.raises(ValueError):
        export(workflow)
    assert not (workflow.tmp / "leak").exists()
    assert not (workflow.tmp / "package/completed.json").exists()


@pytest.mark.parametrize("asset", ["requirement.md", "s0/manifest.json", "p0/roots/0/history.bundle",
                                   "origin/summary.json", "origin/p0-native-calls/0/events.jsonl", "source_blob"])
def test_tampered_asset_rejected_before_target_changes(workflow, asset):
    directory, identity = export(workflow)
    workspace = target(workflow)
    before = repo_state(workspace)
    if asset == "source_blob":
        asset = f"p0/blobs/{workflow.p0.roots[0].entries[0].content_hash}"
    (directory / asset).write_bytes(b"tampered")
    with pytest.raises(ValueError, match="file hash"):
        recovery.restore_p0_recovery(directory, workspace=workspace, requirement_bytes=workflow.requirement,
                                    expected_sha256=identity)
    assert repo_state(workspace) == before


@pytest.mark.parametrize("mutation", ["hash", "requirement", "missing_marker", "wrong_marker"])
def test_authority_and_publication_gates_reject_before_target_changes(workflow, mutation):
    directory, identity = export(workflow)
    workspace = target(workflow)
    before = repo_state(workspace)
    requirement = workflow.requirement
    if mutation == "hash":
        identity = "0" * 64
    elif mutation == "requirement":
        requirement += b"different request"
    elif mutation == "missing_marker":
        (directory / "completed.json").unlink()
    else:
        json_write(directory / "completed.json", {"schema": "jiuwenswarm/p0-recovery-publication/v1", "recovery_sha256": "0" * 64})
    with pytest.raises((ValueError, FileNotFoundError)):
        recovery.restore_p0_recovery(directory, workspace=workspace, requirement_bytes=requirement,
                                    expected_sha256=identity)
    assert repo_state(workspace) == before


@pytest.mark.parametrize("mutation", ["traversal", "asset_symlink", "source_path", "missing_blob"])
def test_structural_escape_or_missing_source_rejected_before_target_changes(workflow, mutation):
    directory, identity = export(workflow)
    workspace = target(workflow)
    before = repo_state(workspace)
    if mutation == "traversal":
        outside = workflow.tmp / "outside"
        outside.write_text("outside bytes")
        identity = rewrite_package(directory, lambda payload: payload["files"].update({"../outside": digest(outside)}))
    elif mutation == "asset_symlink":
        path = directory / "origin/p0-native-calls/0/events.jsonl"
        path.unlink()
        path.symlink_to(workflow.native / "events.jsonl")
    elif mutation == "source_path":
        path = directory / "p0/manifest.json"
        manifest = json.loads(path.read_bytes())
        manifest["roots"][0]["entries"][0]["relative_path_b64"] = base64.b64encode(b"../escaped.py").decode()
        json_write(path, manifest)

        def rehash(payload):
            payload["files"]["p0/manifest.json"] = digest(path)
            payload["source_manifest_sha256"] = hashlib.sha256(path.read_bytes().rstrip(b"\n")).hexdigest()

        identity = rewrite_package(directory, rehash)
    else:
        relative = f"p0/blobs/{workflow.p0.roots[0].entries[0].content_hash}"
        identity = rewrite_package(directory, lambda payload: payload["files"].pop(relative))
    with pytest.raises(ValueError):
        recovery.restore_p0_recovery(directory, workspace=workspace, requirement_bytes=workflow.requirement,
                                    expected_sha256=identity)
    assert repo_state(workspace) == before
    assert not (workflow.tmp / "escaped.py").exists()


def test_loader_requires_listed_native_invocation_evidence(workflow):
    directory, _ = export(workflow)
    workspace = target(workflow)
    before = repo_state(workspace)

    def remove_native(payload):
        payload["native_calls"] = []
        payload["files"] = {name: value for name, value in payload["files"].items()
                            if not name.startswith("origin/p0-native-calls/")}

    identity = rewrite_package(directory, remove_native)
    with pytest.raises(ValueError):
        recovery.restore_p0_recovery(directory, workspace=workspace, requirement_bytes=workflow.requirement,
                                    expected_sha256=identity)
    assert repo_state(workspace) == before


def test_loader_binds_new_requirement_to_original_authority(workflow):
    directory, _ = export(workflow)
    workspace = target(workflow)
    before = repo_state(workspace)
    changed = b"An unrelated new task, relabeled after export.\n"
    (directory / "requirement.md").write_bytes(changed)

    def relabel(payload):
        payload["requirement_messages"] = [changed.decode()]
        payload["requirement_sha256"] = hashlib.sha256(changed).hexdigest()
        payload["files"]["requirement.md"] = digest(directory / "requirement.md")

    identity = rewrite_package(directory, relabel)
    with pytest.raises(ValueError, match="origin evidence"):
        recovery.restore_p0_recovery(directory, workspace=workspace, requirement_bytes=changed,
                                    expected_sha256=identity)
    assert repo_state(workspace) == before


@pytest.mark.parametrize("change", ["restored_source", "wrong_s0"])
def test_loader_checks_original_direct_completion_and_s0_binding(workflow, change):
    directory, _ = export(workflow)
    origin_path = directory / "origin/summary.json"
    origin = json.loads(origin_path.read_bytes())
    if change == "restored_source":
        origin["stages"]["p0"]["status"] = "restored_source"
    else:
        origin["snapshots"]["s0"]["manifest_hash"] = "0" * 64
    json_write(origin_path, origin)

    def rehash_origin(payload):
        payload["source_summary_sha256"] = digest(origin_path)
        payload["files"]["origin/summary.json"] = digest(origin_path)

    identity = rewrite_package(directory, rehash_origin)
    with pytest.raises(ValueError, match="origin evidence"):
        load(directory, workflow, identity)


@pytest.mark.parametrize("kind", ["dirty", "staged", "untracked", "wrong_base"])
def test_destination_must_be_clean_original_baseline_without_mutation(workflow, kind):
    directory, identity = export(workflow)
    workspace = target(workflow)
    if kind == "wrong_base":
        git(workspace, "checkout", "--detach", "-q", workflow.p0_head)
    else:
        path = workspace / ("untracked.txt" if kind == "untracked" else "source.py")
        path.write_text("keep this destination change\n")
        if kind == "staged":
            git(workspace, "add", path.name)
    git(workspace, "status", "--porcelain=v1", "--untracked-files=all")
    before = repo_state(workspace)
    with pytest.raises(ValueError, match="clean repository"):
        recovery.restore_p0_recovery(directory, workspace=workspace, requirement_bytes=workflow.requirement,
                                    expected_sha256=identity)
    assert repo_state(workspace) == before


@pytest.mark.parametrize("container_prefix", [False, True])
def test_relocated_package_restores_source_while_preserving_target_base_and_index(workflow, container_prefix):
    if container_prefix:
        workflow.summary["worker_calls"][0]["output_dir"] = "/agent_out/long-horizon-suite/02-p0/native"
        json_write(workflow.root / "summary.json", workflow.summary)
    directory, identity = export(workflow)
    workspace = target(workflow)
    baseline_index_tree = git(workspace, "write-tree")
    baseline_index = (workspace / ".git/index").read_bytes()
    moved = workflow.tmp / "another-host/p0-package"
    moved.parent.mkdir()
    shutil.move(str(directory), moved)
    shutil.rmtree(workflow.root)
    shutil.rmtree(workflow.source)
    payload, receipt = recovery.restore_p0_recovery(moved, workspace=workspace,
                                                   requirement_bytes=workflow.requirement, expected_sha256=identity)
    assert (workspace / "source.py").read_text() == "P0_WORKING = 3\n"
    assert (workspace / "committed.txt").read_text() == "committed P0 addition\n"
    assert (workspace / "staged.txt").read_text() == "P0 staged addition\n"
    assert (workspace / "untracked.bin").read_bytes() == b"\x00P0 binary\xff\n"
    assert not (workspace / "removed.txt").exists()
    assert git(workspace, "rev-parse", "HEAD") == workflow.base
    assert git(workspace, "write-tree") == baseline_index_tree
    assert (workspace / ".git/index").read_bytes() == baseline_index
    assert receipt["git_history_restored"] is False
    assert receipt["native_session_resume"] is False
    assert receipt["source_p0_head"] == workflow.p0_head
    assert not receipt.get("index_restored", False)
    assert payload["resume_stage"] == "clarification"


def test_clean_target_stat_refresh_does_not_rewrite_baseline_index(workflow):
    directory, identity = export(workflow)
    workspace = target(workflow)
    before = (workspace / ".git/index").read_bytes()
    path = workspace / "source.py"
    info = path.stat()
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 5_000_000_000))
    recovery.restore_p0_recovery(directory, workspace=workspace, requirement_bytes=workflow.requirement,
                                expected_sha256=identity)
    assert (workspace / ".git/index").read_bytes() == before


@pytest.mark.parametrize("detached", [False, True])
def test_failed_repair_fallback_restores_p0_source_head_and_exact_index(workflow, detached):
    source = workflow.source
    symbolic = "" if detached else git(source, "symbolic-ref", "HEAD")
    if detached:
        git(source, "checkout", "--detach", "-q", workflow.p0_head)
        # Checkout can rewrite the index; recapture the P0 actually admitted.
        workflow.p0 = WorkspaceSnapshotter().capture(Workspace.from_cwd(str(source)), workflow.root / "snapshots/p0-detached",
                                                      snapshot_id="p0-detached", include_git_history=True)
    captured_index = (Path(workflow.p0.artifact_root) / "blobs" / workflow.p0.roots[0].git_index_sha256).read_bytes()
    (source / "source.py").write_text("FAILED_REPAIR = 9\n")
    (source / "failed-only.txt").write_text("failed repair artifact\n")
    git(source, "add", "-A")
    git(source, "commit", "-qm", "failed repair commit retained as evidence")
    failed_head = git(source, "rev-parse", "HEAD")
    (source / "source.py").write_text("UNCOMMITTED_FAILED_REPAIR = 10\n")
    receipt = recovery.restore_snapshot_in_place(workflow.p0, source, symbolic)
    assert git(source, "rev-parse", "HEAD") == workflow.p0_head
    assert (source / ".git/index").read_bytes() == captured_index
    assert (source / "source.py").read_text() == "P0_WORKING = 3\n"
    assert not (source / "failed-only.txt").exists()
    assert WorkspaceSnapshotter().matches(workflow.p0)
    assert git(source, "cat-file", "-t", failed_head) == "commit"
    if symbolic:
        assert git(source, "symbolic-ref", "HEAD") == symbolic
    else:
        assert subprocess.run(["git", "-C", str(source), "symbolic-ref", "-q", "HEAD"],
                              capture_output=True, check=False).returncode == 1
    assert receipt["source_restored"] is receipt["head_restored"] is receipt["index_restored"] is True


def test_corrupt_fallback_index_is_rejected_before_source_or_head_changes(workflow):
    source = workflow.source
    symbolic = git(source, "symbolic-ref", "HEAD")
    (source / "source.py").write_text("FAILED_REPAIR = 9\n")
    git(source, "add", "-A")
    git(source, "commit", "-qm", "failed repair")
    before = repo_state(source)
    blob = Path(workflow.p0.artifact_root) / "blobs" / workflow.p0.roots[0].git_index_sha256
    blob.write_bytes(b"broken index")
    with pytest.raises(ValueError, match="index blob changed"):
        recovery.restore_snapshot_in_place(workflow.p0, source, symbolic)
    assert repo_state(source) == before


def test_invalid_fallback_ref_is_rejected_before_source_changes(workflow):
    source = workflow.source
    (source / "source.py").write_text("FAILED_CANDIDATE_MUST_BE_UNTOUCHED = 9\n")
    before = repo_state(source)
    with pytest.raises((ValueError, subprocess.CalledProcessError)):
        recovery.restore_snapshot_in_place(workflow.p0, source, "refs/heads/bad..name")
    assert repo_state(source) == before
