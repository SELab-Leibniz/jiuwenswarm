from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from jiuwenswarm.pact_core.runtime import RuntimeContractError

from jiuwenswarm.lh.cli import arguments_from_namespace, build_parser
from jiuwenswarm.lh.file_artifacts import FileArtifacts
from jiuwenswarm.lh.snapshot_resources import MAX_SNAPSHOT_TOTAL_BYTES, SnapshotResources


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "1024", None, MAX_SNAPSHOT_TOTAL_BYTES + 1])
def test_reject_invalid_or_unbounded_capacity(value):
    with pytest.raises(RuntimeContractError, match="snapshot_max_total_bytes"):
        SnapshotResources(value)


def test_capacity_exact_boundary_and_one_byte_over():
    limits = SnapshotResources(7)
    limits.check_total(7)
    with pytest.raises(RuntimeContractError, match="exceeds 7 bytes"):
        limits.check_total(8)
    assert SnapshotResources(MAX_SNAPSHOT_TOTAL_BYTES).max_total_bytes == MAX_SNAPSHOT_TOTAL_BYTES


def arguments(value):
    return arguments_from_namespace(
        build_parser().parse_args(
            [
                "--task",
                "task.md",
                "--workdir",
                ".",
                "--output",
                "out",
                "--task-id",
                "task",
                "--trial-id",
                "one",
                "--snapshot-max-total-bytes",
                str(value),
            ]
        )
    )


def test_cli_capacity_and_host_frozen_input(tmp_path):
    from test_control_regressions import FixturePlugin

    from jiuwenswarm.lh.controller import LongHorizonBenchmark

    small = LongHorizonBenchmark(arguments(7), plugin=FixturePlugin({"steps": [], "calls": []}))
    large = LongHorizonBenchmark(arguments(8), plugin=FixturePlugin({"steps": [], "calls": []}))
    small_frozen = small._frozen_input(b"original task", tmp_path)
    large_frozen = large._frozen_input(b"original task", tmp_path)
    assert small_frozen["snapshot_resources"]["max_total_bytes"] == 7
    assert large_frozen["snapshot_resources"]["max_total_bytes"] == 8
    assert small_frozen != large_frozen
    assert small._artifacts._snapshot_resources.max_total_bytes == 7
    assert large._artifacts._snapshot_resources.max_total_bytes == 8


def test_file_snapshot_checks_actual_aggregate_size_and_keeps_limit_per_instance(tmp_path, monkeypatch):
    import jiuwenswarm.lh.file_artifacts as module

    source = tmp_path / "source"
    source.mkdir()
    (source / "a").write_bytes(b"abc")
    (source / "b").write_bytes(b"defg")
    patch = tmp_path / "patch"
    patch.write_bytes(b"")
    receipt_file = tmp_path / "receipt.json"
    receipt_file.write_text("{}")
    receipt = SimpleNamespace(
        snapshot=source,
        candidate=SimpleNamespace(checkpoint="cp", tree="tree"),
        artifact=receipt_file,
        artifact_sha256="evidence",
        patch=patch,
    )
    monkeypatch.setattr(module, "verify_export", lambda _: {"opaque_symlinks": {}})
    allowed, refused = FileArtifacts(), FileArtifacts()
    allowed.configure_snapshot_resources(max_total_bytes=7)
    refused.configure_snapshot_resources(max_total_bytes=6)
    record, snapshot = allowed.stage_record(
        output_root=tmp_path, turn_root=tmp_path / "good", stage="s0", receipt=receipt, metadata={}
    )
    assert record["snapshot_resources"]["max_total_bytes"] == 7
    allowed.validate(snapshot)
    with pytest.raises(RuntimeContractError, match="exceeds 6 bytes"):
        refused.stage_record(output_root=tmp_path, turn_root=tmp_path / "bad", stage="s0", receipt=receipt, metadata={})
    assert not list((tmp_path / "bad").glob("checkpoints/*.json"))
    (Path(snapshot.roots[0].view_root) / "a").write_bytes(b"tampered")
    with pytest.raises(RuntimeContractError):
        allowed.validate(snapshot)


def test_reusing_plugin_does_not_change_another_invocation_limit(tmp_path):
    from test_control_regressions import FixturePlugin

    from jiuwenswarm.lh.controller import LongHorizonBenchmark

    plugin = FixturePlugin({"steps": [], "calls": []})
    first = LongHorizonBenchmark(arguments(7), plugin=plugin)
    second = LongHorizonBenchmark(arguments(8), plugin=plugin)
    assert first._artifacts._snapshot_resources.max_total_bytes == 7
    assert second._artifacts._snapshot_resources.max_total_bytes == 8
    assert plugin.artifacts._snapshot_resources.max_total_bytes == 1073741824
    assert first._artifacts is not second._artifacts
    old = first._frozen_input(b"task", tmp_path)
    new = second._frozen_input(b"task", tmp_path)
    state = SimpleNamespace(advisory={plugin.spec.input_key: old})
    with pytest.raises(RuntimeContractError, match="different task, configuration"):
        second._require_same(state, new, {"fingerprint": "fixture-v1"}, "Code")


@pytest.mark.asyncio
async def test_snapshot_failure_happens_before_any_worker_call(tmp_path):
    import json
    from dataclasses import replace

    from test_control_regressions import FixturePlugin, repository

    from jiuwenswarm.lh.controller import LongHorizonBenchmark

    repo = repository(tmp_path)
    task = tmp_path / "task.md"
    task.write_text("Unmodified synthetic snapshot boundary fixture.")
    args = replace(
        arguments(1),
        task=task,
        workdir=repo,
        output=tmp_path / "output",
        workflow="benchmark",
        max_turns=1,
        budget_seconds=30,
        worktree_root=tmp_path / "worktrees",
    )
    calls = {"steps": [], "calls": []}
    outcome = await LongHorizonBenchmark(args, plugin=FixturePlugin(calls)).run()
    report = json.loads(outcome.selection.read_text())
    assert calls["calls"] == []
    assert outcome.internal_failure
    assert not report["internal_accepted"]
    assert report["external_grade"] == {"status": "not_run", "reward": None}
