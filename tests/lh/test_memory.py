# Copyright (c) 2026 Chrys. All rights reserved.

"""Exercise portable memory subprocesses and frozen evidence identities."""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

import pytest

from jiuwenswarm.lh.cli import build_parser, run_command
from jiuwenswarm.lh.memory import CommandMemory
from jiuwenswarm.lh.side_calls import view_identity


def provider(tmp_path, code):
    receipt = tmp_path / "corpus-receipt.json"
    receipt.write_text('{"corpus_commit":"frozen-fixture"}\n')
    return CommandMemory([sys.executable, "-c", code], receipt)


def context(tmp_path):
    return SimpleNamespace(snapshot=SimpleNamespace(roots=(SimpleNamespace(view_root=str(tmp_path)),)))


async def test_memory_command_receives_real_query_and_snapshot_with_receipt(tmp_path):
    command = provider(
        tmp_path,
        "import json,sys; q=json.load(sys.stdin); "
        "assert q['schema']=='long-horizon/memory-query/v1'; "
        "assert len(q['receipt_sha256'])==64; "
        "print(json.dumps({'text':q['requirement']+' '+q['snapshot_root']}))",
    )
    assert await command(context(tmp_path), "repair") == "repair " + str(tmp_path)
    command.receipt.write_text("changed corpus identity")
    with pytest.raises(ValueError, match="receipt changed"):
        await command(context(tmp_path), "repair")


@pytest.mark.parametrize("output", ["[]", '{"text":42}', "x" * 64001])
async def test_memory_rejects_invalid_or_excessive_subprocess_output(tmp_path, output):
    command = provider(tmp_path, "print(" + repr(output) + ")")
    with pytest.raises(ValueError):
        await command(context(tmp_path), "repair")


async def test_cancel_terminates_and_waits_for_memory_process(tmp_path, monkeypatch):
    command = provider(tmp_path, "import time; time.sleep(30)")
    started, processes = asyncio.Event(), []
    original = asyncio.create_subprocess_exec

    async def capture(*args, **kwargs):
        process = await original(*args, **kwargs)
        processes.append(process)
        started.set()
        return process

    monkeypatch.setattr("jiuwenswarm.lh.memory.asyncio.create_subprocess_exec", capture)
    task = asyncio.create_task(command(context(tmp_path), "repair"))
    await asyncio.wait_for(started.wait(), timeout=10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(processes) == 1 and processes[0].returncode is not None


async def test_invalid_cli_memory_pair_rejected_before_plugin_startup(monkeypatch):
    def forbidden_startup(options):
        raise AssertionError("plugin should not start")

    monkeypatch.setattr("jiuwenswarm.lh.cli.load_plugin", forbidden_startup)
    options = build_parser().parse_args(
        [
            "--task",
            "t.md",
            "--workdir",
            ".",
            "--output",
            "/tmp/o",
            "--task-id",
            "a",
            "--trial-id",
            "b",
            "--memgraph-command",
            "python query.py",
        ]
    )
    with pytest.raises(ValueError, match="frozen corpus receipt"):
        await run_command(options)


def test_snapshot_identity_detects_symlink_directory_without_reading_external_target(tmp_path):
    view, external = tmp_path / "view", tmp_path / "external"
    view.mkdir()
    external.mkdir()
    (external / "private.txt").write_text("outside model view")
    snapshot = context(view).snapshot
    before = view_identity(snapshot)
    try:
        (view / "link").symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable")
    after = view_identity(snapshot)
    assert before != after
    (external / "private.txt").write_text("new external content")
    assert view_identity(snapshot) == after
