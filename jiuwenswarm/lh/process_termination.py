# Copyright (c) 2026 Chrys. All rights reserved.

"""Terminate only the process group recorded for an admitted public check.

Core starts each public-check command with ``start_new_session=True`` and keeps
that exact group in its registry while any member remains. Linux does not reuse
a pgid while the group still contains a process, so a retained record is enough
to authorize one final TERM-then-KILL of the whole group. The controller never
scans the host by process name, command line or parent/child relationship.
"""

from __future__ import annotations

import logging
import os
import signal
import time
from collections.abc import Iterable, Mapping
from pathlib import Path

logger = logging.getLogger(__name__)


def _active_check_processes() -> tuple[Mapping[str, object], ...]:
    try:
        from jiuwenswarm.pact_core.verification import active_check_processes
    except (ImportError, AttributeError):
        return ()
    try:
        records = active_check_processes()
    except Exception:
        return ()
    return tuple(record for record in records if isinstance(record, Mapping))


def _stat_fields(pid: int) -> list[str]:
    raw = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8")
    return raw[raw.rindex(")") + 2 :].split()


def _start_ticks(pid: int) -> int | None:
    try:
        return int(_stat_fields(pid)[19])
    except (OSError, ValueError, IndexError):
        return None


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _signal_group(pgid: int, number: int) -> None:
    try:
        os.killpg(pgid, number)
    except (ProcessLookupError, PermissionError):
        pass


def _signal_active_group(pgid: int, number: int) -> bool:
    """Signal ``pgid`` only while Core still holds its registry record.

    Core removes a record and signals a group under the same lock, so a record
    that has been cleared can never authorize another signal from here.
    """
    try:
        from jiuwenswarm.pact_core.verification import signal_active_check_group
    except (ImportError, AttributeError):
        _signal_group(pgid, number)
        return True
    try:
        return bool(signal_active_check_group(pgid, number))
    except Exception:
        return False


def _recorded_groups(records: Iterable[Mapping[str, object]] | None = None) -> dict[int, int | None]:
    """Return pgid -> recorded start ticks, never the controller's own group."""
    own_group = os.getpgrp()
    result: dict[int, int | None] = {}
    for record in _active_check_processes() if records is None else records:
        pgid = record.get("pgid")
        if isinstance(pgid, bool) or not isinstance(pgid, int) or pgid <= 1 or pgid == own_group:
            continue
        ticks = record.get("start_ticks")
        result[pgid] = int(ticks) if isinstance(ticks, int) and not isinstance(ticks, bool) else None
    return result


def _record_identity_mismatch(
    pgid: int,
    recorded_ticks: int | None,
    current_ticks: int | None,
) -> None:
    logger.warning(
        "identity_mismatch pgid=%s recorded_start_ticks=%s current_start_ticks=%s",
        pgid,
        recorded_ticks,
        current_ticks,
    )


def terminate_check_processes(
    *, grace_seconds: float = 0.5, records: Iterable[Mapping[str, object]] | None = None
) -> set[int]:
    """TERM recorded check groups, then KILL the same groups after the grace.

    A record is retained by Core until ``os.killpg(pgid, 0)`` confirms that the
    whole group is empty. While the record exists and the group is alive, the
    controller TERMs the entire group and KILLs it after the grace; member
    start times are not revalidated. Linux guarantees that a pgid cannot be
    reused while any member remains, so the record alone is authoritative. If
    the leader itself is still alive, its recorded start tick is still checked
    to reject a reused leader PID. Once Core clears the record, no further
    signal is sent to that pgid.

    Returns the pgids still alive after the KILL wait. Only records provided by
    Core's check-start registry are considered; unrelated processes are never
    signalled.
    """
    source = None if records is None else tuple(records)
    groups = _recorded_groups(source)
    if not groups:
        return set()

    targets: dict[int, int | None] = {}
    for pgid, ticks in groups.items():
        if not _group_alive(pgid):
            continue
        current = _start_ticks(pgid)
        if current is not None and ticks is not None and current != ticks:
            _record_identity_mismatch(pgid, ticks, current)
            continue
        targets[pgid] = ticks
    if not targets:
        return set()

    signalled: set[int] = set()
    for pgid in targets:
        if source is None:
            if _signal_active_group(pgid, signal.SIGTERM):
                signalled.add(pgid)
        else:
            _signal_group(pgid, signal.SIGTERM)
            signalled.add(pgid)
    if not signalled:
        return set()

    deadline = time.monotonic() + max(0.0, float(grace_seconds))
    while time.monotonic() < deadline:
        if not any(_group_alive(pgid) for pgid in signalled):
            return set()
        time.sleep(0.02)

    kill_targets: set[int] = set()
    for pgid in signalled:
        if not _group_alive(pgid):
            continue
        ticks = targets[pgid]
        current = _start_ticks(pgid)
        if current is not None and ticks is not None and current != ticks:
            _record_identity_mismatch(pgid, ticks, current)
            continue
        if source is None:
            if _signal_active_group(pgid, signal.SIGKILL):
                kill_targets.add(pgid)
        else:
            _signal_group(pgid, signal.SIGKILL)
            kill_targets.add(pgid)

    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        live = {pgid for pgid in kill_targets if _group_alive(pgid)}
        if not live:
            return set()
        time.sleep(0.02)
    return {pgid for pgid in kill_targets if _group_alive(pgid)}


def terminate_descendants(
    *, grace_seconds: float = 0.5, root_pid: int | None = None, proc_root: Path = Path("/proc")
) -> set[int]:
    """Compatibility wrapper for the old name.

    ``root_pid`` and ``proc_root`` are accepted but ignored: descendant scans
    are exactly what allowed unrelated processes to be killed. Only the
    recorded check groups are terminated.
    """
    return terminate_check_processes(grace_seconds=grace_seconds)
