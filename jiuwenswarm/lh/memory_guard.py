# Copyright (c) 2026 Chrys. All rights reserved.

"""Bounded controller memory observation for cgroup v2 task containers.

The task container has one hard memory quota shared by the controller, native
agents, builds and checks. This module only reads that quota and current
occupancy. It never writes cgroup state and degrades to "no limit observed"
when the files are absent, unreadable, unlimited or use an unsupported layout.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

MIB = 1024 * 1024
GIB = 1024 * MIB
SCHEMA = "long-horizon/memory-budget/v1"
CHECKPOINT_SCHEMA = "long-horizon/memory-checkpoint/v1"
DEFAULT_RESERVE_FRACTION = 0.15
MIN_RESERVE_BYTES = 512 * MIB
MAX_RESERVE_BYTES = 2 * GIB
ENV_LIMIT = "LONG_HORIZON_MEMORY_MAX_BYTES"
ENV_RESERVE = "LONG_HORIZON_MEMORY_RESERVE_BYTES"
ENV_CGROUP = "LONG_HORIZON_MEMORY_CGROUP"
OOM_PRIORITY_SCHEMA = "long-horizon/oom-priority/v1"
OOM_SCORE_ADJ_CHILD = 800
OOM_SCORE_ADJ_PARENT = 0
ROLE_MEMORY_SHARE_THRESHOLD = 0.25
OOM_GROUP_KILLS_PARENT = "oom_group_kills_parent"
DEFAULT_PRESSURE_WAIT_SECONDS = 2.0
DEFAULT_PRESSURE_POLL_SECONDS = 0.25


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None


def _has_memory_files(path: Path) -> bool:
    return (path / "memory.current").is_file() or (path / "memory.max").is_file()


def _proc_cgroup_layout(proc_file: Path) -> tuple[str | None, bool]:
    """Return the cgroup-v2 relative path and whether a v1 memory hierarchy exists."""
    relative: str | None = None
    v1 = False
    try:
        lines = proc_file.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        lines = []
    for line in lines:
        fields = line.split(":", 2)
        if len(fields) != 3:
            continue
        hierarchy, controllers, path = fields
        if hierarchy == "0" and not controllers:
            relative = path.lstrip("/")
        elif "memory" in controllers.split(","):
            v1 = True
    return relative, v1


def _locate_cgroup(
    *, cgroup_root: Path = Path("/sys/fs/cgroup"), proc_file: Path = Path("/proc/self/cgroup")
) -> tuple[Path | None, str | None]:
    """Locate the cgroup v2 directory and name an explicit degradation reason."""
    relative, v1 = _proc_cgroup_layout(proc_file)
    if relative is not None:
        parts = Path(relative).parts
        if ".." not in parts:
            candidate = cgroup_root / relative
            if _has_memory_files(candidate):
                return candidate, None
    # A private cgroup namespace exposes the container at the mount root.
    if _has_memory_files(cgroup_root):
        return cgroup_root, None
    if v1:
        return None, "cgroup_v1_unsupported"
    if relative is not None:
        return None, "cgroup_v2_memory_files_unavailable"
    return None, "cgroup_v2_path_unavailable"


def cgroup_memory_path(
    *, cgroup_root: Path = Path("/sys/fs/cgroup"), proc_file: Path = Path("/proc/self/cgroup")
) -> Path | None:
    """Find this process's cgroup v2 directory, including private namespaces."""
    cgroup, _reason = _locate_cgroup(cgroup_root=cgroup_root, proc_file=proc_file)
    return cgroup


def _ancestor_cgroups(cgroup: Path, cgroup_root: Path) -> tuple[Path, ...]:
    """The leaf-to-root v2 chain, or just the leaf when it is outside the mount."""
    if cgroup == cgroup_root:
        return (cgroup,)
    if cgroup_root in cgroup.parents:
        ancestors: list[Path] = []
        current = cgroup
        while True:
            ancestors.append(current)
            if current == cgroup_root:
                break
            current = current.parent
        return tuple(ancestors)
    return (cgroup,)


def read_cgroup_memory_limit(
    cgroup: Path | None, *, cgroup_root: Path = Path("/sys/fs/cgroup")
) -> tuple[int | None, Path | None, list[dict[str, Any]]]:
    """Return the smallest finite memory.max along the v2 ancestor chain.

    The returned rows record every ancestor, including unlimited and unreadable
    entries, so callers can persist why a budget was degraded instead of silently
    falling back to an ancestor or to an exported limit.
    """
    if cgroup is None:
        return None, None, []
    rows: list[dict[str, Any]] = []
    candidates: list[tuple[int, Path]] = []
    for directory in _ancestor_cgroups(cgroup, cgroup_root):
        raw = _read_text(directory / "memory.max")
        if raw is None:
            status, value = "unavailable", None
        elif raw == "max":
            status, value = "unlimited", None
        else:
            value = _positive_int(raw)
            status = "limited" if value is not None else "invalid"
        rows.append(
            {
                "cgroup": str(directory),
                "memory_max": raw,
                "memory_max_bytes": value,
                "status": status,
            }
        )
        if value is not None:
            candidates.append((value, directory))
    if not candidates:
        return None, None, rows
    limit, source = min(candidates, key=lambda item: item[0])
    return limit, source, rows


def read_cgroup_memory_current(cgroup: Path | None) -> int | None:
    """Read one cgroup's ``memory.current``; zero is a valid reading."""
    if cgroup is None:
        return None
    raw = _read_text(cgroup / "memory.current")
    if raw == "0":
        return 0
    return _positive_int(raw)


def read_cgroup_memory_stat(cgroup: Path | None) -> dict[str, int] | None:
    """Read the ``memory.stat`` counters needed to discount reclaimable page cache.

    ``file`` is required; a stat without it is unavailable. Missing dirty and
    write-back counters default to zero so a minimal stat can still be used.
    """
    if cgroup is None:
        return None
    raw = _read_text(cgroup / "memory.stat")
    if raw is None:
        return None
    names = ("file", "file_dirty", "file_writeback")
    values: dict[str, int] = {}
    for line in raw.splitlines():
        fields = line.split()
        if len(fields) != 2 or fields[0] not in names:
            continue
        try:
            number = int(fields[1])
        except ValueError:
            return None
        if number < 0:
            return None
        values[fields[0]] = number
    if "file" not in values:
        return None
    values.setdefault("file_dirty", 0)
    values.setdefault("file_writeback", 0)
    return values


def read_cgroup_memory_usage(cgroup: Path | None) -> tuple[int | None, int | None, str]:
    """Return ``(current, effective, usage_basis)`` for one cgroup.

    ``effective`` subtracts clean file-backed page cache, which the kernel can
    reclaim, and adds back dirty or in-flight pages, which it cannot. When
    ``memory.stat`` is unavailable the raw current reading is the fallback and
    the basis says so explicitly.
    """
    current = read_cgroup_memory_current(cgroup)
    if current is None:
        return None, None, "unavailable"
    counters = read_cgroup_memory_stat(cgroup)
    if counters is None:
        return current, current, "memory.current"
    effective = max(
        0,
        current
        - counters["file"]
        + counters["file_dirty"]
        + counters["file_writeback"],
    )
    return current, effective, "memory.stat"


def read_cgroup_memory(cgroup: Path | None) -> tuple[int | None, int | None]:
    """Return ``(memory.max, memory.current)``; unlimited or unreadable values are ``None``."""
    if cgroup is None:
        return None, None
    maximum_raw = _read_text(cgroup / "memory.max")
    maximum = None
    if maximum_raw is not None and maximum_raw != "max":
        maximum = _positive_int(maximum_raw)
    return maximum, read_cgroup_memory_current(cgroup)


def read_cgroup_oom_group(cgroup: Path | None) -> dict[str, Any] | None:
    """Read ``memory.oom.group`` for evidence only; never write cgroup state."""
    if cgroup is None:
        return None
    raw = _read_text(cgroup / "memory.oom.group")
    if raw is None:
        status, value = "unavailable", None
    elif raw in ("0", "1"):
        value = int(raw)
        status = "configured"
    else:
        value = None
        status = "invalid"
    return {
        "cgroup": str(cgroup),
        "memory_oom_group": raw,
        "value": value,
        "status": status,
    }


def read_cgroup_procs(cgroup: Path | None) -> tuple[int, ...] | None:
    """Read the pids directly assigned to one cgroup, or ``None`` when unavailable."""
    if cgroup is None:
        return None
    raw = _read_text(cgroup / "cgroup.procs")
    if raw is None:
        return None
    pids: list[int] = []
    for token in raw.split():
        try:
            pid = int(token)
        except ValueError:
            continue
        if pid > 0:
            pids.append(pid)
    return tuple(pids)


def reserve_bytes_for(limit_bytes: int | None) -> int:
    """Memory held back for controller checkpoints, builds, export and delivery."""
    if limit_bytes is None or limit_bytes <= 0:
        return 0
    proportional = int(limit_bytes * DEFAULT_RESERVE_FRACTION)
    return max(MIN_RESERVE_BYTES, min(MAX_RESERVE_BYTES, proportional))


@dataclass(frozen=True)
class MemoryBudget:
    limit_bytes: int | None
    reserve_bytes: int = 0
    cgroup: Path | None = None
    current_bytes: int | None = None
    observed_at_unix: float = 0.0
    degraded: bool = False
    degraded_reason: str | None = None
    limit_cgroup: Path | None = None
    cgroup_root: Path | None = None
    ancestry: tuple[dict[str, Any], ...] = ()
    usage_cgroup: Path | None = None
    oom_group: int | None = None
    limit_cgroup_oom_group: int | None = None
    oom_group_records: tuple[dict[str, Any], ...] = ()
    effective_bytes: int | None = None
    usage_basis: str | None = None

    @property
    def threshold_bytes(self) -> int | None:
        if self.limit_bytes is None:
            return None
        return max(0, self.limit_bytes - max(0, self.reserve_bytes))

    @property
    def usage_bytes(self) -> int | None:
        """Preferred non-reclaimable usage for role attribution.

        Pressure decisions use :attr:`effective_bytes` directly.  This property
        retains the raw-current fallback for callers that only attribute a role
        share; it is not sufficient evidence of pressure on its own.
        """
        return self.effective_bytes if self.effective_bytes is not None else self.current_bytes

    def observe(self) -> MemoryBudget:
        if self.cgroup is None:
            reason = self.degraded_reason or ("memory_max_unavailable" if self.limit_bytes is None else None)
            return replace(
                self,
                observed_at_unix=time.time(),
                degraded=self.degraded or reason is not None,
                degraded_reason=reason,
                usage_cgroup=self.usage_cgroup,
            )
        root = self.cgroup_root or Path("/sys/fs/cgroup")
        maximum, source, ancestry = read_cgroup_memory_limit(self.cgroup, cgroup_root=root)
        # Usage must be sampled at the same level as the binding limit. Reading
        # the leaf while the limit sits on an ancestor hides sibling pressure.
        usage_cgroup = source if source is not None else self.cgroup
        current, effective, usage_basis = read_cgroup_memory_usage(usage_cgroup)
        if maximum is None:
            limit = self.limit_bytes
        elif self.limit_bytes is None:
            limit = maximum
        else:
            limit = min(self.limit_bytes, maximum)
        degraded_reason = self.degraded_reason
        if maximum is None:
            degraded_reason = degraded_reason or "memory_max_unavailable"
        elif any(row["status"] in ("unavailable", "invalid") for row in ancestry):
            degraded_reason = degraded_reason or "ancestor_memory_max_unavailable"
        if current is None:
            degraded_reason = "memory_current_unavailable"

        current_group = read_cgroup_oom_group(self.cgroup)
        limit_group = read_cgroup_oom_group(source)
        oom_records: list[dict[str, Any]] = []
        if current_group is not None:
            oom_records.append({**current_group, "role": "current"})
        if limit_group is not None:
            oom_records.append({**limit_group, "role": "limit"})
        oom_group = current_group["value"] if current_group is not None else self.oom_group
        limit_oom_group = limit_group["value"] if limit_group is not None else self.limit_cgroup_oom_group
        if oom_group == 1 or limit_oom_group == 1:
            # A group OOM ignores oom_score_adj: the whole cgroup dies, including
            # the controller parent, so this observation is explicitly degraded.
            degraded_reason = OOM_GROUP_KILLS_PARENT
        return replace(
            self,
            limit_bytes=limit,
            current_bytes=current,
            effective_bytes=effective,
            usage_basis=usage_basis,
            observed_at_unix=time.time(),
            degraded=degraded_reason is not None,
            degraded_reason=degraded_reason,
            limit_cgroup=source,
            ancestry=tuple(ancestry),
            usage_cgroup=usage_cgroup,
            oom_group=oom_group,
            limit_cgroup_oom_group=limit_oom_group,
            oom_group_records=tuple(oom_records),
        )

    def pressure_state(self) -> dict[str, Any]:
        """Return whether pressure can be judged and, if so, whether it is reached.

        A degraded budget, a missing/invalid limit, a missing threshold or a
        missing effective usage is unobservable.  Unobservable budgets are never
        pressure; callers must record the degradation and keep running.
        """
        threshold = self.threshold_bytes
        effective = self.effective_bytes
        if self.limit_bytes is None or self.limit_bytes <= 0:
            reason = self.degraded_reason or "memory_limit_unavailable"
        elif self.degraded:
            reason = self.degraded_reason or "memory_budget_degraded"
        elif threshold is None:
            reason = "memory_threshold_unavailable"
        elif effective is None:
            reason = "memory_effective_usage_unavailable"
        else:
            return {
                "assessable": True,
                "at_pressure": effective >= threshold,
                "reason": None,
                "limit_bytes": self.limit_bytes,
                "effective_bytes": effective,
                "threshold_bytes": threshold,
            }
        return {
            "assessable": False,
            "at_pressure": False,
            "reason": reason,
            "limit_bytes": self.limit_bytes,
            "effective_bytes": effective,
            "threshold_bytes": threshold,
        }

    def _pressure(self) -> bool:
        return self.pressure_state()["at_pressure"]

    def at_pressure(self) -> bool:
        return self.observe()._pressure()

    def snapshot(self) -> dict[str, Any]:
        observed = self.observe()
        effective = observed.effective_bytes if observed.effective_bytes is not None else observed.current_bytes
        usage_basis = observed.usage_basis or (
            "memory.current" if observed.current_bytes is not None else "unavailable"
        )
        oom_group_kills_parent = observed.oom_group == 1 or observed.limit_cgroup_oom_group == 1
        pressure = observed.pressure_state()
        return {
            "schema": SCHEMA,
            "cgroup": str(observed.cgroup) if observed.cgroup is not None else None,
            "usage_cgroup": str(observed.usage_cgroup) if observed.usage_cgroup is not None else None,
            "limit_bytes": observed.limit_bytes,
            "current_bytes": observed.current_bytes,
            "effective_bytes": effective,
            "usage_basis": usage_basis,
            "reserve_bytes": observed.reserve_bytes,
            "threshold_bytes": observed.threshold_bytes,
            "limit_cgroup": str(observed.limit_cgroup) if observed.limit_cgroup is not None else None,
            "oom_group": observed.oom_group,
            "limit_cgroup_oom_group": observed.limit_cgroup_oom_group,
            "oom_group_records": list(observed.oom_group_records),
            "oom_group_kills_parent": oom_group_kills_parent,
            "oom_score_adj_protects_parent": not oom_group_kills_parent,
            "oom_group_detail": (
                "memory.oom.group=1: a group OOM kills every process in the cgroup, "
                "so oom_score_adj cannot protect the controller parent"
                if oom_group_kills_parent
                else None
            ),
            "degraded": observed.degraded,
            "degraded_reason": observed.degraded_reason,
            "protected": not observed.degraded,
            "unprotected": observed.degraded,
            "protection": "unprotected" if observed.degraded else "protected",
            "ancestry": list(observed.ancestry),
            "at_pressure": pressure["at_pressure"],
            "pressure_assessable": pressure["assessable"],
            "pressure_unavailable_reason": pressure["reason"],
            "observed_at_unix": observed.observed_at_unix or time.time(),
        }

    def environment(self) -> dict[str, str]:
        values: dict[str, str] = {}
        if self.limit_bytes is not None:
            values[ENV_LIMIT] = str(self.limit_bytes)
            values[ENV_RESERVE] = str(max(0, self.reserve_bytes))
        if self.cgroup is not None:
            values[ENV_CGROUP] = str(self.cgroup)
        return values


def _budget_from_values(
    *,
    limit: int | None,
    current: int | None,
    reserve: int | None,
    cgroup: Path | None,
    degraded_reason: str | None = None,
    limit_cgroup: Path | None = None,
    cgroup_root: Path | None = None,
    ancestry: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
    usage_cgroup: Path | None = None,
    effective: int | None = None,
    usage_basis: str | None = None,
) -> MemoryBudget:
    if limit is None:
        return MemoryBudget(
            limit_bytes=None,
            reserve_bytes=0,
            cgroup=cgroup,
            current_bytes=current,
            observed_at_unix=time.time(),
            degraded=degraded_reason is not None,
            degraded_reason=degraded_reason,
            limit_cgroup=limit_cgroup,
            cgroup_root=cgroup_root,
            ancestry=tuple(ancestry),
            usage_cgroup=usage_cgroup,
            effective_bytes=effective,
            usage_basis=usage_basis,
        )
    return MemoryBudget(
        limit_bytes=limit,
        reserve_bytes=reserve_bytes_for(limit) if reserve is None else max(0, reserve),
        cgroup=cgroup,
        current_bytes=current,
        observed_at_unix=time.time(),
        degraded=degraded_reason is not None,
        degraded_reason=degraded_reason,
        limit_cgroup=limit_cgroup,
        cgroup_root=cgroup_root,
        ancestry=tuple(ancestry),
        usage_cgroup=usage_cgroup,
        effective_bytes=effective,
        usage_basis=usage_basis,
    )


def memory_budget(
    *,
    cgroup_root: Path = Path("/sys/fs/cgroup"),
    proc_file: Path = Path("/proc/self/cgroup"),
    environ: Mapping[str, str] | None = None,
    reserve_bytes: int | None = None,
) -> MemoryBudget:
    """Observe the current cgroup once; never raise when evidence is unavailable."""
    environment = os.environ if environ is None else environ
    raw_cgroup = environment.get(ENV_CGROUP)
    if raw_cgroup:
        cgroup = Path(raw_cgroup)
        degraded_reason = None
    else:
        cgroup, degraded_reason = _locate_cgroup(cgroup_root=cgroup_root, proc_file=proc_file)
    maximum, limit_cgroup, ancestry = read_cgroup_memory_limit(cgroup, cgroup_root=cgroup_root)
    usage_cgroup = limit_cgroup if limit_cgroup is not None else cgroup
    current, effective, usage_basis = read_cgroup_memory_usage(usage_cgroup)
    exported_limit = _positive_int(environment.get(ENV_LIMIT))
    if exported_limit is not None and maximum is not None:
        limit = min(exported_limit, maximum)
    elif exported_limit is not None:
        limit = exported_limit
    else:
        limit = maximum
    if degraded_reason is None:
        if cgroup is not None and maximum is None:
            degraded_reason = "memory_max_unavailable"
        elif limit is None:
            degraded_reason = "memory_max_unavailable"
        elif any(row["status"] in ("unavailable", "invalid") for row in ancestry):
            degraded_reason = "ancestor_memory_max_unavailable"
        elif current is None and cgroup is not None:
            degraded_reason = "memory_current_unavailable"
    reserve = reserve_bytes if reserve_bytes is not None else _positive_int(environment.get(ENV_RESERVE))
    return _budget_from_values(
        limit=limit,
        current=current,
        reserve=reserve,
        cgroup=cgroup,
        degraded_reason=degraded_reason,
        limit_cgroup=limit_cgroup,
        cgroup_root=cgroup_root,
        ancestry=ancestry,
        usage_cgroup=usage_cgroup,
        effective=effective,
        usage_basis=usage_basis,
    )


def memory_budget_from_environment(environ: Mapping[str, str] | None = None) -> MemoryBudget:
    """Read only the budget exported by ``run_lh``; absence means no watchdog."""
    environment = os.environ if environ is None else environ
    cgroup: Path | None = None
    raw_path = environment.get(ENV_CGROUP)
    if raw_path:
        try:
            cgroup = Path(raw_path)
        except (OSError, ValueError, TypeError):
            cgroup = None
    exported_limit = _positive_int(environment.get(ENV_LIMIT))
    reserve = _positive_int(environment.get(ENV_RESERVE))
    maximum, limit_cgroup, ancestry = read_cgroup_memory_limit(cgroup)
    usage_cgroup = limit_cgroup if limit_cgroup is not None else cgroup
    current, effective, usage_basis = read_cgroup_memory_usage(usage_cgroup)
    if exported_limit is not None and maximum is not None:
        limit = min(exported_limit, maximum)
    else:
        limit = exported_limit if exported_limit is not None else maximum
    degraded_reason = None
    if cgroup is None:
        degraded_reason = "cgroup_budget_not_exported"
    elif maximum is None:
        degraded_reason = "memory_max_unavailable"
    elif limit is None:
        degraded_reason = "memory_max_unavailable"
    elif any(row["status"] in ("unavailable", "invalid") for row in ancestry):
        degraded_reason = "ancestor_memory_max_unavailable"
    elif current is None:
        degraded_reason = "memory_current_unavailable"
    return _budget_from_values(
        limit=limit,
        current=current,
        reserve=reserve,
        cgroup=cgroup,
        degraded_reason=degraded_reason,
        limit_cgroup=limit_cgroup,
        ancestry=ancestry,
        usage_cgroup=usage_cgroup,
        effective=effective,
        usage_basis=usage_basis,
    )


def _checkpoint_digest(body: Mapping[str, Any]) -> str:
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def write_memory_checkpoint(path: Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Persist a checksummed checkpoint before a role child is stopped."""
    body = {
        "schema": CHECKPOINT_SCHEMA,
        "created_at_unix": time.time(),
        **dict(payload),
    }
    value = {**body, "sha256": _checkpoint_digest(body)}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return value


def read_memory_checkpoint(path: Path) -> dict[str, Any]:
    """Validate a checkpoint before any recovery or evidence reuse."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as error:
        raise ValueError("memory checkpoint is unreadable") from error
    if not isinstance(value, dict) or value.get("schema") != CHECKPOINT_SCHEMA:
        raise ValueError("invalid memory checkpoint schema")
    digest = value.pop("sha256", None)
    if not isinstance(digest, str) or digest != _checkpoint_digest(value):
        raise ValueError("memory checkpoint digest mismatch")
    value["sha256"] = digest
    return value


def oom_score_adj_path(pid: int, *, proc_root: Path = Path("/proc")) -> Path:
    return Path(proc_root) / str(int(pid)) / "oom_score_adj"


def read_oom_score_adj(pid: int, *, proc_root: Path = Path("/proc")) -> int | None:
    try:
        return int(oom_score_adj_path(pid, proc_root=proc_root).read_text(encoding="utf-8").strip())
    except (OSError, UnicodeError, ValueError):
        return None


def _write_oom_score_adj(pid: int, value: int, *, proc_root: Path = Path("/proc")) -> dict[str, Any]:
    requested = max(-1000, min(1000, int(value)))
    path = oom_score_adj_path(pid, proc_root=proc_root)
    try:
        path.write_text(f"{requested}\n", encoding="utf-8")
    except (OSError, UnicodeError, ValueError) as error:
        return {
            "schema": OOM_PRIORITY_SCHEMA,
            "pid": int(pid),
            "requested": requested,
            "observed": read_oom_score_adj(pid, proc_root=proc_root),
            "status": "degraded",
            "reason": type(error).__name__,
        }
    observed = read_oom_score_adj(pid, proc_root=proc_root)
    return {
        "schema": OOM_PRIORITY_SCHEMA,
        "pid": int(pid),
        "requested": requested,
        "observed": observed,
        "status": "applied" if observed == requested else "degraded",
        "reason": None if observed == requested else "unverified",
    }


def prioritize_child_oom(pid: int, *, proc_root: Path = Path("/proc")) -> dict[str, Any]:
    """Make a child the preferred OOM victim; failure is recorded, never raised."""
    return _write_oom_score_adj(pid, OOM_SCORE_ADJ_CHILD, proc_root=proc_root)


def lower_parent_oom_score(
    *, pid: int | None = None, proc_root: Path = Path("/proc")
) -> dict[str, Any]:
    """Best-effort parent protection; lowering normally needs CAP_SYS_RESOURCE."""
    target = os.getpid() if pid is None else int(pid)
    current = read_oom_score_adj(target, proc_root=proc_root)
    if current is None:
        return {
            "schema": OOM_PRIORITY_SCHEMA,
            "pid": target,
            "requested": OOM_SCORE_ADJ_PARENT,
            "observed": None,
            "status": "degraded",
            "reason": "unreadable",
        }
    if current <= OOM_SCORE_ADJ_PARENT:
        return {
            "schema": OOM_PRIORITY_SCHEMA,
            "pid": target,
            "requested": OOM_SCORE_ADJ_PARENT,
            "observed": current,
            "status": "unchanged",
            "reason": None,
        }
    return _write_oom_score_adj(target, OOM_SCORE_ADJ_PARENT, proc_root=proc_root)


def role_pressure_decision(
    pid: int,
    current_bytes: int | None,
    *,
    proc_root: Path = Path("/proc"),
    rss_bytes: int | None = None,
    memory_bytes: int | None = None,
    threshold: float = ROLE_MEMORY_SHARE_THRESHOLD,
    cgroup: Path | None = None,
    tree_bytes: Any = None,
    stop_largest: bool = True,
) -> dict[str, Any]:
    """Stop a role tree when it owns pressure or is the largest tree in the cgroup.

    ``current_bytes`` is the effective (non-reclaimable) cgroup usage supplied by
    :meth:`MemoryBudget.usage_bytes`, so a role's share is measured against the
    memory pressure can actually act on rather than reclaimable page cache.
    Tree memory prefers PSS so shared pages are not counted once per role. Where
    PSS is unavailable the tree falls back to RSS. The 25% share threshold is
    unchanged; the largest remaining tree is stopped even below it so an
    aggregate of smaller roles cannot silently pass the threshold.  An executable
    Reviewer raises ``threshold`` and disables ``stop_largest`` while the Worker
    is idle, so a full test run can use most of the container budget.
    """
    measure = tree_bytes or (lambda candidate: process_tree_memory_bytes(candidate, proc_root=proc_root))
    role_bytes = memory_bytes if memory_bytes is not None else rss_bytes
    if role_bytes is None:
        role_bytes = measure(pid)
    largest_bytes: int | None = None
    largest_pid: int | None = None
    is_largest = False
    if cgroup is not None:
        largest_bytes, largest_pid = largest_process_tree_in_cgroup(
            cgroup, proc_root=proc_root, extra_pids=(pid,), tree_bytes=measure
        )
        if largest_bytes is not None and role_bytes is not None and role_bytes >= largest_bytes:
            is_largest = True
            largest_pid = pid
    if role_bytes is None or current_bytes is None or current_bytes <= 0:
        return {
            "stop": False,
            "reason": "unattributable_pressure",
            "rss_bytes": role_bytes,
            "memory_bytes": role_bytes,
            "pss_bytes": role_bytes,
            "current_bytes": current_bytes,
            "role_share": None,
            "largest_tree": is_largest,
            "largest_tree_bytes": largest_bytes,
            "largest_tree_pid": largest_pid,
            "threshold": threshold,
            "stop_largest": stop_largest,
        }
    share = role_bytes / current_bytes
    over_threshold = share >= threshold
    stop = over_threshold or (stop_largest and is_largest)
    if over_threshold:
        reason = "role_memory_pressure"
    elif stop_largest and is_largest:
        reason = "largest_role_tree"
    else:
        reason = "cgroup_memory_pressure_other_process"
    return {
        "stop": stop,
        "reason": reason,
        "rss_bytes": role_bytes,
        "memory_bytes": role_bytes,
        "pss_bytes": role_bytes,
        "current_bytes": current_bytes,
        "role_share": share,
        "largest_tree": is_largest,
        "largest_tree_bytes": largest_bytes,
        "largest_tree_pid": largest_pid,
        "threshold": threshold,
        "stop_largest": stop_largest,
    }


def wait_for_memory_headroom(
    budget: MemoryBudget,
    *,
    wait_seconds: float = DEFAULT_PRESSURE_WAIT_SECONDS,
    poll_seconds: float = DEFAULT_PRESSURE_POLL_SECONDS,
    sleep: Any = time.sleep,
    monotonic: Any = time.monotonic,
) -> dict[str, Any]:
    """Give a short-lived allocation a chance to clear before refusing a spawn."""
    started = monotonic()
    samples: list[dict[str, Any]] = []
    observed_pressure = False
    while True:
        observed = budget.observe()
        snapshot = observed.snapshot()
        samples.append(snapshot)
        at_pressure = observed._pressure()
        observed_pressure = observed_pressure or at_pressure
        if not at_pressure:
            return {
                "schema": SCHEMA + "#spawn-headroom",
                "status": "cleared" if observed_pressure else "clear",
                "reason": None,
                "waited_seconds": max(0.0, monotonic() - started),
                "snapshot": snapshot,
                "samples": samples,
            }
        elapsed = max(0.0, monotonic() - started)
        remaining = max(0.0, wait_seconds - elapsed)
        if remaining <= 0:
            return {
                "schema": SCHEMA + "#spawn-headroom",
                "status": "refused",
                "reason": "memory_pressure",
                "waited_seconds": elapsed,
                "snapshot": snapshot,
                "samples": samples,
            }
        sleep(min(max(0.0, poll_seconds), remaining))


def _process_stat_rows(proc_root: Path) -> dict[int, tuple[int, int]] | None:
    rows: dict[int, tuple[int, int]] = {}
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return None
    for entry in entries:
        if not entry.isdigit():
            continue
        child = int(entry)
        try:
            stat = Path(proc_root) / str(child) / "stat"
            stat = stat.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        try:
            fields = stat[stat.rindex(")") + 2 :].split()
            parent = int(fields[1])
            rss_pages = int(fields[21])
        except (ValueError, IndexError):
            continue
        rows[child] = (parent, max(0, rss_pages))
    return rows


def _process_tree_pids(pid: int, rows: dict[int, tuple[int, int]]) -> list[int] | None:
    if pid not in rows:
        return None
    tree: list[int] = []
    pending = [pid]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        tree.append(current)
        pending.extend(child for child, (parent, _rss) in rows.items() if parent == current)
    return tree


def read_process_pss_bytes(pid: int, *, proc_root: Path = Path("/proc")) -> int | None:
    """Read one process's proportional set size from ``smaps_rollup``."""
    try:
        text = (Path(proc_root) / str(int(pid)) / "smaps_rollup").read_text(encoding="utf-8")
    except (OSError, UnicodeError, ValueError):
        return None
    for line in text.splitlines():
        if not line.startswith("Pss:"):
            continue
        fields = line.split()
        try:
            kib = int(fields[1])
        except (IndexError, ValueError):
            return None
        return max(0, kib) * 1024
    return None


def process_tree_memory_bytes(pid: int, *, proc_root: Path = Path("/proc")) -> int | None:
    """Best-effort role tree memory using PSS, falling back to RSS per process."""
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError):
        page_size = 4096
    rows = _process_stat_rows(proc_root)
    if rows is None:
        return None
    tree = _process_tree_pids(pid, rows)
    if tree is None:
        return None
    total = 0
    for current in tree:
        pss = read_process_pss_bytes(current, proc_root=proc_root)
        total += pss if pss is not None else rows[current][1] * page_size
    return total


def process_tree_pss_bytes(pid: int, *, proc_root: Path = Path("/proc")) -> int | None:
    """Compatibility name for the PSS-first role tree measurement."""
    return process_tree_memory_bytes(pid, proc_root=proc_root)


def largest_process_tree_in_cgroup(
    cgroup: Path,
    *,
    proc_root: Path = Path("/proc"),
    extra_pids: tuple[int, ...] = (),
    tree_bytes: Any = None,
) -> tuple[int | None, int | None]:
    """Return the largest process tree rooted at a pid in ``cgroup.procs``."""
    pids = read_cgroup_procs(cgroup)
    if pids is None:
        return None, None
    measure = tree_bytes or (lambda candidate: process_tree_memory_bytes(candidate, proc_root=proc_root))
    candidates = list(dict.fromkeys((*pids, *extra_pids)))
    largest_bytes: int | None = None
    largest_pid: int | None = None
    for candidate in candidates:
        size = measure(candidate)
        if size is None:
            continue
        if largest_bytes is None or size > largest_bytes:
            largest_bytes, largest_pid = size, candidate
    return largest_bytes, largest_pid


def process_tree_rss_bytes(pid: int, *, proc_root: Path = Path("/proc")) -> int | None:
    """Best-effort RSS evidence for a child tree; the cgroup sample decides pressure."""
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError):
        page_size = 4096
    rows: dict[int, tuple[int, int]] = {}
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return None
    for entry in entries:
        if not entry.isdigit():
            continue
        child = int(entry)
        try:
            stat = Path(proc_root) / str(child) / "stat"
            stat = stat.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        try:
            fields = stat[stat.rindex(")") + 2 :].split()
            parent = int(fields[1])
            rss_pages = int(fields[21])
        except (ValueError, IndexError):
            continue
        rows[child] = (parent, max(0, rss_pages))
    if pid not in rows:
        return None
    total = 0
    pending = [pid]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        total += rows[current][1] * page_size
        pending.extend(child for child, (parent, _rss) in rows.items() if parent == current)
    return total


__all__ = [
    "CHECKPOINT_SCHEMA",
    "DEFAULT_PRESSURE_POLL_SECONDS",
    "DEFAULT_PRESSURE_WAIT_SECONDS",
    "ENV_CGROUP",
    "ENV_LIMIT",
    "ENV_RESERVE",
    "MemoryBudget",
    "OOM_PRIORITY_SCHEMA",
    "OOM_SCORE_ADJ_CHILD",
    "OOM_SCORE_ADJ_PARENT",
    "ROLE_MEMORY_SHARE_THRESHOLD",
    "SCHEMA",
    "cgroup_memory_path",
    "lower_parent_oom_score",
    "memory_budget",
    "memory_budget_from_environment",
    "oom_score_adj_path",
    "prioritize_child_oom",
    "process_tree_memory_bytes",
    "process_tree_pss_bytes",
    "process_tree_rss_bytes",
    "read_cgroup_memory",
    "read_cgroup_memory_current",
    "read_cgroup_memory_limit",
    "read_cgroup_memory_stat",
    "read_cgroup_memory_usage",
    "read_cgroup_oom_group",
    "read_cgroup_procs",
    "read_memory_checkpoint",
    "read_oom_score_adj",
    "read_process_pss_bytes",
    "reserve_bytes_for",
    "role_pressure_decision",
    "largest_process_tree_in_cgroup",
    "wait_for_memory_headroom",
    "write_memory_checkpoint",
]
