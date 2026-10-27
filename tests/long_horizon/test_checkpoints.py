"""Offline integrity and portability checks for immutable stage boundaries."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from jiuwenswarm.long_horizon.checkpoints import (
    copy_snapshot,
    restore_stage_to_empty_directory,
    save_stage_checkpoint,
    verify_stage_checkpoint,
)
from jiuwenswarm.long_horizon.clarification.compat import WorkingDir, Workspace
from jiuwenswarm.long_horizon.clarification.snapshot import WorkspaceSnapshotter


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


@pytest.fixture
def source(tmp_path):
    repo = tmp_path / "source"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Checkpoint Test")
    git(repo, "config", "user.email", "test@localhost")
    (repo / "src").mkdir()
    (repo / "src/api.py").write_text("def value():\n    return 1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    return repo


def save(tmp_path, source, **kwargs):
    return save_stage_checkpoint(tmp_path / "run", stage="p0", status="completed", workspace=source,
                                 metadata={"fixed_denominator": 25}, native_calls=kwargs.pop("native_calls", []),
                                 artifact_paths=kwargs.pop("artifact_paths", []), **kwargs)


def rewrite_record(record, transform):
    """Rehash an edited record to reach its structural/path checks in isolation."""
    payload = json.loads(record.read_bytes())
    transform(payload)
    raw = (json.dumps(payload, sort_keys=True) + "\n").encode()
    record.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    record.with_suffix(".sha256").write_text(digest + "\n")
    (record.parent / "completed.json").write_text(json.dumps({"schema": payload["schema"], "checkpoint_sha256": digest}))


def native_call(root):
    output = root / "run/02-solver/native-1"
    state = output / "state"
    session = state / "agent/sessions/session-1"
    session.mkdir(parents=True)
    (session / "history.jsonl").write_text('{"role":"assistant","content":"actual history"}\n')
    (session / "metadata.json").write_text('{"session_id":"session-1"}\n')
    (state / "config").mkdir()
    (state / "config/config.yaml").write_text("api_key: PRIVATE-CONFIG-SECRET\n")
    (state / "config/.env").write_text("API_KEY=PRIVATE-ENV-SECRET\n")
    unrelated = state / "agent/sessions/unrelated-session"
    unrelated.mkdir()
    (unrelated / "history.json").write_text("UNRELATED-SESSION\n")
    receipts = {
        "metadata.json": {"state_dir": str(state), "session_id": "session-1", "source": {"commit": "pinned"}},
        "request.json": {"request_id": "request-1", "prompt": "the actual request"},
        "terminal-result.json": {"status": "completed", "output": "actual terminal"},
        "usage.json": {"input_tokens": 17, "output_tokens": 9},
        "final-response.json": {"output": "actual final"},
    }
    for name, payload in receipts.items():
        (output / name).write_text(json.dumps(payload) + "\n")
    (output / "events.jsonl").write_text('{"event_type":"chat.tool_call","payload":{"name":"read_file"}}\n')
    (output / "native-config.before.yaml").write_text("api_key: '[REDACTED]'\n")
    (output / "native-config.after.yaml").write_text("api_key: '[REDACTED]'\n")
    return {"output_dir": str(output), "session_id": "session-1", "stage": "solver", "status": "completed"}


def test_stage_owns_source_artifacts_native_receipts_and_config_fingerprint(tmp_path, source):
    call = native_call(tmp_path)
    accepted = tmp_path / "run/02-solver/accepted.json"
    accepted.write_text('{"accepted":true}\n')
    record = save(tmp_path, source, native_calls=[call], artifact_paths=[accepted.parent])
    payload, snapshot = verify_stage_checkpoint(record)
    native = payload["native_calls"][0]
    assert native["receipts"]["request.json"] == hashlib.sha256((Path(call["output_dir"]) / "request.json").read_bytes()).hexdigest()
    assert native["raw_config_sha256"] == hashlib.sha256(b"api_key: PRIVATE-CONFIG-SECRET\n").hexdigest()
    assert "agent/sessions/session-1/history.jsonl" in native["state_files"]
    assert set(native["receipts"]) == {"request.json", "metadata.json", "events.jsonl", "terminal-result.json",
                                       "usage.json", "final-response.json"}
    assert set(native["config_fingerprints"]) == {"native-config.before.yaml", "native-config.after.yaml"}
    assert payload["metadata"] == {"fixed_denominator": 25}
    assert Path(snapshot.roots[0].history_bundle).is_file()
    for path in record.parent.rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            assert b"PRIVATE-CONFIG-SECRET" not in data
            assert b"PRIVATE-ENV-SECRET" not in data
            assert b"UNRELATED-SESSION" not in data
    # Later solver/session edits cannot change the frozen stage.
    (source / "src/api.py").write_text("later source\n")
    accepted.write_text("later acceptance\n")
    shutil.rmtree(Path(call["output_dir"]))
    verify_stage_checkpoint(record)
    destination = tmp_path / "restored"
    restore_stage_to_empty_directory(record, destination)
    assert (destination / "src/api.py").read_text() == "def value():\n    return 1\n"
    assert (source / "src/api.py").read_text() == "later source\n"
    assert not (destination / ".git").exists()


def test_checkpoint_can_move_without_original_run_source_or_snapshot(tmp_path, source):
    original = WorkspaceSnapshotter().capture(Workspace.from_cwd(str(source)), tmp_path / "original-s0",
                                               snapshot_id="s0", include_git_history=True)
    record = save(tmp_path, source, snapshot=original)
    moved = tmp_path / "other-host"
    shutil.move(str(record.parent), moved)
    shutil.rmtree(tmp_path / "run")
    shutil.rmtree(tmp_path / "original-s0")
    shutil.rmtree(source)
    payload, snapshot = verify_stage_checkpoint(moved / "checkpoint.json")
    assert snapshot.roots[0].view_root.startswith(str(moved))
    assert snapshot.roots[0].history_bundle.startswith(str(moved))
    assert payload["snapshot"]["original_manifest_sha256"] == original.manifest_hash
    restore_stage_to_empty_directory(moved / "checkpoint.json", tmp_path / "restored")
    assert (tmp_path / "restored/src/api.py").is_file()


def test_copy_snapshot_uses_canonical_assets_after_original_directory_moved(tmp_path, source):
    original = WorkspaceSnapshotter().capture(Workspace.from_cwd(str(source)), tmp_path / "old",
                                               snapshot_id="s0", include_git_history=True)
    shutil.move(str(tmp_path / "old"), tmp_path / "moved")
    original = WorkspaceSnapshotter.load(tmp_path / "moved", expected_manifest_hash=original.manifest_hash)
    copied, evidence = copy_snapshot(original, tmp_path / "copy")
    assert Path(copied.roots[0].history_bundle).is_file()
    assert copied.roots[0].history_bundle.startswith(str(tmp_path / "copy"))
    assert "roots/0/history.bundle" in evidence["files"]
    shutil.rmtree(tmp_path / "moved")
    WorkspaceSnapshotter._validate_recovery_blobs(copied)


@pytest.mark.parametrize("asset", ["manifest", "blob", "bundle", "artifact", "native", "index"])
def test_any_asset_corruption_blocks_restore_before_creating_destination(tmp_path, source, asset):
    call = native_call(tmp_path)
    artifact = tmp_path / "run/accepted.json"
    artifact.write_text("{}\n")
    record = save(tmp_path, source, native_calls=[call], artifact_paths=[artifact])
    _, snapshot = verify_stage_checkpoint(record)
    paths = {
        "manifest": record.parent / "workspace/manifest.json",
        "blob": record.parent / "workspace/blobs" / snapshot.roots[0].entries[0].content_hash,
        "bundle": Path(snapshot.roots[0].history_bundle),
        "artifact": record.parent / "artifacts/accepted.json",
        "native": record.parent / "native/0/events.jsonl",
        "index": Path(snapshot.roots[0].git_index_blob),
    }
    paths[asset].write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="byte manifest"):
        restore_stage_to_empty_directory(record, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


def test_failed_or_unpublished_capture_is_never_resumable(tmp_path, source):
    import jiuwenswarm.long_horizon.checkpoints as module

    real_write = module._write

    def fail_at_marker(path, data):
        if path.name == "completed.json":
            raise OSError("simulated publication failure")
        return real_write(path, data)

    with mock.patch.object(module, "_write", side_effect=fail_at_marker), \
            pytest.raises(OSError, match="publication failure"):
        save(tmp_path, source)
    record = next((tmp_path / "run/checkpoints").glob("*/checkpoint.json"))
    with pytest.raises(FileNotFoundError):
        verify_stage_checkpoint(record)
    assert not (record.parent / "completed.json").exists()


@pytest.mark.parametrize("target", ["artifact", "native_state", "blob"])
def test_symlink_escape_is_rejected(tmp_path, source, target):
    outside = tmp_path / "outside-secret"
    outside.write_text("must not be copied")
    artifact = tmp_path / "run/accepted"
    artifact.mkdir(parents=True)
    call = native_call(tmp_path)
    if target == "artifact":
        (artifact / "escape").symlink_to(outside)
        with pytest.raises(ValueError, match="symlinks"):
            save(tmp_path, source, artifact_paths=[artifact])
    elif target == "native_state":
        (Path(call["output_dir"]) / "state/agent/sessions/session-1/escape").symlink_to(outside)
        with pytest.raises(ValueError, match="symlinks"):
            save(tmp_path, source, native_calls=[call])
    else:
        record = save(tmp_path, source)
        blob = next((record.parent / "workspace/blobs").iterdir())
        blob.unlink()
        blob.symlink_to(outside)
        with pytest.raises(ValueError, match="symlink"):
            verify_stage_checkpoint(record)
    assert outside.read_text() == "must not be copied"


def test_safe_source_symlink_rebuilt_without_original_view_and_restored(tmp_path, source):
    (source / "api-link.py").symlink_to("src/api.py")
    original = WorkspaceSnapshotter().capture(Workspace.from_cwd(str(source)), tmp_path / "s0",
                                               snapshot_id="s0", include_git_history=True)
    shutil.rmtree(Path(original.roots[0].view_root))
    record = save(tmp_path, source, snapshot=original)
    _, snapshot = verify_stage_checkpoint(record)
    view = Path(snapshot.roots[0].view_root)
    assert (view / "api-link.py").is_symlink()
    assert (view / "api-link.py").resolve() == view / "src/api.py"
    restore_stage_to_empty_directory(record, tmp_path / "restored")
    assert os.readlink(tmp_path / "restored/api-link.py") == "src/api.py"


def test_restore_rejects_nonempty_and_symlink_destination(tmp_path, source):
    record = save(tmp_path, source)
    destination = tmp_path / "existing"
    destination.mkdir()
    (destination / "keep").write_text("keep")
    with pytest.raises(ValueError, match="empty"):
        restore_stage_to_empty_directory(record, destination)
    linked = tmp_path / "linked"
    linked.symlink_to(destination, target_is_directory=True)
    with pytest.raises(ValueError, match="empty"):
        restore_stage_to_empty_directory(record, linked)
    assert (destination / "keep").read_text() == "keep"


@pytest.mark.parametrize("with_reference", [False, True])
def test_restore_rejects_multiple_roots_and_external_references(tmp_path, source, with_reference):
    extra = tmp_path / "extra"
    extra.mkdir()
    (extra / "reference.txt").write_text("reference")
    workspace = Workspace.from_cwd(str(source))
    if with_reference:
        workspace.reference_files = [str(extra / "reference.txt")]
    else:
        workspace.working_dirs = [WorkingDir(str(source), is_primary=True), WorkingDir(str(extra))]
    snapshot = WorkspaceSnapshotter().capture(workspace, tmp_path / "s0", snapshot_id="s0", include_git_history=True)
    record = save(tmp_path, source, snapshot=snapshot)
    with pytest.raises(ValueError, match="one source root and no reference"):
        restore_stage_to_empty_directory(record, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


def test_checkpoint_ignores_informational_original_source_root_for_restoration(tmp_path, source):
    record = save(tmp_path, source)
    # Existing source_root is an original-run path. Only the caller-provided
    # empty destination can receive writes from the restoration API.
    (source / "src/api.py").write_text("preserve live repair\n")
    restore_stage_to_empty_directory(record, tmp_path / "restored")
    assert (source / "src/api.py").read_text() == "preserve live repair\n"


def test_unlisted_asset_and_record_tampering_are_rejected(tmp_path, source):
    record = save(tmp_path, source)
    (record.parent / "unlisted").write_text("unexpected")
    with pytest.raises(ValueError, match="byte manifest"):
        verify_stage_checkpoint(record)
    (record.parent / "unlisted").unlink()
    record.write_bytes(record.read_bytes() + b" ")
    with pytest.raises(ValueError, match="record hash"):
        verify_stage_checkpoint(record)


def test_missing_selected_artifact_aborts_without_completion_marker(tmp_path, source):
    missing = tmp_path / "run/missing-accepted.json"
    with pytest.raises(FileNotFoundError):
        save(tmp_path, source, artifact_paths=[missing])
    checkpoints = list((tmp_path / "run/checkpoints").iterdir())
    assert len(checkpoints) == 1
    assert not (checkpoints[0] / "completed.json").exists()


def test_forged_source_path_cannot_escape_restoration_destination(tmp_path, source):
    record = save(tmp_path, source)
    manifest_path = record.parent / "workspace/manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["roots"][0]["entries"][0]["relative_path_b64"] = base64.b64encode(b"../escape.py").decode()
    raw = (json.dumps(manifest, sort_keys=True) + "\n").encode()
    manifest_path.write_bytes(raw)

    def rehash(payload):
        payload["snapshot"]["manifest_sha256"] = hashlib.sha256(raw.rstrip(b"\n")).hexdigest()
        payload["assets"]["workspace/manifest.json"].update(sha256=hashlib.sha256(raw).hexdigest(), size=len(raw))

    rewrite_record(record, rehash)
    with pytest.raises(ValueError, match="unsafe checkpoint relative path"):
        restore_stage_to_empty_directory(record, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()
    assert not (tmp_path / "escape.py").exists()


def test_runtime_config_receipt_secrets_are_fingerprinted_without_copying(tmp_path, source):
    call = native_call(tmp_path)
    output = Path(call["output_dir"])
    config = output / "native-config.after.yaml"
    config.write_text("api_key: RAW-RUNTIME-SECRET\n")
    record = save(tmp_path, source, native_calls=[call], artifact_paths=[output])
    payload, _ = verify_stage_checkpoint(record)
    assert payload["native_calls"][0]["config_fingerprints"][config.name] == hashlib.sha256(config.read_bytes()).hexdigest()
    assert all(b"RAW-RUNTIME-SECRET" not in path.read_bytes()
               for path in record.parent.rglob("*") if path.is_file())


def test_model_view_mode_tampering_is_detected(tmp_path, source):
    record = save(tmp_path, source)
    _, snapshot = verify_stage_checkpoint(record)
    path = Path(snapshot.roots[0].view_root) / "src/api.py"
    path.chmod(0o500)
    with pytest.raises(ValueError, match="byte manifest"):
        verify_stage_checkpoint(record)


def test_file_read_is_anchored_before_parent_symlink_replacement(tmp_path):
    import jiuwenswarm.long_horizon.checkpoints as module

    selected = tmp_path / "selected"
    selected.mkdir()
    (selected / "receipt").write_text("authorized bytes")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "receipt").write_text("external secret")
    original_open = module._open_posix_directory

    def swap_after_open(path, *, create):
        fd = original_open(path, create=create)
        selected.rename(tmp_path / "moved")
        selected.symlink_to(outside, target_is_directory=True)
        return fd

    with mock.patch.object(module, "_open_posix_directory", side_effect=swap_after_open):
        assert module._read_regular(selected / "receipt") == b"authorized bytes"
