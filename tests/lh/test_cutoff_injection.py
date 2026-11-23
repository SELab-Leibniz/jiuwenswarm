"""General suite timing never discovers a task-specific environment cutoff."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from jiuwenswarm.lh import suite
from jiuwenswarm.lh.localization import localization_configuration


class Plugin:
    artifacts = SimpleNamespace(exhausted=RuntimeError, stopped=RuntimeError)

    def __init__(self):
        self.recalled = 0

    async def clarify(self, context, task):
        return {"status": "available"}

    async def recall(self, context, task):
        self.recalled += 1
        return "evidence"

    async def generate_pact_input(self, context, result, task, *, hints):
        return suite.original_task_pair(task)


def context(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "file.txt").write_text("unchanged")
    return SimpleNamespace(snapshot=SimpleNamespace(roots=(SimpleNamespace(view_root=str(root)),)))


def test_legacy_recall_cutoff_and_graph_dispatch(tmp_path, monkeypatch):
    calls = []

    async def localize(_plugin, _context, _task, _identity, *, cutoff_at_unix, clock):
        calls.append((cutoff_at_unix, clock()))
        return {"status": "available", "text": "", "artifacts": []}

    monkeypatch.setattr(suite, "localize_suite", localize)
    plugin = Plugin()
    ctx = context(tmp_path)
    _, ordinary = asyncio.run(suite.prepare_suite(plugin, ctx, "task", clock=lambda: 100))
    assert ordinary["memory"]["status"] == "available"
    assert calls == [(None, 100)]
    calls.clear()
    _, bounded = asyncio.run(
        suite.prepare_suite(plugin, ctx, "task", cutoff_at_unix=200, clock=lambda: 100, optional_seconds=1)
    )
    assert bounded["localization"]["status"] == "disabled_by_human"
    assert calls == []
    assert plugin.recalled == 2


def test_expired_cutoff_propagates_recall_timeout(tmp_path):
    plugin = Plugin()
    with pytest.raises(TimeoutError):
        asyncio.run(suite.prepare_suite(plugin, context(tmp_path), "task", cutoff_at_unix=99, clock=lambda: 100))


def test_bounded_path_forwards_explicit_clock(tmp_path, monkeypatch):
    calls = []

    async def localize(_plugin, _context, _task, _identity, *, cutoff_at_unix, clock):
        calls.append((cutoff_at_unix, clock()))
        return {"status": "available", "text": "", "artifacts": []}

    monkeypatch.setattr(suite, "localize_suite", localize)
    plugin = Plugin()
    plugin.degraded_clarification = lambda reason: {"status": "degraded"}
    _, result = asyncio.run(
        suite.prepare_suite(
            plugin, context(tmp_path), "task", clock=lambda: 100, optional_seconds=1, planning_deadline=120
        )
    )
    assert result["memory"]["status"] == "available"
    assert calls == [(None, 100)]


def test_localization_identity_includes_cutoff():
    assert localization_configuration() == localization_configuration(cutoff_at_unix=None)
    assert localization_configuration(cutoff_at_unix=200) != localization_configuration(cutoff_at_unix=201)
    with pytest.raises(Exception, match="cutoff_at_unix"):
        localization_configuration(cutoff_at_unix=float("nan"))
