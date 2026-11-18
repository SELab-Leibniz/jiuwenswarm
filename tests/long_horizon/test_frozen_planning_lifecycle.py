"""Frozen evidence reuse and cancellation contracts for concurrent proposals."""

from __future__ import annotations

import asyncio
import shutil
from types import SimpleNamespace

import pytest

from jiuwenswarm.long_horizon.clarification import service
from jiuwenswarm.long_horizon.clarification import evidence
from jiuwenswarm.long_horizon.clarification.budget import SideCallIntegrityError
from jiuwenswarm.long_horizon.clarification.types import RequirementRevision


def test_integrity_failure_cancels_and_joins_every_started_proposal(monkeypatch):
    async def exercise():
        started = set()
        joined = set()
        all_started = asyncio.Event()

        class Model:
            async def propose(self, prompt, *, sample_index):
                started.add(sample_index)
                if len(started) == 3:
                    all_started.set()
                try:
                    await all_started.wait()
                    if sample_index == 2:
                        raise SideCallIntegrityError("frozen source changed")
                    await asyncio.Event().wait()
                finally:
                    joined.add(sample_index)

        monkeypatch.setattr(service, "collect_base_evidence", lambda *_: pytest.fail("re-read frozen evidence"))
        with pytest.raises(SideCallIntegrityError, match="frozen source changed"):
            await service.ClarificationService(Model()).clarify(
                revision=RequirementRevision(number=1, messages=("Inspect public source",)),
                background="", snapshot=SimpleNamespace(), base_evidence="already frozen",
            )
        assert joined == started == {1, 2, 3}

    asyncio.run(exercise())


def test_non_git_evidence_ignores_rg_config_and_sorts_before_truncation(tmp_path, monkeypatch):
    rg = shutil.which("rg")
    if rg is None:
        pytest.skip("ripgrep is required for the real frozen evidence search")
    view = tmp_path / "view"
    view.mkdir()
    for name in ["z", "y", "x", "w", "v", "u", "a"]:
        (view / f"{name}.txt").write_text("needle\n")
    config = tmp_path / "rg.conf"
    config.write_text("--glob=!*.txt\n")
    monkeypatch.setenv("RIPGREP_CONFIG_PATH", str(config))
    monkeypatch.setattr(evidence, "find_rg", lambda: rg)
    snapshot = SimpleNamespace(roots=(SimpleNamespace(
        source_root=str(view), view_root=str(view), label="public", git_head="",
    ),))
    packet = evidence.collect_base_evidence(snapshot, "needle")
    assert "a.txt:1:needle" in packet
    assert "z.txt:1:needle" not in packet
    assert packet.index("a.txt:1:needle") < packet.index("u.txt:1:needle")


def test_external_cancellation_joins_every_started_proposal():
    async def exercise():
        started = set()
        joined = set()
        all_started = asyncio.Event()

        class Model:
            async def propose(self, prompt, *, sample_index):
                started.add(sample_index)
                if len(started) == 3:
                    all_started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    joined.add(sample_index)

        task = asyncio.create_task(service.ClarificationService(Model()).clarify(
            revision=RequirementRevision(number=1, messages=("Inspect public source",)),
            background="", snapshot=SimpleNamespace(), base_evidence="",
        ))
        await all_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert joined == started == {1, 2, 3}

    asyncio.run(exercise())


def test_out_of_order_failures_keep_original_sample_identity_and_order():
    async def exercise():
        events = {i: asyncio.Event() for i in (1, 2, 3)}
        completion_order = []

        class Model:
            async def propose(self, prompt, *, sample_index):
                if sample_index < 3:
                    await events[sample_index + 1].wait()
                completion_order.append(sample_index)
                events[sample_index].set()
                raise ValueError(f"sample-{sample_index}")

        result = await service.ClarificationService(Model()).clarify(
            revision=RequirementRevision(number=1, messages=("Inspect public source",)),
            background="", snapshot=SimpleNamespace(), base_evidence="already frozen",
        )
        assert completion_order == [3, 2, 1]
        assert result.status == "degraded"
        assert [item.sample_index for item in result.investigations] == [1, 2, 3]
        assert [f"sample-{index}" in warning for index, warning in enumerate(result.warnings, 1)] == [True] * 3

    asyncio.run(exercise())
