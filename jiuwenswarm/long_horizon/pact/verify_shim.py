# Copyright (c) 2026 Chrys. All rights reserved.

"""Run a PACT verify command in a detached worktree that can actually find its tools.

Migrated from Chrys ``6fcfb3321892735631b49f33236b1e2a988ded2d``:
``src/chrys/pact/verify_shim.py``. Uses the installed Jiuwen suite and keeps the
original ignored-dependency linking, fail-fast checks, and execution receipts.

pact_core verifies every checkpoint in a fresh ``git worktree``. A fresh worktree
carries only tracked files, so the dependency directories a repository keeps
ignored -- ``node_modules``, ``.venv``, ``target``, a vendored tree -- are absent
there, and ``npm test`` ends with ``vitest: not found`` while the very same command
passes in the primary checkout. This shim links those ignored directories from the
primary checkout into the worktree (only where the worktree has nothing of that
name) and then runs the real command in place. Linking is best effort: the command
runs whatever happened.

Usage (what ``wrap_verify_command`` produces)::

    python -m jiuwenswarm.long_horizon.pact.verify_shim -- 'npm test --silent'
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from pact_core.runtime.schemas import RuntimeStateError

from jiuwenswarm.long_horizon.pact.public_verification import isolated_verify_command, pinned_commands

_SKIP_PREFIXES = (".pact", ".pact-io", ".git")
MEMORY_ENV_LIMIT = "LONG_HORIZON_MEMORY_MAX_BYTES"
MEMORY_ENV_RESERVE = "LONG_HORIZON_MEMORY_RESERVE_BYTES"
MEMORY_ENV_CGROUP = "LONG_HORIZON_MEMORY_CGROUP"
MEMORY_ENV_EVENT_LOG = "LONG_HORIZON_MEMORY_EVENT_LOG"
OOM_SCORE_ADJ_CHILD = 800
MEMORY_RESERVE_FRACTION = 0.15
MEMORY_RESERVE_MIN_BYTES = 512 * 1024 * 1024
MEMORY_RESERVE_MAX_BYTES = 2 * 1024 * 1024 * 1024


def _memory_int(value):
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _memory_text(path):
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None


def _memory_cgroup(configured):
    if configured:
        return Path(configured)
    root = Path("/sys/fs/cgroup")
    try:
        lines = Path("/proc/self/cgroup").read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        lines = []
    relative = None
    for line in lines:
        if line.startswith("0::"):
            relative = line[3:].lstrip("/")
            break
    if relative is not None and ".." not in Path(relative).parts:
        candidate = root / relative
        if (candidate / "memory.max").is_file() or (candidate / "memory.current").is_file():
            return candidate
    if (root / "memory.max").is_file() or (root / "memory.current").is_file():
        return root
    return None


def _memory_stat_counters(location):
    """Return the memory.stat counters needed to discount reclaimable cache."""
    if location is None:
        return None
    raw = _memory_text(location / "memory.stat")
    if raw is None:
        return None
    names = ("file", "file_dirty", "file_writeback")
    values = {}
    for line in raw.splitlines():
        fields = line.split()
        if len(fields) != 2 or fields[0] not in names:
            continue
        try:
            value = int(fields[1])
        except ValueError:
            return None
        if value < 0:
            return None
        values[fields[0]] = value
    if "file" not in values:
        return None
    values.setdefault("file_dirty", 0)
    values.setdefault("file_writeback", 0)
    return values


def _memory_usage(location, current):
    """Return effective non-reclaimable usage and the basis used to compute it."""
    if current is None:
        return None, "unavailable"
    counters = _memory_stat_counters(location)
    if counters is None:
        return current, "memory.current"
    effective = max(
        0,
        current - counters["file"] + counters["file_dirty"] + counters["file_writeback"],
    )
    return effective, "memory.stat"


def _memory_snapshot():
    limit = _memory_int(os.environ.get(MEMORY_ENV_LIMIT))
    location = _memory_cgroup(os.environ.get(MEMORY_ENV_CGROUP))
    if location is None and limit is None:
        return None
    maximum_raw = _memory_text(location / "memory.max") if location is not None else None
    maximum = None if maximum_raw in (None, "max") else _memory_int(maximum_raw)
    if limit is None:
        limit = maximum
    elif maximum is not None:
        limit = min(limit, maximum)
    if limit is None:
        return None
    reserve = _memory_int(os.environ.get(MEMORY_ENV_RESERVE))
    if reserve is None:
        reserve = max(MEMORY_RESERVE_MIN_BYTES,
                      min(MEMORY_RESERVE_MAX_BYTES, int(limit * MEMORY_RESERVE_FRACTION)))
    current = _memory_int(_memory_text(location / "memory.current")) if location is not None else None
    if current is None and location is not None and _memory_text(location / "memory.current") == "0":
        current = 0
    effective, usage_basis = _memory_usage(location, current)
    threshold = max(0, limit - reserve)
    degraded_reason = None
    if maximum is None:
        degraded_reason = "memory_max_unavailable"
    if current is None:
        degraded_reason = "memory_current_unavailable"
    assessable = degraded_reason is None and effective is not None
    return {
        "schema": "long-horizon/memory-budget/v1",
        "cgroup": str(location) if location is not None else None,
        "limit_bytes": limit,
        "current_bytes": current,
        "effective_bytes": effective,
        "usage_basis": usage_basis,
        "reserve_bytes": reserve,
        "threshold_bytes": threshold,
        "degraded": degraded_reason is not None,
        "degraded_reason": degraded_reason,
        "pressure_assessable": assessable,
        "at_pressure": assessable and effective >= threshold,
        "observed_at_unix": time.time(),
    }


def _record_memory_event(event):
    sys.stderr.write(json.dumps(event, sort_keys=True) + "\n")
    sys.stderr.flush()
    path = os.environ.get(MEMORY_ENV_EVENT_LOG)
    if path:
        try:
            with open(path, "a", encoding="utf-8") as stream:
                stream.write(json.dumps(event, sort_keys=True) + "\n")
        except OSError:
            pass


def _wait_for_memory_headroom(*, wait_seconds=2.0, poll_seconds=0.25,
                              sleep=time.sleep, monotonic=time.monotonic):
    """Give transient pressure a short chance to clear before a check is refused."""
    started = monotonic()
    samples = []
    observed_pressure = False
    while True:
        snapshot = _memory_snapshot()
        if snapshot is None:
            return None
        samples.append(snapshot)
        if snapshot.get("degraded"):
            _record_memory_event({
                "schema": "long-horizon/memory-pressure-event/v1",
                "reason": "memory_observation_degraded",
                "stage": "public_check",
                "waited_seconds": max(0.0, monotonic() - started),
                "memory": snapshot,
                "samples": samples,
                "refused_spawn": False,
            })
            return None
        at_pressure = snapshot.get("at_pressure") is True
        observed_pressure = observed_pressure or at_pressure
        if not at_pressure:
            return None
        elapsed = max(0.0, monotonic() - started)
        remaining = max(0.0, wait_seconds - elapsed)
        if remaining <= 0:
            event = {
                "schema": "long-horizon/memory-pressure-event/v1",
                "reason": "pre_spawn_memory_pressure",
                "stage": "public_check",
                "waited_seconds": elapsed,
                "memory": snapshot,
                "samples": samples,
                "refused_spawn": True,
            }
            _record_memory_event(event)
            return event
        sleep(min(max(0.0, poll_seconds), remaining))


def _set_oom_score_adj(pid, *, proc_root=Path("/proc")):
    path = Path(proc_root) / str(int(pid)) / "oom_score_adj"
    try:
        path.write_text(f"{OOM_SCORE_ADJ_CHILD}\n", encoding="utf-8")
        observed = int(path.read_text(encoding="utf-8").strip())
    except (OSError, UnicodeError, ValueError) as error:
        return {"schema": "long-horizon/oom-priority/v1", "pid": int(pid),
                "requested": OOM_SCORE_ADJ_CHILD, "observed": None,
                "status": "degraded", "reason": type(error).__name__}
    return {"schema": "long-horizon/oom-priority/v1", "pid": int(pid),
            "requested": OOM_SCORE_ADJ_CHILD, "observed": observed,
            "status": "applied" if observed == OOM_SCORE_ADJ_CHILD else "degraded",
            "reason": None if observed == OOM_SCORE_ADJ_CHILD else "unverified"}


def wrap_verify_command(command: str) -> str:
    """Return ``command`` wrapped so it runs through this shim with the same interpreter."""
    return isolated_verify_command(["--", command])


def primary_checkout(worktree: Path) -> Path | None:
    """The main checkout a linked worktree belongs to, or None when ``worktree`` is not linked."""
    git = shutil.which("git")
    if git is None:
        return None
    try:
        common = subprocess.run(  # noqa: S603 - fixed git argv
            [git, "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=worktree,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        ).stdout.strip()
        own = subprocess.run(  # noqa: S603 - fixed git argv
            [git, "rev-parse", "--path-format=absolute", "--git-dir"],
            cwd=worktree,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    if not common or common == own:
        return None
    primary = Path(common).resolve().parent
    return primary if primary.is_dir() else None


def ignored_directories(primary: Path) -> list[str]:
    """Ignored directories of the primary checkout, relative, as git lists them."""
    git = shutil.which("git")
    if git is None:
        return []
    try:
        listing = subprocess.run(  # noqa: S603 - fixed git argv
            [git, "ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z"],
            cwd=primary,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=True,
            timeout=120,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    out: list[str] = []
    for entry in listing.split("\0"):
        if not entry.endswith("/"):
            continue
        rel = entry.rstrip("/")
        if not rel or rel.startswith(_SKIP_PREFIXES) or ".." in Path(rel).parts:
            continue
        out.append(rel)
    return out


def link_ignored_dependencies(worktree: Path) -> list[Path]:
    """Symlink the primary checkout's ignored directories into ``worktree``; return what was linked."""
    primary = primary_checkout(worktree)
    if primary is None or primary.resolve() == worktree.resolve():
        return []
    linked: list[Path] = []
    for rel in ignored_directories(primary):
        source = primary / rel
        target = worktree / rel
        if not source.is_dir() or target.exists() or target.is_symlink():
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(source, target, target_is_directory=True)
        except OSError:
            continue
        linked.append(target)
    return linked


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "--":
        args = args[1:]
    commands = args
    public_digest: str | None = None
    command_env = dict(os.environ)
    command_env.pop("JIUWENSWARM_PACT_SOURCE_WORKSPACE", None)
    if len(args) == 10 and args[::2] == [
        "--public-spec",
        "--sha256",
        "--campaign-workspace",
        "--campaign-id",
        "--contract-sha256",
    ]:
        try:
            commands = pinned_commands(
                Path(args[1]), args[3], workspace=Path(args[5]), campaign_id=args[7], contract_sha256=args[9]
            )
            public_digest = args[3]
            command_env["JIUWENSWARM_PACT_SOURCE_WORKSPACE"] = str(Path(args[5]).resolve())
        except (OSError, ValueError, RuntimeStateError, KeyError, TypeError, AttributeError) as error:
            sys.stderr.write(f"Public verification refused: {error}\n")
            return 2
    elif len(args) != 1 or not args[0].strip():
        sys.stderr.write("usage: python -m jiuwenswarm.long_horizon.pact.verify_shim -- '<verify command>'\n")
        return 2
    cwd = Path.cwd()
    try:
        linked = link_ignored_dependencies(cwd)
    except Exception as error:
        sys.stderr.write(f"[verify-shim] dependency linking skipped: {error}\n")
        linked = []
    if linked:
        noun = "directory" if len(linked) == 1 else "directories"
        sys.stderr.write(f"[verify-shim] linked {len(linked)} ignored {noun} from the primary checkout\n")
    # PACT owns the timeout/process group for this verifier, as for explicit commands.
    for index, command in enumerate(commands, start=1):
        if public_digest is not None and os.name != "nt":
            # A failed submission followed by a stale-artifact assertion must not
            # become success merely because a POSIX shell returns its last status.
            command = "set -e\n" + command
        if _wait_for_memory_headroom() is not None:
            return 75
        process = subprocess.Popen(  # noqa: S602 - public verify commands are shell strings by contract
            command, shell=True, cwd=cwd, env=command_env, stdin=subprocess.DEVNULL
        )
        oom_priority = _set_oom_score_adj(process.pid)
        if oom_priority["status"] == "degraded":
            _record_memory_event({"event": "oom_priority_degraded", **oom_priority})
        completed = subprocess.CompletedProcess(command, process.wait())
        if public_digest is not None and (completed.returncode != 0 or index == len(commands)):
            # PACT retains this bounded status line even when command output is long.
            # Admission or process failures before a check returns produce no receipt.
            sys.stdout.write(
                json.dumps(
                    {
                        "schema": "jiuwenswarm/pact-public-verification-result/v1",
                        "spec_sha256": public_digest,
                        "planned_checks": len(commands),
                        "executed_checks": index,
                        "status": "passed" if completed.returncode == 0 else "failed",
                    }
                )
                + "\n"
            )
            sys.stdout.flush()
        if completed.returncode != 0:
            return completed.returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
