# Copyright (c) 2026 Chrys. All rights reserved.

"""Cgroup v2 memory sampling, reserves and checksummed watchdog checkpoints."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from jiuwenswarm.lh.memory_guard import (
    CHECKPOINT_SCHEMA,
    memory_budget,
    memory_budget_from_environment,
    read_cgroup_memory,
    read_memory_checkpoint,
    reserve_bytes_for,
    write_memory_checkpoint,
)

MIB = 1024 * 1024
GIB = 1024 * MIB


def _cgroup(root: Path, *, maximum: str = str(16 * GIB), current: str = str(15 * GIB)) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "memory.max").write_text(maximum + "\n")
    (root / "memory.current").write_text(current + "\n")
    (root / "memory.stat").write_text(
        "anon 0\nfile 0\nfile_dirty 0\nfile_writeback 0\ninactive_file 0\nshmem 0\n"
    )
    return root


def _stat(root: Path, *, file: int, dirty: int = 0, writeback: int = 0, anon: int = 0) -> None:
    (root / "memory.stat").write_text(
        f"anon {anon}\nfile {file}\nfile_dirty {dirty}\nfile_writeback {writeback}\n"
        f"inactive_file {file}\nshmem 0\n"
    )


def _proc(path: Path, relative: str = "/") -> Path:
    path.write_text("0::" + relative + "\n")
    return path


def test_cgroup_v2_limit_current_reserve_and_pressure(tmp_path):
    root = _cgroup(tmp_path / "cgroup")
    budget = memory_budget(cgroup_root=root, proc_file=_proc(tmp_path / "self.cgroup"), environ={})
    assert budget.limit_bytes == 16 * GIB
    assert budget.current_bytes == 15 * GIB
    assert budget.reserve_bytes == reserve_bytes_for(16 * GIB)
    assert budget.threshold_bytes == 16 * GIB - budget.reserve_bytes
    assert budget.at_pressure()
    assert budget.observe().current_bytes == 15 * GIB
    snapshot = budget.snapshot()
    assert snapshot["schema"] == "long-horizon/memory-budget/v1"
    assert snapshot["at_pressure"] is True


def test_reclaimable_file_cache_is_not_pressure(tmp_path):
    root = _cgroup(tmp_path / "cgroup", current="17153597440")
    _stat(root, file=16913727488, anon=105517056)
    budget = memory_budget(cgroup_root=root, proc_file=_proc(tmp_path / "self.cgroup"), environ={})
    observed = budget.observe()
    assert observed.current_bytes == 17153597440
    assert observed.effective_bytes == 17153597440 - 16913727488
    assert observed.usage_basis == "memory.stat"
    assert not observed.at_pressure()
    snapshot = observed.snapshot()
    assert snapshot["current_bytes"] == 17153597440
    assert snapshot["effective_bytes"] == 17153597440 - 16913727488
    assert snapshot["usage_basis"] == "memory.stat"
    assert snapshot["at_pressure"] is False


def test_anon_near_the_limit_still_pressures_and_counts_dirty_pages(tmp_path):
    root = _cgroup(tmp_path / "cgroup")
    _stat(root, file=1 * GIB, dirty=128 * MIB, writeback=64 * MIB, anon=14 * GIB)
    budget = memory_budget(cgroup_root=root, proc_file=_proc(tmp_path / "self.cgroup"), environ={})
    observed = budget.observe()
    assert observed.effective_bytes == 15 * GIB - 1 * GIB + 128 * MIB + 64 * MIB
    assert observed.usage_basis == "memory.stat"
    assert observed.at_pressure()
    assert observed.snapshot()["at_pressure"] is True


def test_missing_memory_stat_falls_back_to_current_usage(tmp_path):
    root = _cgroup(tmp_path / "cgroup")
    (root / "memory.stat").unlink()
    budget = memory_budget(cgroup_root=root, proc_file=_proc(tmp_path / "self.cgroup"), environ={})
    observed = budget.observe()
    assert observed.current_bytes == 15 * GIB
    assert observed.effective_bytes == 15 * GIB
    assert observed.usage_basis == "memory.current"
    assert observed.at_pressure()
    snapshot = observed.snapshot()
    assert snapshot["current_bytes"] == 15 * GIB
    assert snapshot["effective_bytes"] == 15 * GIB
    assert snapshot["usage_basis"] == "memory.current"
    assert snapshot["at_pressure"] is True


def test_missing_or_unlimited_cgroup_degrades_without_errors(tmp_path):
    missing = tmp_path / "missing"
    missing_proc = tmp_path / "missing.cgroup"
    budget = memory_budget(cgroup_root=missing, proc_file=missing_proc, environ={})
    assert budget.limit_bytes is None and budget.current_bytes is None
    assert not budget.at_pressure()
    assert budget.threshold_bytes is None

    unlimited = _cgroup(tmp_path / "unlimited", maximum="max", current="1024")
    assert read_cgroup_memory(unlimited) == (None, 1024)
    degraded = memory_budget(
        cgroup_root=unlimited, proc_file=_proc(tmp_path / "unlimited.cgroup"), environ={}
    )
    assert degraded.limit_bytes is None and not degraded.at_pressure()


def test_environment_budget_is_fixed_but_current_is_resampled(tmp_path):
    root = _cgroup(tmp_path / "cgroup", current=str(1 * GIB))
    environment = {
        "LONG_HORIZON_MEMORY_MAX_BYTES": str(16 * GIB),
        "LONG_HORIZON_MEMORY_RESERVE_BYTES": str(2 * GIB),
        "LONG_HORIZON_MEMORY_CGROUP": str(root),
    }
    budget = memory_budget_from_environment(environment)
    assert budget.limit_bytes == 16 * GIB
    assert budget.threshold_bytes == 14 * GIB
    assert not budget.at_pressure()

    (root / "memory.current").write_text(str(15 * GIB))
    assert budget.observe().current_bytes == 15 * GIB
    assert budget.observe().at_pressure()
    empty = memory_budget_from_environment({})
    assert empty.limit_bytes is None and not empty.at_pressure()
    # The general observer honours an explicitly exported cgroup path too.
    explicit = memory_budget(environ=environment)
    assert explicit.cgroup == root and explicit.observe().current_bytes == 15 * GIB


def test_checkpoint_round_trip_and_tamper_refusal(tmp_path):
    path = tmp_path / "memory-checkpoint.json"
    payload = {"reason": "memory_pressure", "work_id": "w", "current_bytes": 123, "threshold_bytes": 100}
    write_memory_checkpoint(path, payload)
    loaded = json.loads(path.read_text())
    assert loaded["schema"] == CHECKPOINT_SCHEMA and loaded["sha256"]
    assert read_memory_checkpoint(path)["work_id"] == "w"

    loaded["work_id"] = "different"
    path.write_text(json.dumps(loaded, sort_keys=True) + "\n")
    with pytest.raises(ValueError):
        read_memory_checkpoint(path)


def _ancestor_cgroup(root: Path, relative: str, *, maximum: int, current: int | None = None) -> Path:
    path = root / relative.strip("/")
    path.mkdir(parents=True, exist_ok=True)
    (path / "memory.max").write_text(str(maximum) + "\n")
    if current is not None:
        (path / "memory.current").write_text(str(current) + "\n")
    return path


def test_cgroup_v2_ancestor_chain_uses_the_smallest_valid_limit(tmp_path):
    root = _cgroup(tmp_path / "cgroup", maximum=str(32 * GIB), current=str(1 * GIB))
    _ancestor_cgroup(root, "team", maximum=16 * GIB)
    leaf = _ancestor_cgroup(root, "team/task", maximum=8 * GIB, current=1 * GIB)
    proc = _proc(tmp_path / "self.cgroup", "/team/task")
    budget = memory_budget(cgroup_root=root, proc_file=proc, environ={})
    assert budget.limit_bytes == 8 * GIB
    assert budget.limit_cgroup == leaf
    assert budget.current_bytes == 1 * GIB
    assert budget.degraded is False and budget.degraded_reason is None
    snapshot = budget.snapshot()
    assert snapshot["limit_cgroup"] == str(leaf)
    assert snapshot["degraded"] is False
    assert [row["cgroup"] for row in snapshot["ancestry"]] == [
        str(leaf),
        str(root / "team"),
        str(root),
    ]
    assert [row["memory_max_bytes"] for row in snapshot["ancestry"]] == [
        8 * GIB,
        16 * GIB,
        32 * GIB,
    ]


def test_cgroup_v1_and_unreadable_files_are_explicitly_degraded(tmp_path):
    v1_proc = tmp_path / "v1.cgroup"
    v1_proc.write_text("3:memory:/task\n1:name=systemd:/user.slice\n")
    v1 = memory_budget(cgroup_root=tmp_path / "missing", proc_file=v1_proc, environ={})
    assert v1.limit_bytes is None
    assert v1.degraded is True and v1.degraded_reason == "cgroup_v1_unsupported"
    assert v1.snapshot()["degraded"] is True

    root = _cgroup(tmp_path / "cgroup", maximum=str(16 * GIB), current=str(1 * GIB))
    (root / "memory.current").unlink()
    unreadable = memory_budget(cgroup_root=root, proc_file=_proc(tmp_path / "self.cgroup"), environ={})
    assert unreadable.limit_bytes == 16 * GIB
    assert unreadable.current_bytes is None
    assert unreadable.degraded is True
    assert unreadable.degraded_reason == "memory_current_unavailable"
    assert unreadable.snapshot()["degraded_reason"] == "memory_current_unavailable"


def test_explicit_environment_limit_still_reports_a_degraded_cgroup(tmp_path):
    proc = tmp_path / "v1.cgroup"
    proc.write_text("3:memory:/task\n")
    budget = memory_budget(
        cgroup_root=tmp_path / "missing",
        proc_file=proc,
        environ={"LONG_HORIZON_MEMORY_MAX_BYTES": str(16 * GIB)},
    )
    assert budget.limit_bytes == 16 * GIB
    assert budget.degraded is True
    assert budget.degraded_reason == "cgroup_v1_unsupported"


def test_degraded_budget_with_numeric_usage_is_not_pressure(tmp_path):
    root = _cgroup(tmp_path / "degraded", maximum="max", current=str(15 * GIB))
    _stat(root, file=0)
    budget = memory_budget_from_environment(
        {
            "LONG_HORIZON_MEMORY_MAX_BYTES": str(16 * GIB),
            "LONG_HORIZON_MEMORY_CGROUP": str(root),
        }
    )
    observed = budget.observe()
    assert observed.degraded is True
    assert observed.degraded_reason == "memory_max_unavailable"
    assert observed.limit_bytes == 16 * GIB
    assert observed.effective_bytes == 15 * GIB
    assert observed.threshold_bytes == 14 * GIB
    assert observed.at_pressure() is False
    snapshot = observed.snapshot()
    assert snapshot["degraded"] is True
    assert snapshot["effective_bytes"] == 15 * GIB
    assert snapshot["threshold_bytes"] == 14 * GIB
    assert snapshot["at_pressure"] is False
    assert snapshot["pressure_assessable"] is False


def test_pressure_requires_effective_usage_not_a_current_fallback():
    from jiuwenswarm.lh.memory_guard import MemoryBudget

    budget = MemoryBudget(
        limit_bytes=16 * GIB,
        reserve_bytes=2 * GIB,
        current_bytes=15 * GIB,
        effective_bytes=None,
    )
    assert budget.threshold_bytes == 14 * GIB
    assert budget.usage_bytes == 15 * GIB
    assert budget.at_pressure() is False
    assert budget.snapshot()["at_pressure"] is False
    assert budget.snapshot()["pressure_assessable"] is False


def test_degraded_budget_never_pressures_even_with_complete_numbers():
    from jiuwenswarm.lh.memory_guard import MemoryBudget

    budget = MemoryBudget(
        limit_bytes=16 * GIB,
        reserve_bytes=2 * GIB,
        current_bytes=15 * GIB,
        effective_bytes=15 * GIB,
        degraded=True,
        degraded_reason="oom_group_kills_parent",
    )
    assert budget.at_pressure() is False
    assert budget.snapshot()["at_pressure"] is False
    assert budget.snapshot()["pressure_assessable"] is False


def test_oom_priority_helper_writes_high_child_score_and_degrades_safely(tmp_path):
    from jiuwenswarm.lh.memory_guard import lower_parent_oom_score, prioritize_child_oom, read_oom_score_adj

    proc = tmp_path / "proc"
    child = proc / "4321"
    child.mkdir(parents=True)
    (child / "oom_score_adj").write_text("0\n")
    record = prioritize_child_oom(4321, proc_root=proc)
    assert record["status"] == "applied" and record["requested"] == 800
    assert read_oom_score_adj(4321, proc_root=proc) == 800
    assert (child / "oom_score_adj").read_text().strip() == "800"

    missing = prioritize_child_oom(9999, proc_root=proc)
    assert missing["status"] == "degraded"
    assert missing["reason"] == "FileNotFoundError"
    assert missing["requested"] == 800

    parent = proc / "123"
    parent.mkdir()
    (parent / "oom_score_adj").write_text("500\n")
    lowered = lower_parent_oom_score(pid=123, proc_root=proc)
    assert lowered["status"] == "applied" and lowered["observed"] == 0
    assert read_oom_score_adj(123, proc_root=proc) == 0
    assert (parent / "oom_score_adj").read_text().strip() == "0"

    ignored = lower_parent_oom_score(pid=9999, proc_root=proc)
    assert ignored["status"] == "degraded"


def test_pressure_wait_clears_or_refuses_before_a_build_spawn(tmp_path):
    from jiuwenswarm.lh.memory_guard import wait_for_memory_headroom

    root = _cgroup(tmp_path / "cgroup", maximum=str(16 * GIB), current=str(15 * GIB))
    budget = memory_budget(cgroup_root=root, proc_file=_proc(tmp_path / "self.cgroup"), environ={})
    sleeps = []

    def clearing_sleep(seconds):
        sleeps.append(seconds)
        (root / "memory.current").write_text(str(1 * GIB))

    cleared = wait_for_memory_headroom(budget, wait_seconds=1.0, poll_seconds=0.01, sleep=clearing_sleep)
    assert cleared["status"] == "cleared"
    assert cleared["waited_seconds"] >= 0 and sleeps

    (root / "memory.current").write_text(str(15 * GIB))
    refused = wait_for_memory_headroom(
        budget, wait_seconds=0.02, poll_seconds=0.005, sleep=lambda seconds: sleeps.append(seconds)
    )
    assert refused["status"] == "refused"
    assert refused["reason"] == "memory_pressure"
    assert refused["snapshot"]["at_pressure"] is True
    assert len(refused["samples"]) >= 2


def test_observe_reads_usage_from_the_limit_cgroup_so_siblings_count(tmp_path):
    root = _cgroup(tmp_path / "cgroup", maximum=str(32 * GIB), current=str(12 * GIB))
    limit = _ancestor_cgroup(root, "team", maximum=8 * GIB, current=10 * GIB)
    _ancestor_cgroup(root, "team/task", maximum=16 * GIB, current=2 * GIB)
    _ancestor_cgroup(root, "team/sibling", maximum=16 * GIB, current=8 * GIB)
    budget = memory_budget(
        cgroup_root=root,
        proc_file=_proc(tmp_path / "self.cgroup", "/team/task"),
        environ={},
    )
    assert budget.limit_cgroup == limit
    observed = budget.observe()
    assert observed.limit_cgroup == limit
    assert observed.current_bytes == 10 * GIB
    assert observed.at_pressure()
    snapshot = observed.snapshot()
    assert snapshot["current_bytes"] == 10 * GIB
    assert snapshot["usage_cgroup"] == str(limit)
    assert snapshot["protected"] is True
    assert snapshot["unprotected"] is False


def test_missing_usage_at_the_limit_cgroup_is_degraded_and_unprotected(tmp_path):
    root = _cgroup(tmp_path / "cgroup", maximum=str(32 * GIB), current=str(12 * GIB))
    limit = _ancestor_cgroup(root, "team", maximum=8 * GIB)
    _ancestor_cgroup(root, "team/task", maximum=16 * GIB, current=2 * GIB)
    budget = memory_budget(
        cgroup_root=root,
        proc_file=_proc(tmp_path / "self.cgroup", "/team/task"),
        environ={},
    )
    observed = budget.observe()
    assert observed.limit_cgroup == limit
    assert observed.current_bytes is None
    assert observed.degraded is True
    assert observed.degraded_reason == "memory_current_unavailable"
    snapshot = observed.snapshot()
    assert snapshot["protected"] is False
    assert snapshot["unprotected"] is True
    assert snapshot["protection"] == "unprotected"


def test_oom_group_is_recorded_for_current_and_limit_cgroups_without_writing(tmp_path):
    root = _cgroup(tmp_path / "cgroup", maximum=str(32 * GIB), current=str(12 * GIB))
    (root / "memory.oom.group").write_text("0\n")
    limit = _ancestor_cgroup(root, "team", maximum=8 * GIB, current=10 * GIB)
    (limit / "memory.oom.group").write_text("1\n")
    leaf = _ancestor_cgroup(root, "team/task", maximum=16 * GIB, current=2 * GIB)
    (leaf / "memory.oom.group").write_text("0\n")
    budget = memory_budget(
        cgroup_root=root,
        proc_file=_proc(tmp_path / "self.cgroup", "/team/task"),
        environ={},
    )
    snapshot = budget.snapshot()
    assert snapshot["oom_group"] == 0
    assert snapshot["limit_cgroup_oom_group"] == 1
    assert snapshot["oom_group_kills_parent"] is True
    assert snapshot["oom_score_adj_protects_parent"] is False
    assert "oom_score_adj" in snapshot["oom_group_detail"]
    assert snapshot["degraded"] is True
    assert snapshot["degraded_reason"] == "oom_group_kills_parent"
    assert (limit / "memory.oom.group").read_text() == "1\n"
    assert (leaf / "memory.oom.group").read_text() == "0\n"


def _fake_process(proc: Path, pid: int, parent: int, *, rss_pages: int, pss_kib: int | None) -> None:
    directory = proc / str(pid)
    directory.mkdir(parents=True)
    fields = ["S", str(parent)] + ["0"] * 19 + [str(rss_pages)]
    (directory / "stat").write_text(f"{pid} (synthetic) " + " ".join(fields) + "\n")
    if pss_kib is not None:
        (directory / "smaps_rollup").write_text(f"Rss: {rss_pages * 4} kB\nPss: {pss_kib} kB\n")


def test_process_tree_memory_prefers_pss_and_falls_back_to_rss(tmp_path):
    from jiuwenswarm.lh.memory_guard import process_tree_memory_bytes

    proc = tmp_path / "proc"
    _fake_process(proc, 100, 1, rss_pages=1000, pss_kib=100)
    _fake_process(proc, 101, 100, rss_pages=2000, pss_kib=50)
    _fake_process(proc, 102, 100, rss_pages=10, pss_kib=None)
    assert process_tree_memory_bytes(100, proc_root=proc) == 150 * 1024 + 10 * os.sysconf("SC_PAGE_SIZE")


def test_role_pressure_stops_the_largest_tree_even_below_the_share_threshold(tmp_path, monkeypatch):
    from jiuwenswarm.lh import memory_guard

    cgroup = tmp_path / "cgroup"
    cgroup.mkdir()
    (cgroup / "cgroup.procs").write_text("100\n200\n")
    sizes = {100: 150 * MIB, 200: 80 * MIB}
    monkeypatch.setattr(memory_guard, "process_tree_memory_bytes", lambda pid, **_kwargs: sizes.get(pid))
    largest = memory_guard.role_pressure_decision(100, 1 * GIB, cgroup=cgroup)
    assert largest["stop"] is True
    assert largest["role_share"] < 0.25
    assert largest["largest_tree"] is True
    assert largest["largest_tree_pid"] == 100
    assert largest["reason"] == "largest_role_tree"

    sizes[200] = 200 * MIB
    smaller = memory_guard.role_pressure_decision(100, 1 * GIB, cgroup=cgroup)
    assert smaller["stop"] is False
    assert smaller["largest_tree"] is False
    assert smaller["largest_tree_pid"] == 200
    assert smaller["reason"] == "cgroup_memory_pressure_other_process"


def test_role_pressure_share_uses_effective_usage_as_its_denominator(tmp_path, monkeypatch):
    from jiuwenswarm.lh import memory_guard

    cgroup = tmp_path / "cgroup"
    cgroup.mkdir()
    (cgroup / "cgroup.procs").write_text("100\n200\n")
    sizes = {100: 200 * MIB, 200: 400 * MIB}
    monkeypatch.setattr(memory_guard, "process_tree_memory_bytes", lambda pid, **_kwargs: sizes.get(pid))
    effective = 14 * GIB
    decision = memory_guard.role_pressure_decision(100, effective, cgroup=cgroup)
    assert decision["role_share"] == 200 * MIB / effective
    assert decision["current_bytes"] == effective
    assert decision["stop"] is False


def test_role_pressure_does_not_guess_largest_without_cgroup_procs(tmp_path, monkeypatch):
    from jiuwenswarm.lh import memory_guard

    cgroup = tmp_path / "cgroup"
    cgroup.mkdir()
    monkeypatch.setattr(memory_guard, "process_tree_memory_bytes", lambda _pid, **_kwargs: 100 * MIB)
    decision = memory_guard.role_pressure_decision(100, 1 * GIB, cgroup=cgroup)
    assert decision["stop"] is False
    assert decision["largest_tree"] is False
    assert decision["largest_tree_pid"] is None
