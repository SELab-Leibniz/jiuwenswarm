# Copyright (c) 2026 Chrys. All rights reserved.

"""Original fixture link bytes survive sealing without entering a model view."""

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
pytest.importorskip("pact_core.benchmark")
from pact_core.runtime import WorkRuntime
from pact_core.runtime.export import export_candidate, verify_export
from pact_core.runtime.schemas import RuntimeContractError

from jiuwenswarm.long_horizon.pact.long_horizon_evidence import stage_record
from jiuwenswarm.long_horizon.clarification.checkpoints import verify_stage_checkpoint


@pytest.mark.parametrize("target", ["/dev/null", "/non/existing/file", "../../outside", "../source/x"])
def test_native_stage_keeps_original_external_fixture_as_hidden_metadata(
    tmp_path: Path, git_repo_factory: Callable[[Path], Path], target: str
) -> None:
    source = git_repo_factory(tmp_path / "repo")
    (source / "fixture").symlink_to(target)
    (source / "fixture-alias").symlink_to("fixture")
    for args in (["add", "fixture", "fixture-alias"], ["commit", "-m", "Original fixture"]):
        subprocess.run(["git", *args], cwd=source, stdin=subprocess.DEVNULL, capture_output=True, check=True)
    runtime = WorkRuntime(source, worktree_root=tmp_path / "worktrees")
    state = runtime.create_work(
        request_id="fixture-test",
        contract={
            "schema": "pact-runtime/goal-contract/v1",
            "goal": "Preserve original fixture",
            "acceptance_criteria": [{"id": "preserve", "text": "Preserve original fixture"}],
            "non_goals": [],
        },
    )
    output = tmp_path / "output"
    receipt = export_candidate(runtime, state.candidate, output / "export")
    export_sha = receipt.artifact_sha256
    record, snapshot = stage_record(
        output_root=output,
        turn_root=output / "evidence" / "turn_1",
        stage="s0",
        receipt=receipt,
        metadata={},
    )
    _, checked = verify_stage_checkpoint(output / str(record["checkpoint"]))
    assert checked.manifest_hash == snapshot.manifest_hash
    entry = next(item for item in snapshot.roots[0].entries if item.relative_path == "fixture")
    assert entry.kind == "symlink"
    assert not entry.model_visible
    assert entry.metadata_reason == "symlink_target_outside_snapshot"
    assert entry.content_hash == hashlib.sha256(target.encode()).hexdigest()
    alias = next(item for item in snapshot.roots[0].entries if item.relative_path == "fixture-alias")
    assert alias.kind == "symlink" and not alias.model_visible
    assert (Path(snapshot.artifact_root) / "blobs" / entry.content_hash).read_bytes() == target.encode()
    view = Path(snapshot.roots[0].view_root) / "fixture"
    assert not view.exists() and not view.is_symlink()
    assert not (receipt.snapshot / "fixture").is_symlink()
    assert (source / "fixture").readlink() == Path(target)
    assert verify_export(receipt)["delivery_authorized"] is False
    assert receipt.artifact_sha256 == export_sha


@pytest.mark.parametrize("name", [".gitignore", ".GITIGNORE", "node_modules/fixture"])
def test_stage_rejects_external_ignore_rules_or_omitted_fixture_metadata(
    tmp_path: Path, git_repo_factory: Callable[[Path], Path], name: str
) -> None:
    source = git_repo_factory(tmp_path / "repo")
    fixture = source / name
    fixture.parent.mkdir(exist_ok=True)
    fixture.symlink_to("/dev/null")
    for args in (["add", "-f", name], ["commit", "-m", "Original fixture"]):
        subprocess.run(["git", *args], cwd=source, stdin=subprocess.DEVNULL, capture_output=True, check=True)
    runtime = WorkRuntime(source, worktree_root=tmp_path / "worktrees")
    state = runtime.create_work(
        request_id="fixture-test",
        contract={
            "schema": "pact-runtime/goal-contract/v1",
            "goal": "Preserve original fixture",
            "acceptance_criteria": [{"id": "preserve", "text": "Preserve original fixture"}],
            "non_goals": [],
        },
    )
    output = tmp_path / "output"
    receipt = export_candidate(runtime, state.candidate, output / "export")
    with pytest.raises(RuntimeContractError, match=r"external ignore|omitted or exposed"):
        stage_record(
            output_root=output,
            turn_root=output / "evidence" / "turn_1",
            stage="s0",
            receipt=receipt,
            metadata={},
        )
    assert verify_export(receipt)["delivery_authorized"] is False
