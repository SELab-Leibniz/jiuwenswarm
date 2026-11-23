# Copyright (c) 2026 Chrys. All rights reserved.

"""Candidate promotion must preserve generated text bytes and its identity guard."""

from __future__ import annotations

import hashlib
import json
import subprocess

import pytest
from pact_core.checkpoint import CheckpointStore


@pytest.mark.parametrize(
    "content",
    [b"name,value\r\nA,1\r\nB,2\r\n", b"first\rsecond\r", b"plain\n", b"legacy,\x93quote\x94\r\n", bytes(range(256))],
)
def test_promotion_preserves_candidate_line_endings(tmp_path, content):
    source = tmp_path / "source"
    source.mkdir()
    for args in (["init", "-b", "main"], ["config", "user.name", "Test"], ["config", "user.email", "test@example.com"]):
        subprocess.run(["git", "-C", str(source), *args], stdin=subprocess.DEVNULL, capture_output=True, check=True)
    (source / "README.md").write_text("Public source\n")
    subprocess.run(["git", "-C", str(source), "add", "."], stdin=subprocess.DEVNULL, capture_output=True, check=True)
    subprocess.run(
        ["git", "-C", str(source), "commit", "-m", "Initial"], stdin=subprocess.DEVNULL, capture_output=True, check=True
    )
    store = CheckpointStore(source, "byte-promotion")
    initialization = store.initialize()
    (store.worker_worktree / "result.csv").write_bytes(content)
    checkpoint = store.checkpoint_worker(round_number=1)
    patch = tmp_path / "candidate.patch"
    outcome = store.promote(checkpoint.commit, patch)
    assert outcome.status == "applied"
    assert (source / "result.csv").read_bytes() == content
    current = store.workspace_identity()
    assert current.workspace_tree == checkpoint.tree
    assert current.head_commit == initialization.head_commit
    assert current.index_tree == initialization.index_tree
    assert current.branch == initialization.branch
    from pact_core.goal_tracker import sha256_text

    assert (
        sha256_text(store._diff(initialization.baseline_commit, checkpoint.commit))
        == hashlib.sha256(patch.read_bytes()).hexdigest()
    )


def test_promotion_still_rejects_source_drift(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    for args in (["init", "-b", "main"], ["config", "user.name", "Test"], ["config", "user.email", "test@example.com"]):
        subprocess.run(["git", "-C", str(source), *args], stdin=subprocess.DEVNULL, capture_output=True, check=True)
    (source / "source.txt").write_text("Original\n")
    subprocess.run(["git", "-C", str(source), "add", "."], stdin=subprocess.DEVNULL, capture_output=True, check=True)
    subprocess.run(
        ["git", "-C", str(source), "commit", "-m", "Initial"], stdin=subprocess.DEVNULL, capture_output=True, check=True
    )
    store = CheckpointStore(source, "drift-guard")
    store.initialize()
    (store.worker_worktree / "source.txt").write_bytes(b"Candidate\r\n")
    checkpoint = store.checkpoint_worker(round_number=1)
    (source / "source.txt").write_text("Unrelated source change\n")
    outcome = store.promote(checkpoint.commit, tmp_path / "candidate.patch")
    assert outcome.status == "blocked"
    assert (source / "source.txt").read_text() == "Unrelated source change\n"


@pytest.mark.asyncio
async def test_full_suite_acceptance_and_export_keep_crlf_bytes(tmp_path, git_repo_factory):
    from test_suite_flow import SuiteAgent, trial

    from jiuwenswarm.lh import Capabilities, ExecutionBinding, LongHorizonBenchmark
    from jiuwenswarm.lh.plugins.generic import GenericPlugin

    class CrlfAgent(SuiteAgent):
        def run_turn(self, request):
            response = super().run_turn(request)
            path = request.workdir / "greeting.txt"
            if self.role == "worker" and path.exists():
                path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
                (request.workdir / "legacy.csv").write_bytes(b"legacy,\x93quote\x94\r\n")
            return response

    calls = []
    args = trial(tmp_path, git_repo_factory)
    plugin = GenericPlugin(
        name="byte-preserving-agent",
        capabilities=Capabilities(True, True, True, True),
        resolve=lambda workspace, args: ExecutionBinding("byte-preservation-v1", {"runtime": "scripted-suite"}),
        adapter_factory=lambda role, **kwargs: CrlfAgent(role, calls, expect_memory=False),
    )
    outcome = await LongHorizonBenchmark(args, plugin=plugin).run()
    report = json.loads(outcome.selection.read_text())
    assert not outcome.internal_failure, report["internal_failure"]
    assert report["internal_accepted"] and report["delivered"]
    assert (args.workdir / "greeting.txt").read_bytes() == b"hello\r\n"
    assert (args.workdir / "legacy.csv").read_bytes() == b"legacy,\x93quote\x94\r\n"


def test_json_artifacts_keep_lossless_patch_strings_valid_utf8(tmp_path):
    from pact_core.artifacts import write_json_atomic

    patch = b"legacy,\x93quote\x94\r\n".decode("utf-8", errors="surrogateescape")
    path = tmp_path / "artifact.json"
    payload = {"schema": "test/patch-bytes/v1", "patch": patch, "unicode": "中文"}
    write_json_atomic(path, payload)
    assert json.loads(path.read_text(encoding="utf-8")) == payload
