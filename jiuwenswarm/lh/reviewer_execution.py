# Copyright (c) 2026 Chrys. All rights reserved.

"""Executable, disposable Reviewer turns.

The Reviewer used to be a read-only reader.  A Reviewer that can only read the
final tree cannot build the candidate, run the project's checks, or observe a
fresh/empty user-visible state.  This module keeps acceptance independent while
letting the Reviewer execute:

* every Reviewer turn runs with a disposable copy of the candidate checkpoint as
  its cwd and writable area; writes inside that copy are expected and allowed;
* the real candidate, the source workspace and the delivery location are hashed
  before and after the turn, and a change discards the verdict, restores the
  protected tree and records the violation;
* processes started by the Reviewer are stopped and the copy is removed.

The Core still owns verification, admission, acceptance and delivery.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import CancelledError as FutureCancelledError
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterable
from uuid import uuid4

from jiuwenswarm.pact_core.artifacts import write_json_atomic
from jiuwenswarm.pact_core.schemas import ReviewDecisionCapture, TurnRequest, TurnResult

from .generalization_check import reviewer_generalization_check_guidance
from .process_kill_guard import (  # noqa: F401
    PROCESS_KILL_GUIDANCE_VERSION,
    guarded_environment,
    process_kill_guidance,
)
from .reference_client_spec import reviewer_reference_client_spec_guidance
from .reviewer_privilege import ReviewerLaunchPolicy, ReviewerProtectionError
from .turn_environment import current_environment, turn_environment

REVIEWER_EXECUTION_GUIDANCE_VERSION = "isolated-executable-review/v1"
REVIEWER_EXECUTION_PROMPT_MARKER = "## Executable isolated review"
SCRATCH_GUIDANCE_MARKER = "## Scratch-only build boundary"

# Directories written by the controller as runtime metadata, not by a
# protected-view writer. They are excluded from recovered-view fingerprints
# so an ordinary budget/ledger update cannot invalidate its own guard.
CONTROLLER_METADATA_PREFIXES = (".pact", ".pact-io")

DEFAULT_REVIEWER_MEMORY_SHARE = 0.80
DEFAULT_REVIEWER_TIMEOUT_FRACTION = 0.50
MIN_REVIEWER_TIMEOUT_SECONDS = 600.0
MAX_REVIEWER_TIMEOUT_SECONDS = 21600.0
REVIEWER_FINISH_RESERVE_SECONDS = 60.0
REVIEWER_SCRATCH_SCHEMA = "long-horizon/reviewer-scratch/v1"
REVIEWER_MUTATION_SCHEMA = "long-horizon/reviewer-mutation-evidence/v1"
REVIEWER_FAILURE_SCHEMA = "long-horizon/reviewer-execution-failure/v1"
REVIEWER_SKIP_SCHEMA = "long-horizon/reviewer-skip-evidence/v1"
REVIEWER_CLEANUP_SCHEMA = "long-horizon/reviewer-cleanup-failure/v1"
PROTECTED_VIEW_SCHEMA = "long-horizon/protected-view-integrity/v1"
PROTECTED_VIEW_RECOVERY_SCHEMA = "long-horizon/protected-view-recovery/v1"
REVIEWER_PERIPHERAL_RESERVE_SECONDS = 10.0
DEFAULT_RECOVERY_QUIET_SECONDS = 5.0

ENV_REVIEWER_MEMORY_SHARE = "LONG_HORIZON_REVIEWER_MEMORY_SHARE"
ENV_REVIEWER_TIMEOUT_FRACTION = "LONG_HORIZON_REVIEWER_TIMEOUT_FRACTION"
ENV_REVIEWER_TIMEOUT_MIN_SECONDS = "LONG_HORIZON_REVIEWER_TIMEOUT_MIN_SECONDS"
ENV_REVIEWER_TIMEOUT_MAX_SECONDS = "LONG_HORIZON_REVIEWER_TIMEOUT_MAX_SECONDS"
ENV_RECOVERY_QUIET_SECONDS = "LONG_HORIZON_RECOVERY_QUIET_SECONDS"

_TERM_GRACE_SECONDS = 3.0
_KILL_WAIT_SECONDS = 3.0
_MAX_EVIDENCE_PATHS = 200


class ReviewerExecutionError(RuntimeError):
    """A bounded, recoverable failure of one Reviewer isolation step."""

    def __init__(self, stage: str, detail: str) -> None:
        super().__init__(detail)
        self.stage = stage


class ReviewerPeripheralTimeout(ReviewerExecutionError):
    """A copy, hash, restore or delete step exhausted its share of the trial."""


def _remaining_budget(deadline: float | None, *, reserve: float = 0.0) -> float | None:
    if deadline is None:
        return None
    return max(0.0, float(deadline) - time.time() - max(0.0, reserve))


def _bounded_timeout(deadline: float | None, cap: float, *, stage: str) -> float:
    remaining = _remaining_budget(deadline)
    if remaining is None:
        return cap
    if remaining <= 0:
        raise ReviewerPeripheralTimeout(stage, f"{stage} has no remaining trial budget")
    return max(0.001, min(cap, remaining))


def recovery_quiet_seconds() -> float:
    """How long a restored view must stay unchanged before recovery is trusted."""
    return _env_float(
        ENV_RECOVERY_QUIET_SECONDS, DEFAULT_RECOVERY_QUIET_SECONDS, 0.0, 60.0
    )


def reviewer_scratch_shell_prefix(scratch_candidate: Path | str) -> str:
    """An explicit per-command environment for backends without session env support."""
    environment = reviewer_scratch_environment(scratch_candidate)
    # PWD is advisory and some shells rewrite it; the output/cache variables are
    # the ones that must stay in the disposable checkout.
    names = (
        "TMPDIR",
        "TMP",
        "TEMP",
        "XDG_CACHE_HOME",
        "CARGO_TARGET_DIR",
        "CARGO_HOME",
        "GOCACHE",
        "GOMODCACHE",
        "GOPATH",
        "PIP_CACHE_DIR",
        "UV_CACHE_DIR",
        "NPM_CONFIG_CACHE",
        "npm_config_cache",
        "YARN_CACHE_FOLDER",
        "PNPM_STORE_DIR",
        "GRADLE_USER_HOME",
        "MAVEN_USER_HOME",
        "BUNDLE_PATH",
        "BUN_INSTALL_CACHE_DIR",
        "PUPPETEER_CACHE_DIR",
    )
    return "env " + " ".join(
        f"{name}={shlex.quote(environment[name])}" for name in names if name in environment
    )


def _scratch_rule(scratch_candidate: Path | str | None, *, marker: bool) -> str:
    scratch = (
        Path(scratch_candidate).expanduser().absolute()
        if scratch_candidate is not None
        else None
    )
    prefix = SCRATCH_GUIDANCE_MARKER + ": " if marker else ""
    shell_prefix = reviewer_scratch_shell_prefix(scratch) if scratch is not None else ""
    return (
        prefix
        + "Build and write only in the assigned scratch checkout at "
        + (str(scratch) if scratch is not None else "the absolute path shown as your cwd")
        + ". Before any build, cd to that exact absolute path. Do not use find/ls over /workspace, /tmp or "
        "other global roots to locate the project, and never build in a path discovered that way."
        + (
            " If your backend does not inject the scratch environment into the shell tool, prefix every "
            "shell command with `" + shell_prefix + "` and run it from that same scratch cwd."
            if shell_prefix
            else ""
        )
    )


def reviewer_scratch_guidance(scratch_candidate: Path | str | None = None) -> str:
    """The addressable scratch-only clause, safe to append more than once."""
    return "\n\n" + _scratch_rule(scratch_candidate, marker=True)


def reviewer_execution_guidance(scratch_candidate: Path | str | None = None) -> str:
    """Bound review instructions; deliberately generic (no task names)."""
    scratch_rule = _scratch_rule(scratch_candidate, marker=True) + "\n"
    return (
        "\n\n" + REVIEWER_EXECUTION_PROMPT_MARKER + " (" + REVIEWER_EXECUTION_GUIDANCE_VERSION + ")\n"
        "Your cwd is a disposable copy of the candidate and the only writable area. Build here and run the "
        "project's full test or check command in a clean state; report the exact command and result.\n"
        + scratch_rule
        +
        "If the deliverable has a user-visible interface, run the application and check fresh/empty data, a "
        "brand-new or anonymous account, after restart, and zero/empty lists with a real browser or HTTP.\n"
        "Every interface element named by the requirement must render correctly in these states, not only when "
        "data is present. A missing or broken element is blocking: return the candidate to the Worker with "
        "reproduction steps.\n"
        "Do not accept interface behavior by reading code alone. Never write the real workspace, the candidate "
        "checkpoint or the delivery location; a detected change discards this verdict.\n"
        "For deliverables with no user-visible interface this interface gate does not apply.\n"
        "Record remaining implementation gaps as a JSON array inside `<pact-gaps>...</pact-gaps>` on its "
        "own lines; use `[]` when no gap remains. Each item must be a concise, specific gap description. "
        "Always supply this block, and put it before any terminal marker or trailing JSON decision: the "
        "controller uses the normalized gap identities, not prose wording, to distinguish continued "
        "progress from a repeated no-progress round.\n"
        "Never stop processes by broad command-line or name matching (`pkill -f`, `killall`, "
        "`pgrep -f ... | xargs kill`); stop only a process you started with a recorded PID, or its process "
        "group, and write a background service's PID to a file.\n"
        + reviewer_generalization_check_guidance()
        + reviewer_reference_client_spec_guidance()
    )


def _env_float(name: str, default: float, low: float, high: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return min(high, max(low, value))


def _cgroup_cpu_quota() -> int | None:
    """Return floor(quota/period) for the container's cgroup v2 CPU limit."""
    candidates: list[Path] = []
    try:
        for line in Path("/proc/self/cgroup").read_text(encoding="utf-8").splitlines():
            fields = line.split(":", 2)
            if len(fields) == 3 and fields[0] == "0" and not fields[1]:
                relative = fields[2].lstrip("/")
                if ".." not in Path(relative).parts:
                    candidates.append(Path("/sys/fs/cgroup") / relative / "cpu.max")
    except (OSError, UnicodeError):
        pass
    candidates.append(Path("/sys/fs/cgroup/cpu.max"))
    for path in candidates:
        try:
            quota_raw, period_raw = path.read_text(encoding="utf-8").split()[:2]
            if quota_raw == "max":
                return None
            quota, period = int(quota_raw), int(period_raw)
        except (OSError, ValueError, UnicodeError):
            continue
        if quota > 0 and period > 0:
            return max(1, quota // period)
    return None


def cpu_slots() -> int:
    """Build/test parallelism follows the container CPU quota and affinity."""
    raw = os.environ.get("LONG_HORIZON_REVIEWER_CPU_SLOTS", "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    try:
        affinity = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        affinity = os.cpu_count() or 1
    quota = _cgroup_cpu_quota()
    return max(1, min(affinity, quota) if quota is not None else affinity)


def reviewer_environment() -> dict[str, str]:
    """Environment variables that bound concurrent builds and test workers."""
    slots = str(cpu_slots())
    return {
        "MAKEFLAGS": f"-j{slots}",
        "CMAKE_BUILD_PARALLEL_LEVEL": slots,
        "CARGO_BUILD_JOBS": slots,
        "GOMAXPROCS": slots,
        "npm_config_jobs": slots,
        "UV_CONCURRENT_BUILDS": slots,
        "UV_CONCURRENT_DOWNLOADS": slots,
        "PYTEST_XDIST_AUTO_NUM_WORKERS": slots,
    }


def reviewer_scratch_environment(scratch: Path | str) -> dict[str, str]:
    """Keep common build/tool outputs inside the assigned scratch checkout.

    The variables are deliberately generic: any tool that honours its usual
    output/cache variable stays in the disposable copy, while a tool that does
    not cannot silently place large build products in a protected view.
    """
    root = Path(scratch).expanduser().absolute()
    cache = root / ".scratch-cache"
    tmp = root / ".scratch-tmp"
    return {
        "PWD": str(root),
        "TMPDIR": str(tmp),
        "TMP": str(tmp),
        "TEMP": str(tmp),
        "XDG_CACHE_HOME": str(cache),
        "CARGO_TARGET_DIR": str(root / "target"),
        "CARGO_HOME": str(cache / "cargo"),
        "GOCACHE": str(cache / "go-build"),
        "GOMODCACHE": str(cache / "go-mod"),
        "GOPATH": str(cache / "go"),
        "PIP_CACHE_DIR": str(cache / "pip"),
        "UV_CACHE_DIR": str(cache / "uv"),
        "NPM_CONFIG_CACHE": str(cache / "npm"),
        "npm_config_cache": str(cache / "npm"),
        "YARN_CACHE_FOLDER": str(cache / "yarn"),
        "PNPM_STORE_DIR": str(cache / "pnpm"),
        "GRADLE_USER_HOME": str(cache / "gradle"),
        "MAVEN_USER_HOME": str(cache / "maven"),
        "BUNDLE_PATH": str(cache / "bundle"),
        "BUN_INSTALL_CACHE_DIR": str(cache / "bun"),
        "PUPPETEER_CACHE_DIR": str(cache / "puppeteer"),
    }


def reviewer_scratch_path(root: Path | str, call_id: str) -> Path:
    """The one disposable checkout path shared by reviewer and side-call code."""
    return Path(root).expanduser().absolute() / "reviewer-scratch" / call_id / "candidate"


def protected_view_roots(workdir: Path | str, protected_roots: Iterable[Path | str] = ()) -> tuple[Path, ...]:
    """The exact protected scope: the assigned view plus every controller surface."""
    roots: list[Path] = []
    seen: set[Path] = set()
    for value in (workdir, *protected_roots):
        if not value:
            continue
        path = Path(value).expanduser().absolute()
        if path in seen:
            continue
        seen.add(path)
        roots.append(path)
    return tuple(roots)


def reviewer_memory_share() -> float:
    """Fraction of the effective container budget a Reviewer may own.

    Worker process roles keep the historical 25% share.  While the Worker is
    idle, an executing Reviewer may use most of the container budget.
    """
    return _env_float(ENV_REVIEWER_MEMORY_SHARE, DEFAULT_REVIEWER_MEMORY_SHARE, 0.25, 0.95)


@dataclass(frozen=True, slots=True)
class ReviewerExecutionLimits:
    timeout_fraction: float
    min_timeout_seconds: float
    max_timeout_seconds: float
    finish_reserve_seconds: float = REVIEWER_FINISH_RESERVE_SECONDS


def reviewer_execution_limits() -> ReviewerExecutionLimits:
    minimum = _env_float(ENV_REVIEWER_TIMEOUT_MIN_SECONDS, MIN_REVIEWER_TIMEOUT_SECONDS, 1.0, MAX_REVIEWER_TIMEOUT_SECONDS)
    maximum = _env_float(ENV_REVIEWER_TIMEOUT_MAX_SECONDS, MAX_REVIEWER_TIMEOUT_SECONDS, minimum, 7 * 24 * 3600.0)
    return ReviewerExecutionLimits(
        timeout_fraction=_env_float(ENV_REVIEWER_TIMEOUT_FRACTION, DEFAULT_REVIEWER_TIMEOUT_FRACTION, 0.05, 1.0),
        min_timeout_seconds=minimum,
        max_timeout_seconds=maximum,
    )


def reviewer_timeout_seconds(
    *,
    requested: float,
    deadline: float | None,
    now: float | None = None,
    limits: ReviewerExecutionLimits | None = None,
) -> float:
    """A bounded per-call Reviewer timeout inside the original trial deadline.

    The previous turn timeout is a floor only when the remaining trial budget is
    larger; the result is always capped by the remaining budget minus a small
    finish reserve, so a slow build cannot consume delivery time.
    """
    limits = limits or reviewer_execution_limits()
    clock = time.time if now is None else lambda: float(now)
    if deadline is None:
        return min(limits.max_timeout_seconds, max(limits.min_timeout_seconds, float(requested)))
    remaining = float(deadline) - clock()
    if remaining <= 0:
        return 0.0
    reserve = min(limits.finish_reserve_seconds, remaining * 0.5)
    usable = max(1.0, remaining - reserve)
    target = remaining * limits.timeout_fraction
    target = min(limits.max_timeout_seconds, max(limits.min_timeout_seconds, target))
    return max(1.0, min(target, usable))


def _git_environment() -> dict[str, str]:
    """Keep read-only Git queries from refreshing the protected index as a side effect."""
    return {**current_environment(), "GIT_OPTIONAL_LOCKS": "0"}


def _run(
    argv: list[str],
    *,
    cwd: Path | None = None,
    input_bytes: bytes | None = None,
    deadline: float | None = None,
    stage: str = "command",
    env: dict[str, str] | None = None,
) -> bytes:
    timeout = _bounded_timeout(deadline, 300.0, stage=stage)
    try:
        completed = subprocess.run(
            argv,
            env=env if env is not None else current_environment(),
            cwd=str(cwd) if cwd is not None else None,
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise ReviewerPeripheralTimeout(stage, f"{stage} timed out after {timeout:.1f}s") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip().splitlines()
        operation = " ".join(argv[:4])
        raise RuntimeError(
            f"command failed ({completed.returncode}): {operation}"
            + (f": {detail[-1][:300]}" if detail else "")
        )
    return completed.stdout


def _git(root: Path, *args: str, deadline: float | None = None) -> str:
    return _run(
        ["git", "-C", str(root), *args],
        deadline=deadline,
        stage="hash",
        env=_git_environment(),
    ).decode("utf-8", errors="replace")


def _git_optional(root: Path, *args: str, deadline: float | None = None) -> str:
    try:
        timeout = _bounded_timeout(deadline, 300.0, stage="hash")
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            env=_git_environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, ReviewerPeripheralTimeout):
        return ""
    if completed.returncode != 0:
        return ""
    return completed.stdout.decode("utf-8", errors="replace")


def _is_git_tree(root: Path, *, deadline: float | None = None) -> bool:
    """True only for the root of a checkout, not a directory inside one."""
    try:
        top = _git(root, "rev-parse", "--show-toplevel", deadline=deadline).strip()
    except Exception:
        return False
    return bool(top) and Path(top).resolve() == Path(root).expanduser().resolve()


def materialize_checkpoint(
    source: Path, destination: Path, *, deadline: float | None = None,
    cancelled: threading.Event | None = None,
) -> None:
    """Copy the checked-out checkpoint into ``destination`` without touching git metadata.

    ``git archive`` is a single streaming snapshot and keeps the copy free of a
    live ``.git`` pointer back to the controller repository.  Non-git inputs fall
    back to a reflink copy where the filesystem supports it.  Both paths honor
    the remaining trial budget.
    """
    source = Path(source).expanduser().absolute()
    destination = Path(destination).expanduser().absolute()
    # Rebuilding a candidate replaces any view that a recovered guard still
    # names at that location; the guard must not survive its view.
    discard_recovered_views((destination,))
    deadline = min(float(deadline), time.time() + 300.0) if deadline is not None else time.time() + 300.0
    try:
        destination.mkdir(parents=True, exist_ok=True)
        if cancelled is not None and cancelled.is_set():
            raise ReviewerExecutionError("cancelled", "reviewer materialization cancelled")
        copy_options: dict[str, Any] = {"deadline": deadline}
        if cancelled is not None:
            copy_options["cancelled"] = cancelled
        if _is_git_tree(source, deadline=deadline):
            _stream_git_archive(source, destination, **copy_options)
            return
        _copy_tree(source, destination, **copy_options)
    except ReviewerExecutionError:
        _remove(destination)
        raise
    except Exception as exc:
        _remove(destination)
        raise ReviewerExecutionError("materialize", f"{type(exc).__name__}: {exc}") from exc


def _wait_copy_process(process: subprocess.Popen, deadline: float | None, cancelled: threading.Event | None) -> int:
    while process.poll() is None:
        if cancelled is not None and cancelled.is_set():
            raise ReviewerExecutionError("cancelled", "reviewer materialization cancelled")
        timeout = _bounded_timeout(deadline, 300.0, stage="materialize")
        try:
            return process.wait(timeout=min(0.1, timeout))
        except subprocess.TimeoutExpired:
            continue
    return process.returncode


def _stream_git_archive(source: Path, destination: Path, *, deadline: float | None,
                        cancelled: threading.Event | None = None) -> None:
    producer: subprocess.Popen | None = None
    extractor: subprocess.Popen | None = None
    with tempfile.TemporaryFile() as producer_error, tempfile.TemporaryFile() as extractor_error:
        try:
            producer = subprocess.Popen(
                ["git", "-C", str(source), "archive", "--format=tar", "HEAD"],
                env=current_environment(),
                stdout=subprocess.PIPE, stderr=producer_error, start_new_session=True,
            )
            assert producer.stdout is not None
            extractor = subprocess.Popen(
                ["tar", "-x", "-C", str(destination)], stdin=producer.stdout,
                env=current_environment(),
                stdout=subprocess.DEVNULL, stderr=extractor_error, start_new_session=True,
            )
            producer.stdout.close()
            extractor_code = _wait_copy_process(extractor, deadline, cancelled)
            producer_code = _wait_copy_process(producer, deadline, cancelled)
            if producer_code or extractor_code:
                producer_error.seek(0)
                extractor_error.seek(0)
                detail = (producer_error.read(300) + extractor_error.read(300)).decode("utf-8", "replace")
                raise ReviewerExecutionError(
                    "materialize", f"git archive/extraction failed ({producer_code}, {extractor_code}): {detail}"
                )
        except subprocess.TimeoutExpired as exc:
            raise ReviewerPeripheralTimeout("materialize", "git archive/extraction timed out") from exc
        finally:
            for child in (extractor, producer):
                if child is None:
                    continue
                if child.poll() is None:
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                try:
                    child.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass


def _copy_tree(source: Path, destination: Path, *, deadline: float | None = None,
               cancelled: threading.Event | None = None) -> None:
    cp = shutil.which("cp")
    if cp is not None:
        _bounded_timeout(deadline, 300.0, stage="materialize")
        process = None
        try:
            process = subprocess.Popen(
                [cp, "-a", "--reflink=auto", str(source) + os.sep + ".", str(destination)],
                env=current_environment(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            code = _wait_copy_process(process, deadline, cancelled)
        finally:
            if process is not None and process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=2)
        if code == 0:
            # A copied worktree pointer would let the Reviewer reach the real
            # repository through the shared git directory.
            git_pointer = destination / ".git"
            if git_pointer.is_file():
                git_pointer.unlink()
            return
    _bounded_timeout(deadline, 300.0, stage="materialize")
    def copy_file(src: str, dst: str) -> str:
        with open(src, "rb") as reader, open(dst, "wb") as writer:
            while chunk := reader.read(1024 * 1024):
                if cancelled is not None and cancelled.is_set():
                    raise ReviewerExecutionError("cancelled", "reviewer materialization cancelled")
                _bounded_timeout(deadline, 300.0, stage="materialize")
                writer.write(chunk)
        shutil.copystat(src, dst, follow_symlinks=False)
        return dst

    def ignore(directory: str, names: list[str]) -> set[str]:
        if cancelled is not None and cancelled.is_set():
            raise ReviewerExecutionError("cancelled", "reviewer materialization cancelled")
        return {".git"} & set(names)

    shutil.copytree(source, destination, symlinks=True, dirs_exist_ok=True,
                    ignore=ignore, copy_function=copy_file)


def _path_digest(
    path: Path, root: Path | None = None, *, _active: set[Path] | None = None
) -> str:
    try:
        if path.is_symlink():
            link = os.readlink(path)
            if root is None:
                return "link:" + link
            base = Path(root).expanduser().resolve()
            target = Path(link) if Path(link).is_absolute() else path.parent / link
            try:
                resolved = target.resolve(strict=True)
            except OSError:
                return "link-missing:" + link
            if not resolved.is_relative_to(base):
                return "link-outside:" + link
            if resolved.is_file():
                return "link-file:" + link + ":" + hashlib.sha256(resolved.read_bytes()).hexdigest()
            if resolved.is_dir():
                active = _active if _active is not None else set()
                if resolved == base or resolved in active:
                    try:
                        relative = resolved.relative_to(base).as_posix()
                    except ValueError:
                        relative = str(resolved)
                    return "link-cycle:" + link + ":" + relative
                # The current digest root is active while its tree is being
                # walked, including when the caller reached this link through
                # an outer manifest rather than ``_tree_digest`` itself.
                added_base = base not in active
                if added_base:
                    active.add(base)
                try:
                    return "link-dir:" + link + ":" + _tree_digest(resolved, _active=active)
                finally:
                    if added_base:
                        active.discard(base)
            return "link-inside:" + link
        if path.is_file():
            return "file:" + hashlib.sha256(path.read_bytes()).hexdigest()
        if path.is_dir():
            return "dir"
    except OSError:
        return "unreadable"
    return "missing"


def _tree_digest(root: Path, *, _active: set[Path] | None = None) -> str:
    digest = hashlib.sha256()
    root = Path(root)
    if not root.exists():
        return "missing"
    if root.is_file() or root.is_symlink():
        return _path_digest(root, root.parent, _active=_active)
    active = _active if _active is not None else set()
    resolved_root = root.resolve()
    added_root = resolved_root not in active
    if added_root:
        active.add(resolved_root)
    try:
        for base, dirs, files in os.walk(root, followlinks=False):
            link_dirs: list[str] = []
            for name in sorted(dirs):
                if name == ".git":
                    continue
                path = Path(base) / name
                if path.is_symlink():
                    link_dirs.append(name)
            dirs[:] = sorted(name for name in dirs if name != ".git" and name not in link_dirs)
            for name in sorted([*files, *link_dirs]):
                path = Path(base) / name
                relative = path.relative_to(root).as_posix()
                digest.update(relative.encode() + b"\x00")
                digest.update(_path_digest(path, root, _active=active).encode() + b"\x00")
    finally:
        if added_root:
            active.discard(resolved_root)
    return digest.hexdigest()


def _ignored_view_path(relative: str, ignore_prefixes: tuple[str, ...]) -> bool:
    return any(
        relative == prefix.rstrip("/") or relative.startswith(prefix)
        for prefix in ignore_prefixes
    )


def _tree_manifest(
    root: Path, ignore_prefixes: tuple[str, ...] = (".pact/",)
) -> tuple[dict[str, str], set[str]]:
    """Full disposable-view manifest, including ignored and untracked files.

    Git's control directory is compared separately as metadata, and controller
    state under ``.pact/`` stays outside this content scope just as it does for
    ``git status``.  Every other file is hashed so a build that escaped into
    the protected view is visible even when ``git status`` ignores the result.
    """
    root = Path(root)
    files: dict[str, str] = {}
    directories: set[str] = set()
    if not root.exists() and not root.is_symlink():
        return files, directories
    if root.is_file() or root.is_symlink():
        return {root.name: _path_digest(root, root.parent)}, directories
    for base, child_dirs, names in os.walk(root, followlinks=False):
        current = Path(base)
        link_dirs: list[str] = []
        real_dirs: list[str] = []
        for name in sorted(child_dirs):
            path = current / name
            if name == ".git":
                continue
            relative = path.relative_to(root).as_posix()
            if _ignored_view_path(relative, ignore_prefixes):
                continue
            if path.is_symlink():
                link_dirs.append(name)
            else:
                real_dirs.append(name)
                directories.add(relative)
        child_dirs[:] = real_dirs
        for name in sorted([*names, *link_dirs]):
            path = current / name
            if path.name == ".git" and path.is_dir():
                continue
            relative = path.relative_to(root).as_posix()
            if _ignored_view_path(relative, ignore_prefixes):
                continue
            files[relative] = _path_digest(path, root)
    return files, directories


def _full_tree_manifest(root: Path) -> tuple[dict[str, str], set[str]]:
    """Manifest every entry, including Git control files normally skipped above.

    Keys are relative to *root*; a root that is itself a file uses ``""``.  The
    caller prefixes these keys with the protected view's ``.git`` location.
    """
    root = Path(root)
    files: dict[str, str] = {}
    directories: set[str] = set()
    if not root.exists() and not root.is_symlink():
        return files, directories
    if root.is_file() or root.is_symlink():
        files[""] = _path_digest(root, root.parent)
        return files, directories
    for base, child_dirs, names in os.walk(root, followlinks=False):
        current = Path(base)
        link_dirs: list[str] = []
        real_dirs: list[str] = []
        for name in sorted(child_dirs):
            path = current / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                link_dirs.append(name)
            else:
                real_dirs.append(name)
                directories.add(relative)
        child_dirs[:] = real_dirs
        for name in sorted([*names, *link_dirs]):
            path = current / name
            relative = path.relative_to(root).as_posix()
            files[relative] = _path_digest(path, root)
    return files, directories


def _git_view_manifest(root: Path) -> tuple[dict[str, str], set[str]]:
    """The complete ``.git`` entry in the protected view, including lock files."""
    git_path = Path(root) / ".git"
    files, directories = _full_tree_manifest(git_path)
    if not files and not directories:
        return {}, set()
    if "" in files:
        return {".git": files[""]}, set()
    return {f".git/{relative}": digest for relative, digest in files.items()}, {
        f".git/{relative}" for relative in directories
    }


def _manifest_digest(files: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for relative in sorted(files):
        digest.update(relative.encode("utf-8", errors="surrogateescape") + b"\x00")
        digest.update(files[relative].encode("utf-8", errors="surrogateescape") + b"\x00")
    return digest.hexdigest()


def _manifest_change_paths(
    before_files: dict[str, str],
    after_files: dict[str, str],
    before_dirs: set[str],
    after_dirs: set[str],
) -> list[str]:
    changed = {
        *{path for path in set(before_files) | set(after_files) if before_files.get(path) != after_files.get(path)},
        *{path for path in before_dirs ^ after_dirs},
    }
    return sorted(changed)


def _git_control_paths(root: Path, *, deadline: float | None = None) -> dict[str, Path]:
    """Locate HEAD, config and hooks for a normal checkout or linked worktree."""
    git_dir_raw = _git_optional(root, "rev-parse", "--absolute-git-dir", deadline=deadline).strip()
    common_raw = _git_optional(root, "rev-parse", "--git-common-dir", deadline=deadline).strip()
    if not git_dir_raw:
        return {}
    git_dir = Path(git_dir_raw)
    common = Path(common_raw) if common_raw else git_dir
    if not common.is_absolute():
        common = (root / common).resolve()
    paths: dict[str, Path] = {}
    for name in ("HEAD", "config"):
        path = git_dir / name
        if not path.exists() and (common / name).exists():
            path = common / name
        paths[f".git/{name}"] = path
    hooks = git_dir / "hooks"
    if not hooks.exists() and (common / "hooks").exists():
        hooks = common / "hooks"
    paths[".git/hooks"] = hooks
    # Reflogs are rewritten by ordinary Git plumbing during a restore. They are
    # still Git metadata: capture them so the trusted-state comparison is
    # byte-identical instead of failing on our own restore bookkeeping. A
    # linked worktree's .git pointer shares an external git directory, whose
    # reflog is not part of this view and must not be rolled back.
    if (Path(root) / ".git").is_dir():
        logs = git_dir / "logs"
        if not logs.exists() and (common / "logs").exists():
            logs = common / "logs"
        paths[".git/logs"] = logs
    return paths


def _git_metadata_digests(root: Path, *, deadline: float | None = None) -> dict[str, str]:
    """Hash the local Git control files a Reviewer could use to redirect work."""
    result: dict[str, str] = {}
    for name, path in _git_control_paths(root, deadline=deadline).items():
        if path.is_dir():
            result[name] = _tree_digest(path) if path.exists() else "missing"
        else:
            result[name] = _path_digest(path)
    return result


def _git_symlink_digests(root: Path, *, deadline: float | None = None) -> dict[str, str]:
    """Hash tracked symlinks, following only targets that stay inside the tree."""
    output = _git(root, "ls-files", "-s", "-z", deadline=deadline)
    result: dict[str, str] = {}
    for entry in output.encode("utf-8", errors="surrogateescape").split(b"\x00"):
        if not entry or b"\t" not in entry:
            continue
        metadata, raw_path = entry.split(b"\t", 1)
        fields = metadata.split()
        if not fields or fields[0] != b"120000":
            continue
        try:
            relative = raw_path.decode("utf-8")
        except UnicodeError:
            continue
        result[relative] = _path_digest(root / relative, root)
    return result


def _status_paths(raw: str, ignore_prefixes: tuple[str, ...] = (".pact/",)) -> list[str]:
    paths: list[str] = []
    for entry in raw.split("\x00"):
        if len(entry) < 4:
            continue
        path = entry[3:]
        if not path:
            continue
        if any(path == prefix.rstrip("/") or path.startswith(prefix) for prefix in ignore_prefixes):
            continue
        paths.append(path)
    return sorted(set(paths))


def _git_refs(root: Path, *, deadline: float | None = None) -> dict[str, str]:
    output = _git(root, "for-each-ref", "--format=%(refname) %(objectname)", deadline=deadline)
    refs: dict[str, str] = {}
    for line in output.splitlines():
        name, _, value = line.partition(" ")
        if name and value:
            refs[name] = value
    return refs


@dataclass
class _ProtectedTree:
    root: Path
    backup: Path
    use_checkpoint: bool = False
    checkpoint_repo: Path | None = None
    checkpoint_commit: str | None = None
    deadline: float | None = None
    git: bool = False
    head: str = ""
    symbolic_ref: str = ""
    refs: dict[str, str] | None = None
    status: str = ""
    status_paths: set[str] | None = None
    status_digests: dict[str, str] | None = None
    status_missing: set[str] | None = None
    index_path: Path | None = None
    index_backup: Path | None = None
    metadata: dict[str, str] | None = None
    index_entries: str = ""
    symlinks: dict[str, str] | None = None
    metadata_paths: dict[str, Path] | None = None
    metadata_backup: dict[str, Path] | None = None
    ignore_prefixes: tuple[str, ...] = (".pact/",)
    worktree: dict[str, str] | None = None
    worktree_dirs: set[str] | None = None
    tracked_paths: set[str] | None = None
    git_view: dict[str, str] | None = None
    git_view_dirs: set[str] | None = None
    git_view_file_backup: Path | None = None
    digest: str = ""
    dirty: dict[str, Path] | None = None
    captured: bool = False
    missing: bool = False
    restore_error: str = ""

    def capture(self, *, deadline: float | None = None,
                cancelled: threading.Event | None = None) -> dict[str, Any]:
        self.deadline = deadline
        self.root = self.root.expanduser().absolute()
        self.backup.mkdir(parents=True, exist_ok=True)
        self.captured = True
        self.status_missing = set()
        self.dirty = {}
        if not self.root.exists() and not self.root.is_symlink():
            self.missing = True
            self.git = False
            self.worktree, self.worktree_dirs, self.tracked_paths = {}, set(), set()
            self.digest = "missing"
            return self.snapshot()
        self.git = _is_git_tree(self.root, deadline=deadline)
        if self.git:
            self.head = _git(self.root, "rev-parse", "HEAD", deadline=deadline).strip()
            self.symbolic_ref = _git_optional(
                self.root, "symbolic-ref", "-q", "HEAD", deadline=deadline
            ).strip()
            self.refs = _git_refs(self.root, deadline=deadline)
            self.status = _git(
                self.root,
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
                deadline=deadline,
            )
            self.status_paths = set(_status_paths(self.status, self.ignore_prefixes))
            self.status_digests = {
                relative: _path_digest(self.root / relative, self.root)
                for relative in sorted(self.status_paths)
            }
            for index, relative in enumerate(sorted(self.status_paths)):
                source = self.root / relative
                if not (source.is_file() or source.is_symlink()):
                    self.status_missing.add(relative)
                    continue
                target = self.backup / f"{index:05d}"
                target.parent.mkdir(parents=True, exist_ok=True)
                if source.is_symlink():
                    target.symlink_to(os.readlink(source))
                else:
                    shutil.copy2(source, target, follow_symlinks=False)
                self.dirty[relative] = target
            index_raw = _git_optional(
                self.root, "rev-parse", "--git-path", "index", deadline=deadline
            ).strip()
            if index_raw:
                index_path = Path(index_raw)
                if not index_path.is_absolute():
                    index_path = self.root / index_path
                self.index_path = index_path
                if index_path.is_file():
                    self.index_backup = self.backup / "index"
                    shutil.copy2(index_path, self.index_backup)
            self.symlinks = _git_symlink_digests(self.root, deadline=deadline)
            tracked = _git(self.root, "ls-files", "-z", deadline=deadline)
            self.tracked_paths = {path for path in tracked.split("\x00") if path}
            self.index_entries = _git(self.root, "ls-files", "--stage", "-z", deadline=deadline)
            self.worktree, self.worktree_dirs = _tree_manifest(self.root, self.ignore_prefixes)
            self.metadata = _git_metadata_digests(self.root, deadline=deadline)
            self.git_view, self.git_view_dirs = _git_view_manifest(self.root)
            git_view_path = self.root / ".git"
            if git_view_path.is_file() or git_view_path.is_symlink():
                self.git_view_file_backup = self.backup / "git-view-file"
                if git_view_path.is_symlink():
                    self.git_view_file_backup.symlink_to(os.readlink(git_view_path))
                else:
                    shutil.copy2(git_view_path, self.git_view_file_backup, follow_symlinks=False)
            self.metadata_paths = _git_control_paths(self.root, deadline=deadline)
            self.metadata_backup = {}
            for name, path in self.metadata_paths.items():
                if not path.exists():
                    continue
                target = self.backup / "git-control" / name
                target.parent.mkdir(parents=True, exist_ok=True)
                if path.is_dir():
                    shutil.copytree(path, target, symlinks=True, dirs_exist_ok=True)
                else:
                    shutil.copy2(path, target, follow_symlinks=False)
                self.metadata_backup[name] = target
            self.digest = hashlib.sha256(
                json.dumps(
                    {
                        "head": self.head,
                        "refs": self.refs,
                        "status": self.status_digests,
                        "symlinks": self.symlinks,
                        "git_metadata": self.metadata,
                        "index_entries": self.index_entries,
                    },
                    sort_keys=True,
                ).encode()
            ).hexdigest()
        else:
            if self.root.is_dir():
                def copy_file(src: str, dst: str) -> str:
                    with open(src, "rb") as reader, open(dst, "wb") as writer:
                        while chunk := reader.read(1024 * 1024):
                            if cancelled is not None and cancelled.is_set():
                                raise ReviewerExecutionError("cancelled", "protected-tree capture cancelled")
                            _bounded_timeout(deadline, 300.0, stage="capture")
                            writer.write(chunk)
                    shutil.copystat(src, dst, follow_symlinks=False)
                    return dst

                def check_directory(_directory: str, _names: list[str]) -> set[str]:
                    if cancelled is not None and cancelled.is_set():
                        raise ReviewerExecutionError("cancelled", "protected-tree capture cancelled")
                    return set()

                shutil.copytree(self.root, self.backup / "tree", symlinks=True, dirs_exist_ok=True,
                                copy_function=copy_file, ignore=check_directory)
            elif self.root.is_symlink():
                (self.backup / "tree").symlink_to(os.readlink(self.root))
            else:
                shutil.copy2(self.root, self.backup / "tree", follow_symlinks=False)
            self.worktree, self.worktree_dirs = _tree_manifest(self.root, self.ignore_prefixes)
            self.git_view, self.git_view_dirs = _git_view_manifest(self.root)
            self.tracked_paths = set()
            self.digest = _manifest_digest(self.worktree)
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        if self.missing:
            after = "missing" if not self.root.exists() and not self.root.is_symlink() else _tree_digest(self.root)
            changed = after != "missing"
            return {
                "root": str(self.root),
                "kind": "missing",
                "before_sha256": self.digest,
                "after_sha256": after,
                "changed": changed,
                "changed_paths": [str(self.root)] if changed else [],
                "changed_path_count": 1 if changed else 0,
                "changed_paths_truncated": False,
                "checks": {"content_hash": changed, "git_state": False},
                "branch": "content_hash" if changed else "",
                "recoverable": False,
            }
        if not self.git:
            after_files, after_dirs = _tree_manifest(self.root, self.ignore_prefixes)
            after = _manifest_digest(after_files)
            before_files = self.worktree or {}
            added = sorted(set(after_files) - set(before_files))
            removed = sorted(set(before_files) - set(after_files))
            modified = sorted(
                path for path in set(before_files) & set(after_files)
                if before_files[path] != after_files[path]
            )
            after_git_view, after_git_view_dirs = _git_view_manifest(self.root)
            git_view_changed_paths = _manifest_change_paths(
                self.git_view or {}, after_git_view, self.git_view_dirs or set(), after_git_view_dirs
            )
            git_view_changed = bool(git_view_changed_paths)
            content_changed = after != self.digest
            changed_paths = sorted({*added, *removed, *modified, *git_view_changed_paths})
            checks = {"content_hash": content_changed, "git_state": git_view_changed}
            branches = [name for name, changed in checks.items() if changed]
            return {
                "root": str(self.root),
                "kind": "tree",
                "before_sha256": self.digest,
                "after_sha256": after,
                "changed": content_changed or git_view_changed,
                "changed_paths": changed_paths[:_MAX_EVIDENCE_PATHS],
                "changed_path_count": len(changed_paths),
                "changed_paths_truncated": len(changed_paths) > _MAX_EVIDENCE_PATHS,
                "checks": checks,
                "branch": "+".join(branches),
                "recoverable": (
                    bool(added)
                    and not removed
                    and not modified
                    and not git_view_changed
                ),
            }
        head = _git_optional(self.root, "rev-parse", "HEAD", deadline=self.deadline).strip()
        try:
            refs = _git_refs(self.root, deadline=self.deadline)
        except Exception:
            refs = {}
        status = _git_optional(
            self.root,
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            deadline=self.deadline,
        )
        after_paths = set(_status_paths(status, self.ignore_prefixes))
        after_digests = {
            relative: _path_digest(self.root / relative, self.root) for relative in sorted(after_paths)
        }
        symlinks = _git_symlink_digests(self.root, deadline=self.deadline)
        metadata = _git_metadata_digests(self.root, deadline=self.deadline)
        index_entries = _git_optional(
            self.root, "ls-files", "--stage", "-z", deadline=self.deadline
        )
        after = hashlib.sha256(
            json.dumps(
                {
                    "head": head,
                    "refs": refs,
                    "status": after_digests,
                    "symlinks": symlinks,
                    "git_metadata": metadata,
                    "index_entries": index_entries,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        after_files, after_dirs = _tree_manifest(self.root, self.ignore_prefixes)
        after_git_view, after_git_view_dirs = _git_view_manifest(self.root)
        git_view_changed_paths = _manifest_change_paths(
            self.git_view or {}, after_git_view, self.git_view_dirs or set(), after_git_view_dirs
        )
        git_view_changed = bool(git_view_changed_paths)
        before_files = self.worktree or {}
        added = sorted(set(after_files) - set(before_files))
        removed = sorted(set(before_files) - set(after_files))
        modified = sorted(
            path for path in set(before_files) & set(after_files)
            if before_files[path] != after_files[path]
        )
        worktree_changed_paths = {*added, *removed, *modified}
        changed_paths = {
            path
            for path in set(self.status_paths or ()) | after_paths
            if (self.status_digests or {}).get(path) != after_digests.get(path)
        }
        for name in set(self.symlinks or {}) | set(symlinks):
            if (self.symlinks or {}).get(name) != symlinks.get(name):
                changed_paths.add(name)
        for name in set(self.metadata or {}) | set(metadata):
            if (self.metadata or {}).get(name) != metadata.get(name):
                changed_paths.add(name)
        changed_paths.update(worktree_changed_paths)
        changed_paths.update(git_view_changed_paths)
        content_changed = _manifest_digest(after_files) != _manifest_digest(before_files)
        git_state_changed = after != self.digest or git_view_changed
        tracked_changed = bool(worktree_changed_paths & (self.tracked_paths or set()))
        checks = {"content_hash": content_changed, "git_state": git_state_changed}
        branches = [name for name, changed in checks.items() if changed]
        ordered_paths = sorted(changed_paths)
        return {
            "root": str(self.root),
            "kind": "git",
            "before_sha256": self.digest,
            "after_sha256": after,
            "before_worktree_sha256": _manifest_digest(before_files),
            "after_worktree_sha256": _manifest_digest(after_files),
            "before_head": self.head,
            "after_head": head,
            "changed": content_changed or git_state_changed,
            "changed_paths": ordered_paths[:_MAX_EVIDENCE_PATHS],
            "changed_path_count": len(ordered_paths),
            "changed_paths_truncated": len(ordered_paths) > _MAX_EVIDENCE_PATHS,
            "checks": checks,
            "branch": "+".join(branches),
            "recoverable": (
                content_changed
                and not git_state_changed
                and not tracked_changed
                and not git_view_changed
                and bool(added)
                and not removed
                and not modified
            ),
        }

    def _restore_git_metadata(self) -> None:
        for name, source in (self.metadata_backup or {}).items():
            target = (self.metadata_paths or {}).get(name)
            if target is None:
                continue
            if target.exists() or target.is_symlink():
                _remove_strict(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, target, symlinks=True, dirs_exist_ok=True)
            else:
                shutil.copy2(source, target, follow_symlinks=False)

    def _restore_git_view(self) -> None:
        """Reconcile the protected view's ``.git`` entry with the trusted capture.

        Recovery may only proceed when this returns without error.  New lock
        files and other added entries are removed; a modified pointer or an
        entry that cannot be rebuilt from the capture is an untrusted restore.
        """
        if self.git_view is None:
            return
        git_path = self.root / ".git"
        before = self.git_view
        if ".git" in before and not any(name.startswith(".git/") for name in before):
            current = _path_digest(git_path, self.root) if git_path.exists() or git_path.is_symlink() else "missing"
            if current != before[".git"]:
                backup = self.git_view_file_backup
                if backup is None or not (backup.exists() or backup.is_symlink()):
                    raise RuntimeError("modified .git pointer has no trusted backup")
                _remove_strict(git_path)
                if backup.is_symlink():
                    git_path.symlink_to(os.readlink(backup))
                else:
                    shutil.copy2(backup, git_path, follow_symlinks=False)
            return
        after_files, after_dirs = _full_tree_manifest(git_path)
        after = {f".git/{relative}": digest for relative, digest in after_files.items()}
        after_dir_paths = {f".git/{relative}" for relative in after_dirs}
        for relative in sorted(set(after) - set(before), key=lambda value: value.count("/"), reverse=True):
            _remove_strict(git_path / relative.removeprefix(".git/"))
        for relative in sorted(after_dir_paths - (self.git_view_dirs or set()), key=lambda value: value.count("/"), reverse=True):
            try:
                (git_path / relative.removeprefix(".git/")).rmdir()
            except OSError:
                pass
        # Known control files were restored by the caller before this check.
        after_files, _ = _full_tree_manifest(git_path)
        after = {f".git/{relative}": digest for relative, digest in after_files.items()}
        mismatched = sorted(
            path for path in set(before) | set(after) if before.get(path) != after.get(path)
        )
        if mismatched:
            raise RuntimeError(
                "protected .git metadata differs from the trusted capture: "
                + ", ".join(mismatched[:20])
            )

    def _restore_from_checkpoint(self) -> None:
        if self.checkpoint_repo is None or not self.checkpoint_commit:
            raise RuntimeError("no controller candidate checkpoint is available")
        self.root.mkdir(parents=True, exist_ok=True)
        payload = _run(
            [
                "git",
                "-C",
                str(self.checkpoint_repo),
                "archive",
                "--format=tar",
                self.checkpoint_commit,
            ],
            deadline=self.deadline,
            stage="restore",
        )
        for child in list(self.root.iterdir()):
            if child.name == ".git":
                continue
            _remove_strict(child)
        _run(
            ["tar", "-x", "-C", str(self.root)],
            input_bytes=payload,
            deadline=self.deadline,
            stage="restore",
        )
        self._restore_git_metadata()
        if self.index_backup is not None and self.index_path is not None:
            self.index_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.index_backup, self.index_path)
        # The checkpoint tar contains only worktree content.  Git metadata in
        # the view is reconciled against the trusted pre-turn capture, never
        # carried over from a possibly mutated .git.
        self._restore_git_view()

    def restore(self) -> str:
        """Restore the captured state; return an error string on failure."""
        self.restore_error = ""
        try:
            if self.missing:
                if self.root.exists() or self.root.is_symlink():
                    _remove_strict(self.root)
                return ""
            if self.use_checkpoint and self.checkpoint_repo is not None and self.checkpoint_commit:
                self._restore_from_checkpoint()
                return ""
            if not self.git:
                backup_tree = self.backup / "tree"
                if not backup_tree.exists() and not backup_tree.is_symlink():
                    raise RuntimeError("protected backup is missing")
                if self.root.exists() or self.root.is_symlink():
                    if self.root.is_dir() and not self.root.is_symlink():
                        for child in list(self.root.iterdir()):
                            _remove_strict(child)
                    else:
                        _remove_strict(self.root)
                if backup_tree.is_dir() and not backup_tree.is_symlink():
                    shutil.copytree(backup_tree, self.root, symlinks=True, dirs_exist_ok=True)
                elif backup_tree.is_symlink():
                    self.root.symlink_to(os.readlink(backup_tree))
                else:
                    shutil.copy2(backup_tree, self.root, follow_symlinks=False)
                return ""
            # Restore Git control state and the exact captured index/worktree,
            # including pre-existing deletions and staged changes.
            if self.symbolic_ref:
                _git(self.root, "symbolic-ref", "HEAD", self.symbolic_ref, deadline=self.deadline)
            else:
                _git(
                    self.root,
                    "update-ref",
                    "--no-deref",
                    "HEAD",
                    self.head,
                    deadline=self.deadline,
                )
            current = _git_refs(self.root, deadline=self.deadline)
            for name in sorted(set(current) - set(self.refs or {})):
                _git(self.root, "update-ref", "-d", name, deadline=self.deadline)
            for name, value in sorted((self.refs or {}).items()):
                if current.get(name) != value:
                    _git(self.root, "update-ref", name, value, deadline=self.deadline)
            self._restore_git_metadata()
            if self.index_backup is not None and self.index_path is not None:
                self.index_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(self.index_backup, self.index_path)
            _git(
                self.root,
                "checkout-index",
                "-a",
                "-f",
                deadline=self.deadline,
            )
            current_status = _git(
                self.root,
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
                deadline=self.deadline,
            )
            before_paths = set(self.status_paths or ())
            for relative in _status_paths(current_status, self.ignore_prefixes):
                if relative not in before_paths:
                    _remove_strict(self.root / relative)
            for relative in sorted(before_paths):
                target = self.root / relative
                if relative in (self.status_missing or ()) or relative not in (self.dirty or {}):
                    _remove_strict(target)
                    continue
                source = (self.dirty or {})[relative]
                target.parent.mkdir(parents=True, exist_ok=True)
                _remove_strict(target)
                if source.is_symlink():
                    target.symlink_to(os.readlink(source))
                else:
                    shutil.copy2(source, target, follow_symlinks=False)
            # Ignored/untracked build products are invisible to git status.
            # Remove every path that did not exist in the captured view.
            current_files, current_dirs = _tree_manifest(self.root, self.ignore_prefixes)
            before_files = self.worktree or {}
            for relative in sorted(set(current_files) - set(before_files)):
                _remove_strict(self.root / relative)
            for relative in sorted((current_dirs or set()) - (self.worktree_dirs or set()), reverse=True):
                try:
                    (self.root / relative).rmdir()
                except OSError:
                    pass
            if self.index_backup is not None and self.index_path is not None:
                self.index_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(self.index_backup, self.index_path)
            self._restore_git_view()
            return ""
        except Exception as exc:
            self.restore_error = f"{type(exc).__name__}: {exc}"[:1000]
            return self.restore_error


class _ProtectedTreeGroup:
    def __init__(
        self,
        roots: Iterable[Path],
        call_root: Path,
        *,
        checkpoint: tuple[Path, str] | None = None,
        checkpoint_target: Path | None = None,
        deadline: float | None = None,
    ) -> None:
        unique: list[Path] = []
        seen: set[Path] = set()
        for root in roots:
            path = Path(root).expanduser().absolute()
            if path in seen:
                continue
            seen.add(path)
            unique.append(path)
        target = (
            Path(checkpoint_target).expanduser().absolute()
            if checkpoint_target is not None
            else None
        )
        repo = Path(checkpoint[0]).expanduser().absolute() if checkpoint else None
        commit = checkpoint[1] if checkpoint else None
        self.trees = [
            _ProtectedTree(
                path,
                call_root / "protected" / f"{index:03d}",
                use_checkpoint=target is not None and path == target,
                checkpoint_repo=repo,
                checkpoint_commit=commit,
                deadline=deadline,
            )
            for index, path in enumerate(unique)
        ]
        self.completed_trees: list[_ProtectedTree] = []

    def capture(self, *, deadline: float | None = None,
                cancelled: threading.Event | None = None) -> None:
        for tree in self.trees:
            tree.capture(deadline=deadline, cancelled=cancelled)
            self.completed_trees.append(tree)

    def violations(self) -> list[dict[str, Any]]:
        # A cancelled capture may have only a partial backup. Comparing it
        # would manufacture a mutation and could restore from incomplete data.
        return [row for row in (tree.snapshot() for tree in self.completed_trees) if row.get("changed")]

    def restore(self) -> tuple[bool, list[dict[str, str]]]:
        errors: list[dict[str, str]] = []
        for tree in self.completed_trees:
            try:
                error = tree.restore()
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"[:1000]
                tree.restore_error = error
            if error:
                errors.append({"root": str(tree.root), "error": error})
        return not errors, errors


def _remove(path: Path) -> None:
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
    except OSError:
        pass


def _remove_strict(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _darwin_process_rows() -> dict[int, tuple[int, int]]:
    """Use libproc when a sandbox does not permit executing ps."""
    import ctypes

    rows: dict[int, tuple[int, int]] = {}
    try:
        library = ctypes.CDLL("/usr/lib/libproc.dylib")
        size = library.proc_listpids(1, 0, None, 0)
        if size <= 0:
            return rows
        pids = (ctypes.c_int * (size // ctypes.sizeof(ctypes.c_int) + 128))()
        count = library.proc_listpids(1, 0, pids, ctypes.sizeof(pids)) // ctypes.sizeof(ctypes.c_int)
        info = (ctypes.c_uint32 * 16)()
        for pid in pids[:count]:
            if pid > 0 and library.proc_pidinfo(pid, 13, 0, info, ctypes.sizeof(info)) >= 12:
                rows[pid] = (int(info[1]), int(info[2]))
    except (OSError, ValueError):
        return rows
    return rows


def _darwin_uses_scratch(pid: int, scratch: Path) -> bool:
    import ctypes

    try:
        library = ctypes.CDLL("/usr/lib/libproc.dylib")
        info = ctypes.create_string_buffer(8192)
        if library.proc_pidinfo(pid, 9, 0, info, ctypes.sizeof(info)) <= 0:
            return False
        needle = os.fsencode(str(scratch))
        return any(path == needle or path.startswith(needle + b"/")
                   for path in info.raw.split(b"\0") if path)
    except (OSError, ValueError):
        return False


def _process_rows() -> dict[int, tuple[int, int]]:
    rows: dict[int, tuple[int, int]] = {}
    try:
        entries = os.listdir("/proc")
    except OSError:
        if sys.platform == "darwin":
            try:
                result = subprocess.run(["ps", "-axo", "pid=,ppid=,pgid="], capture_output=True,
                                        text=True, check=False, env=current_environment())
            except OSError:
                return _darwin_process_rows()
            if result.returncode != 0:
                return _darwin_process_rows()
            for line in result.stdout.splitlines():
                try:
                    pid, parent, group = (int(value) for value in line.split())
                    rows[pid] = (parent, group)
                except ValueError:
                    continue
        return rows
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            stat = Path("/proc") / entry / "stat"
            text = stat.read_text(encoding="utf-8")
            fields = text[text.rindex(")") + 2 :].split()
            rows[pid] = (int(fields[1]), int(fields[2]))
        except (OSError, ValueError, IndexError):
            continue
    return rows


def _descendants(roots: Iterable[int], rows: dict[int, tuple[int, int]]) -> set[int]:
    found: set[int] = set()
    pending = list(roots)
    while pending:
        current = pending.pop()
        if current in found:
            continue
        found.add(current)
        pending.extend(pid for pid, (parent, _group) in rows.items() if parent == current)
    return found


def _uses_scratch(pid: int, scratch: Path) -> bool:
    needle = str(scratch).encode()

    def inside(value: bytes) -> bool:
        return value == needle or value.startswith(needle + os.fsencode(os.sep))

    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["lsof", "-a", "-d", "cwd", "-p", str(pid), "-Fn"],
                capture_output=True, text=True, check=False, timeout=2, env=current_environment(),
            )
        except (OSError, subprocess.TimeoutExpired):
            return _darwin_uses_scratch(pid, scratch)
        if result.returncode != 0:
            return _darwin_uses_scratch(pid, scratch)
        return any(line.startswith("n") and inside(os.fsencode(line[1:]))
                   for line in result.stdout.splitlines())
    proc = Path("/proc") / str(pid)
    try:
        if inside(os.fsencode(os.readlink(proc / "cwd"))):
            return True
    except OSError:
        pass
    try:
        if needle in (proc / "cmdline").read_bytes():
            return True
    except OSError:
        pass
    try:
        for row in (proc / "environ").read_bytes().split(b"\0"):
            key, separator, value = row.partition(b"=")
            if separator and key in (b"PWD", b"TMPDIR", b"TMP", b"TEMP") and inside(value):
                return True
    except OSError:
        pass
    return False


def _process_start_time(pid: int) -> int | None:
    """The process start time field (22) that makes a PID identity, not just a number."""
    try:
        text = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8")
        fields = text[text.rindex(")") + 2 :].split()
        return int(fields[19])
    except (OSError, ValueError, IndexError):
        return None


def _terminate(pid: int, *, protected_groups: set[int] | None = None) -> bool:
    """Stop *pid*'s group, re-verifying the PID's start time before every signal."""
    if pid == os.getpid():
        return False
    started = _process_start_time(pid)
    try:
        group = os.getpgid(pid)
    except (ProcessLookupError, PermissionError):
        group = None
    if started is None:
        return False
    for number, grace in ((signal.SIGTERM, _TERM_GRACE_SECONDS), (signal.SIGKILL, _KILL_WAIT_SECONDS)):
        if _process_start_time(pid) != started:
            return True
        try:
            if group is not None and group != os.getpgrp() and group not in (protected_groups or set()):
                os.killpg(group, number)
            else:
                os.kill(pid, number)
        except (ProcessLookupError, PermissionError):
            return True
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            if _process_start_time(pid) != started:
                return True
            try:
                if sys.platform == "linux":
                    state = (Path("/proc") / str(pid) / "stat").read_text()
                    if state[state.rindex(")") + 2 :].split()[0] == "Z":
                        return True
                os.kill(pid, 0)
            except (ProcessLookupError, PermissionError, OSError, IndexError):
                return True
            time.sleep(0.02)
    return _process_start_time(pid) != started


def terminate_reviewer_processes(
    before: dict[int, tuple[int, int]],
    roots: Iterable[int],
    scratch: Path,
    *,
    roots_shared: bool = True,
) -> set[int]:
    """Stop the processes of a per-call Reviewer process, if this turn owns one.

    ``roots`` may name a shared Reviewer process (for example one ACP Reviewer
    used by several concurrent proposals). A shared process has no per-call
    descendants: a sibling proposal's tools live in the same process tree, and
    a restarted root is not proof that every new descendant belongs to this
    turn. Callers that report shared roots therefore leave them completely
    alone here; the process owner cleans all descendants when that process
    closes.

    A non-shared Reviewer starts and exits within the call. Its reported roots
    are explicitly owned by this turn, so they and their descendants are
    stopped even when they predate the call or never enter its scratch view.
    The scratch/group discovery below still catches detached tools, so a tool
    cannot outlive the turn.
    """
    root_pids = set(roots)
    if roots_shared and root_pids:
        return set()
    after = _process_rows()
    started = {pid for pid in after if pid not in before}
    scratch_pids = {pid for pid in started if _uses_scratch(pid, scratch)}
    owned_groups = {after[pid][1] for pid in scratch_pids if after[pid][1] != os.getpgrp()}
    candidates = set(scratch_pids)
    candidates.update(_descendants(scratch_pids, after).intersection(started))
    candidates.update(pid for pid in started if after[pid][1] in owned_groups)
    if not roots_shared:
        live_roots = root_pids.intersection(after)
        candidates.update(live_roots)
        candidates.update(_descendants(live_roots, after))
    candidates.discard(os.getpid())
    # Kill children before their parents so a shell cannot respawn them.
    ordered = sorted(candidates, key=lambda pid: len(_descendants([pid], after)), reverse=True)
    remaining: set[int] = set()
    for pid in ordered:
        if not _terminate(pid):
            remaining.add(pid)
    return remaining


def terminate_reviewer_descendants(root_pid: int) -> set[int]:
    """Clean every descendant of a shared Reviewer process before it closes."""
    after = _process_rows()
    if root_pid not in after:
        return set()
    descendants = _descendants([root_pid], after)
    descendants.discard(root_pid)
    descendants.discard(os.getpid())
    protected_groups = {after[root_pid][1], os.getpgrp()}
    ordered = sorted(descendants, key=lambda pid: len(_descendants([pid], after)), reverse=True)
    remaining: set[int] = set()
    for pid in ordered:
        if not _terminate(pid, protected_groups=protected_groups):
            remaining.add(pid)
    return remaining


class NonDegradableIntegrityError(RuntimeError):
    """An integrity failure that no broad degradation path may swallow."""


class RecoveredViewIntegrityError(NonDegradableIntegrityError):
    """A restored protected view changed again; no further recovery is allowed."""


SIDE_CALL_INTEGRITY_STATE_SCHEMA = "long-horizon/side-call-integrity-state/v1"


@dataclass(frozen=True, slots=True)
class _RecoveredViewGuard:
    roots: tuple[Path, ...]
    identity: str
    fingerprints: tuple[tuple[str, str], ...] = ()


_RECOVERED_VIEWS: dict[tuple[str, ...], _RecoveredViewGuard] = {}
_RECOVERED_VIEWS_LOCK = threading.RLock()
_RECOVERY_STATE_PATH: Path | None = None
_RECOVERY_STATE_WORK_ID: str | None = None
_RECOVERY_STATE_COUNT = 0
_RECOVERY_EVENTS: list[dict[str, Any]] = []
_MAX_RECOVERY_EVENTS = 100


def _roots_overlap(left: Iterable[Path], right: Iterable[Path]) -> bool:
    for a in left:
        for b in right:
            if a == b or a.is_relative_to(b) or b.is_relative_to(a):
                return True
    return False


def _root_fingerprint(root: Path) -> str:
    # Imported lazily: side_calls imports this module for its wrapper types.
    from types import SimpleNamespace

    from .side_calls import view_identity

    snapshot = SimpleNamespace(
        roots=(SimpleNamespace(view_root=str(Path(root).expanduser().absolute())),)
    )
    return view_identity(snapshot, ignore_git_index=False)


def _fingerprints_for_roots(roots: Iterable[Path]) -> tuple[tuple[str, str], ...]:
    """Canonical ``(root, digest)`` pairs, independent of caller order."""
    unique = sorted({Path(root).expanduser().absolute() for root in roots}, key=str)
    return tuple((str(root), _root_fingerprint(root)) for root in unique)


def _identity_from_fingerprints(rows: Iterable[tuple[str, str]]) -> str:
    canonical = [
        {"root": str(root), "digest": str(digest)}
        for root, digest in sorted(rows, key=lambda row: str(row[0]))
    ]
    return json.dumps(canonical, sort_keys=True, separators=(",", ":"))


def _fingerprints_from_identity(identity: str) -> tuple[tuple[str, str], ...]:
    try:
        rows = json.loads(identity)
    except (TypeError, ValueError):
        return ()
    if not isinstance(rows, list):
        return ()
    result: list[tuple[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            return ()
        root, digest = row.get("root"), row.get("digest")
        if not isinstance(root, str) or not isinstance(digest, str):
            return ()
        result.append((root, digest))
    return tuple(sorted(result, key=lambda row: row[0]))


def recovered_view_identity(roots: Iterable[Path]) -> str:
    """Full protected-view identity as canonical sorted ``(root, digest)`` pairs."""
    return _identity_from_fingerprints(_fingerprints_for_roots(roots))


def _guard_fingerprints(guard: _RecoveredViewGuard) -> tuple[tuple[str, str], ...]:
    return (
        guard.fingerprints
        or _fingerprints_from_identity(guard.identity)
        or _fingerprints_for_roots(guard.roots)
    )


def _present_guard_fingerprints(guard: _RecoveredViewGuard) -> tuple[tuple[str, str], ...]:
    """The guard's fingerprints whose root still exists.

    A root that was removed in full is a destroyed view, not a content change.
    The guard follows the roots that remain, and is released when none do.
    """
    return tuple(
        sorted(
            (
                (root, digest)
                for root, digest in _guard_fingerprints(guard)
                if Path(root).exists() or Path(root).is_symlink()
            ),
            key=lambda row: row[0],
        )
    )


def _guard_view_removed(guard: _RecoveredViewGuard) -> bool:
    return not _present_guard_fingerprints(guard)


def _guard_changed(guard: _RecoveredViewGuard) -> bool:
    """Compare only roots that still exist; a fully missing root is not tamper."""
    present = _present_guard_fingerprints(guard)
    if not present:
        return False
    expected = _identity_from_fingerprints(present)
    current = recovered_view_identity(Path(root) for root, _digest in present)
    return current != expected


def _release_guard(guard: _RecoveredViewGuard, *, event: str | None = None) -> bool:
    """Unregister exactly *guard*, optionally recording a lifecycle event."""
    key = tuple(str(root) for root in guard.roots)
    with _RECOVERED_VIEWS_LOCK:
        if _RECOVERED_VIEWS.get(key) is not guard:
            return False
        del _RECOVERED_VIEWS[key]
        if event is not None:
            _RECOVERY_EVENTS.append(
                {
                    "event": event,
                    "roots": [str(root) for root in guard.roots],
                    "at_unix": time.time(),
                }
            )
            del _RECOVERY_EVENTS[:-_MAX_RECOVERY_EVENTS]
        _persist_recovery_state_locked()
        return True


def side_call_integrity_state_path(workspace: Path | str, work_id: str) -> Path:
    """The work-owned file that survives controller restart/resume."""
    safe_work_id = str(work_id).replace("/", "_").replace("\\", "_") or "work"
    return (
        Path(workspace).expanduser().absolute()
        / ".pact"
        / "side-call-integrity"
        / f"{safe_work_id}.json"
    )


def _persist_recovery_state_locked() -> None:
    path = _RECOVERY_STATE_PATH
    if path is None:
        return
    guards: list[dict[str, object]] = []
    for guard in _RECOVERED_VIEWS.values():
        fingerprints = (
            guard.fingerprints
            or _fingerprints_from_identity(guard.identity)
            or _fingerprints_for_roots(guard.roots)
        )
        guards.append(
            {
                "roots": [str(root) for root in guard.roots],
                "fingerprints": [
                    {"root": str(root), "digest": str(digest)} for root, digest in fingerprints
                ],
            }
        )
    write_json_atomic(
        path,
        {
            "schema": SIDE_CALL_INTEGRITY_STATE_SCHEMA,
            "work_id": _RECOVERY_STATE_WORK_ID,
            "recoveries": _RECOVERY_STATE_COUNT,
            "guards": guards,
            "events": list(_RECOVERY_EVENTS),
        },
    )


def configure_side_call_integrity_state(
    path: Path | str | None,
    *,
    work_id: str | None = None,
    controller: Any = None,
    plugin: Any = None,
) -> int:
    """Load the work-owned recovery count and guards on controller start/resume."""
    global _RECOVERY_STATE_PATH, _RECOVERY_STATE_WORK_ID, _RECOVERY_STATE_COUNT
    with _RECOVERED_VIEWS_LOCK:
        _RECOVERY_STATE_PATH = Path(path).expanduser().absolute() if path is not None else None
        _RECOVERY_STATE_WORK_ID = work_id
        _RECOVERY_STATE_COUNT = 0
        _RECOVERED_VIEWS.clear()
        _RECOVERY_EVENTS.clear()
        if _RECOVERY_STATE_PATH is not None and _RECOVERY_STATE_PATH.is_file():
            payload = json.loads(_RECOVERY_STATE_PATH.read_text(encoding="utf-8"))
            _RECOVERY_STATE_COUNT = max(0, int(payload.get("recoveries") or 0))
            _RECOVERY_EVENTS.extend(
                row
                for row in payload.get("events", [])
                if isinstance(row, dict) and isinstance(row.get("event"), str)
            )
            for row in payload.get("guards", []):
                if not isinstance(row, dict):
                    continue
                roots = tuple(
                    sorted(
                        {
                            Path(str(root)).expanduser().absolute()
                            for root in row.get("roots", [])
                            if str(root).strip()
                        },
                        key=str,
                    )
                )
                if not roots:
                    continue
                fingerprints = tuple(
                    sorted(
                        (
                            (str(item["root"]), str(item["digest"]))
                            for item in row.get("fingerprints", [])
                            if isinstance(item, dict) and "root" in item and "digest" in item
                        ),
                        key=lambda item: item[0],
                    )
                )
                identity = (
                    _identity_from_fingerprints(fingerprints)
                    if fingerprints
                    else str(row.get("identity") or "")
                )
                _RECOVERED_VIEWS[tuple(str(root) for root in roots)] = _RecoveredViewGuard(
                    roots, identity, fingerprints
                )
        owners: list[Any] = []
        for owner in (controller, plugin, getattr(controller, "_plugin", None) if controller is not None else None):
            if owner is not None and owner not in owners:
                owners.append(owner)
        for owner in owners:
            setattr(owner, "_side_call_integrity_recoveries", _RECOVERY_STATE_COUNT)
            setattr(owner, "_side_call_integrity_state_path", _RECOVERY_STATE_PATH)
        return _RECOVERY_STATE_COUNT


def clear_side_call_integrity_state() -> None:
    """Forget in-process recovery state (tests and controller teardown)."""
    configure_side_call_integrity_state(None)


def record_side_call_recovery(count: int) -> None:
    """Persist an accepted recovery count so a resumed controller cannot reset it."""
    global _RECOVERY_STATE_COUNT
    with _RECOVERED_VIEWS_LOCK:
        _RECOVERY_STATE_COUNT = max(_RECOVERY_STATE_COUNT, int(count))
        _persist_recovery_state_locked()


def register_recovered_view(roots: Iterable[Path], identity: str) -> None:
    """Remember the exact restored state that every later use must still match."""
    unique = tuple(sorted({Path(root).expanduser().absolute() for root in roots}, key=str))
    if not unique:
        return
    fingerprints = _fingerprints_from_identity(identity)
    guard = _RecoveredViewGuard(unique, identity, fingerprints)
    with _RECOVERED_VIEWS_LOCK:
        _RECOVERED_VIEWS[tuple(str(root) for root in unique)] = guard
        _persist_recovery_state_locked()


def revalidate_recovered_view(roots: Iterable[Path], *, expected: str, quiet_seconds: float) -> tuple[bool, str]:
    """Wait out the quiet period, then prove the restored view has not changed since."""
    if quiet_seconds > 0:
        time.sleep(quiet_seconds)
    current = recovered_view_identity(roots)
    return current == expected, current


def verify_recovered_views(roots: Iterable[Path] | None = None) -> None:
    """Hard-fail if any restored view changed before its next permitted use.

    Only guards that are still registered participate. Handing a view back to a
    legitimate writer (or destroying/replacing it) removes its guard first, so
    ordinary Worker writes after a successful recovery are not violations.
    """
    requested = tuple(Path(root).expanduser().absolute() for root in roots) if roots is not None else ()
    with _RECOVERED_VIEWS_LOCK:
        guards = list(_RECOVERED_VIEWS.values())
    for guard in guards:
        if requested and not _roots_overlap(guard.roots, requested):
            continue
        if _guard_view_removed(guard):
            _release_guard(guard, event="guard_released_view_removed")
            continue
        if _guard_changed(guard):
            # Keep the guard: every later use and every export must keep failing
            # until an operator explicitly discards the Work.
            raise RecoveredViewIntegrityError(
                "a recovered protected view changed again after its verified quiet period; "
                "the run cannot continue or recover"
            )


def _released_guard_keys(roots: Iterable[Path]) -> list[tuple[str, ...]]:
    requested = tuple(Path(root).expanduser().absolute() for root in roots)
    if not requested:
        return []
    with _RECOVERED_VIEWS_LOCK:
        return [
            key
            for key, guard in _RECOVERED_VIEWS.items()
            if _roots_overlap(guard.roots, requested)
        ]


def release_recovered_views(roots: Iterable[Path], *, validate: bool = True) -> None:
    """End guards for a view that is being handed back or replaced.

    The final validation happens *before* the handoff. A mismatch is the same
    hard failure as any other post-recovery change; when it matches, the guard
    is unregistered so the legitimate writer that follows is unconstrained.
    Destructive callers pass ``validate=False`` because the old view no longer
    exists to compare.
    """
    keys = _released_guard_keys(roots)
    if not keys:
        return
    with _RECOVERED_VIEWS_LOCK:
        guards = [(key, _RECOVERED_VIEWS[key]) for key in keys if key in _RECOVERED_VIEWS]
    if validate:
        for _key, guard in guards:
            if _guard_view_removed(guard):
                _release_guard(guard, event="guard_released_view_removed")
                continue
            if _guard_changed(guard):
                raise RecoveredViewIntegrityError(
                    "a recovered protected view changed before it was handed back to a "
                    "legitimate writer; the run cannot continue or recover"
                )
    with _RECOVERED_VIEWS_LOCK:
        removed = False
        for key, guard in guards:
            if _RECOVERED_VIEWS.get(key) is guard:
                del _RECOVERED_VIEWS[key]
                removed = True
        if removed:
            _persist_recovery_state_locked()


def discard_recovered_views(roots: Iterable[Path]) -> None:
    """Forget guards whose view is being destroyed or replaced."""
    release_recovered_views(roots, validate=False)


def _environment_target(native: Any) -> Any:
    """Find the adapter that can receive a per-turn environment through wrappers."""
    target = native
    seen: set[int] = set()
    while target is not None and id(target) not in seen:
        seen.add(id(target))
        if callable(getattr(target, "set_turn_environment", None)):
            return target
        child = getattr(target, "native", None)
        if child is None or child is target:
            break
        target = child
    return None


def _apply_adapter_environment(native: Any, environment: dict[str, str]) -> Any:
    target = _environment_target(native)
    if target is not None:
        target.set_turn_environment(environment)
    return target


def _clear_adapter_environment(target: Any) -> None:
    clear = getattr(target, "clear_turn_environment", None)
    if callable(clear):
        clear()


class IsolatedExecutableReviewer:
    """Wrap a Reviewer adapter with a disposable executable checkout.

    Worker turns pass through unchanged.  Reviewer requests are redirected to a
    private copy; the real protected roots are hashed after Reviewer processes
    are stopped, and any change discards the verdict.  Isolation failures are
    ordinary failed review turns, never controller-level runtime errors.
    """

    def __init__(
        self,
        native: Any,
        *,
        root: Path,
        protected_roots: Iterable[Path] = (),
        limits: ReviewerExecutionLimits | None = None,
        scratch_namespace: str | None = None,
        checkpoint: tuple[Path, str] | Path | str | None = None,
        checkpoint_repo: Path | None = None,
        checkpoint_provider: Callable[[], tuple[Path, str] | None] | None = None,
        require_read_only_copy: bool = False,
        launch_policy: ReviewerLaunchPolicy | None = None,
    ) -> None:
        self.native = native
        self.root = Path(root).expanduser().absolute()
        self.protected_roots = tuple(Path(path).expanduser().absolute() for path in protected_roots if path)
        self.limits = limits or reviewer_execution_limits()
        self.checkpoint = checkpoint
        self.checkpoint_repo = Path(checkpoint_repo).expanduser().absolute() if checkpoint_repo else None
        self.checkpoint_provider = checkpoint_provider
        # Retained for callers of the earlier read-only-copy contract.  The
        # copy is disposable, so writes inside it are allowed and are not
        # compared or reported as integrity violations.
        self.require_read_only_copy = bool(require_read_only_copy)
        self.launch_policy = launch_policy
        self.scratch_namespace = scratch_namespace
        self._cancel_requested = threading.Event()
        self._cancel_condition = threading.Condition()
        self._cancel_inflight = 0
        self._turn_active = False
        self._has_run = False
        self.candidate_untrusted = False
        self.last_failure: dict[str, Any] | None = None
        self.last_violation: dict[str, Any] | None = None
        self.processes_terminated = True
        self.integrity_failures: list[dict[str, Any]] = []

    @property
    def id(self) -> str:
        return "isolated-executable-" + str(getattr(self.native, "id", "reviewer"))

    @property
    def budget_failure(self) -> str | None:
        return self.native.budget_failure

    @property
    def configuration_failure(self) -> str | None:
        return self.native.configuration_failure

    def probe(self) -> Any:
        return self.native.probe()

    async def cancel_current_turn(self) -> None:
        with self._cancel_condition:
            if not self._turn_active and self._has_run:
                return
            self._cancel_requested.set()
            self._cancel_inflight += 1
        try:
            await self.native.cancel_current_turn()
        finally:
            with self._cancel_condition:
                self._cancel_inflight -= 1
                if self._cancel_inflight == 0 and not self._turn_active and self._has_run:
                    self._cancel_requested = threading.Event()
                self._cancel_condition.notify_all()

    def _peripheral_deadline(self, deadline: float | None) -> float | None:
        if deadline is None:
            return None
        remaining = max(0.0, float(deadline) - time.time())
        reserve = min(self.limits.finish_reserve_seconds, remaining * 0.5)
        return float(deadline) - reserve

    def _checkpoint_identity(self, request: TurnRequest) -> tuple[Path, str] | None:
        value: Any = None
        if self.checkpoint_provider is not None:
            try:
                value = self.checkpoint_provider()
            except Exception:
                value = None
        elif self.checkpoint is not None:
            value = self.checkpoint
        if isinstance(value, tuple) and len(value) == 2:
            repo, commit = value
            if repo and commit:
                return Path(repo).expanduser().absolute(), str(commit)
        elif value is not None and self.checkpoint_repo is not None:
            return self.checkpoint_repo, str(value)
        if request.workdir is None:
            return None
        workdir = Path(request.workdir)
        try:
            if not _is_git_tree(workdir, deadline=request.deadline):
                return None
            head = _git(workdir, "rev-parse", "HEAD", deadline=request.deadline).strip()
        except Exception:
            return None
        return (workdir, head) if head else None

    def _process_roots(self) -> tuple[int, ...]:
        for name in ("reviewer_process_roots", "process_roots"):
            provider = getattr(self.native, name, None)
            if callable(provider):
                try:
                    return tuple(int(pid) for pid in provider())
                except Exception:
                    return ()
        return ()

    def _deadline_shortfall(self, deadline: float | None, started: float) -> str | None:
        if deadline is None:
            return None
        remaining = float(deadline) - time.time()
        if remaining <= 0:
            return "reviewer deadline reached before the isolated turn"
        reserve = min(self.limits.finish_reserve_seconds, remaining * 0.5)
        elapsed = max(0.0, time.time() - started)
        overhead = max(REVIEWER_PERIPHERAL_RESERVE_SECONDS, elapsed * 1.5)
        if remaining <= reserve + overhead:
            return (
                "reviewer deadline skipped: insufficient remaining trial budget for peripheral isolation "
                f"overhead and finish reserve (remaining {remaining:.1f}s, reserve "
                f"{reserve:.1f}s, estimated overhead {overhead:.1f}s)"
            )
        return None

    def run_turn(self, request: TurnRequest) -> TurnResult:
        if request.role != "reviewer" or request.workdir is None:
            if request.role == "worker" and request.workdir is not None:
                # The view is being handed back to its legitimate writer.
                release_recovered_views((request.workdir, *self.protected_roots))
            return self.native.run_turn(request)
        with self._cancel_condition:
            while self._cancel_inflight:
                self._cancel_condition.wait()
            if self._turn_active:
                raise RuntimeError("concurrent turns on one reviewer instance")
            self._turn_active = True
            self._has_run = True
        try:
            protected_roots = protected_view_roots(request.workdir, self.protected_roots)
            resolved_roots = tuple(Path(path).expanduser().resolve() for path in protected_roots)
            for protected_root in resolved_roots:
                if self.root.resolve() == protected_root or self.root.resolve().is_relative_to(protected_root):
                    raise ReviewerProtectionError(f"reviewer scratch root overlaps a protected view: {protected_root}")
            # A recovered view is only a loan: prove it still matches before this
            # next reviewer turn is allowed to use it.
            verify_recovered_views(resolved_roots)
            if self.launch_policy is not None:
                self.launch_policy.protect_views(*resolved_roots)
            call_id = self.scratch_namespace or uuid4().hex
            candidate = reviewer_scratch_path(self.root, call_id)
            inherited = current_environment()
            overlay = {
                **reviewer_environment(),
                **reviewer_scratch_environment(candidate),
            }
            if self.launch_policy is None or self.launch_policy.mode != "uid_drop":
                overlay = guarded_environment(overlay, self.root / "process-kill-guard",
                                              base_environment=inherited)
            with turn_environment(overlay, base=inherited):
                return self._run_isolated_turn(request, resolved_roots, call_id)
        finally:
            with self._cancel_condition:
                self._turn_active = False
                if self._cancel_inflight == 0:
                    self._cancel_requested = threading.Event()
                self._cancel_condition.notify_all()

    def _run_isolated_turn(
        self, request: TurnRequest, protected_roots: tuple[Path, ...], call_id: str
    ) -> TurnResult:
        call_root = self.root / "reviewer-scratch" / call_id
        candidate = call_root / "candidate"
        guard_before = recovered_view_identity(protected_roots)
        evidence_root = self.root / "reviewer-mutation-evidence"
        started = time.time()
        checkpoint = self._checkpoint_identity(request)
        protected = _ProtectedTreeGroup(
            protected_roots,
            call_root,
            checkpoint=checkpoint,
            checkpoint_target=Path(request.workdir).expanduser().absolute(),
            deadline=request.deadline,
        )
        before_processes = _process_rows()
        result: TurnResult | None = None
        failure: tuple[str, BaseException] | None = None
        preparation_cancelled = False
        skipped: str | None = None
        terminated = False
        restore_ok = True
        self.candidate_untrusted = False
        self.last_failure = None
        self.last_violation = None
        self.processes_terminated = True

        def stop_processes() -> None:
            nonlocal terminated
            if terminated:
                return
            terminated = True
            try:
                remaining = terminate_reviewer_processes(
                    before_processes,
                    self._process_roots(),
                    call_root,
                    roots_shared=bool(getattr(self.native, "reviewer_processes_shared", False)),
                )
                if remaining:
                    self.processes_terminated = False
            except Exception:
                self.processes_terminated = False

        try:
            # Decide before touching the filesystem: when the remaining trial
            # budget cannot also cover isolation overhead and the finish
            # reserve, skip this call and let the controller use its existing
            # soft-deadline/fallback delivery path.
            skipped = self._deadline_shortfall(request.deadline, started)
            if skipped is None:
                try:
                    protected.capture(deadline=self._peripheral_deadline(request.deadline),
                                      cancelled=self._cancel_requested)
                except (FutureCancelledError, ReviewerExecutionError) as exc:
                    if isinstance(exc, FutureCancelledError) or exc.stage == "cancelled":
                        preparation_cancelled = True
                    else:
                        failure = ("capture", exc)
                except Exception as exc:
                    failure = ("capture", exc)
            if failure is None and skipped is None and not preparation_cancelled:
                try:
                    candidate.mkdir(parents=True, exist_ok=True)
                    materialize_checkpoint(
                        request.workdir,
                        candidate,
                        deadline=self._peripheral_deadline(request.deadline),
                        cancelled=self._cancel_requested,
                    )
                    if self.launch_policy is not None:
                        self.launch_policy.grant_traversal(candidate.parent)
                        self.launch_policy.own_tree(candidate)
                except (FutureCancelledError, ReviewerExecutionError) as exc:
                    if isinstance(exc, FutureCancelledError) or exc.stage == "cancelled":
                        preparation_cancelled = True
                    else:
                        failure = ("materialize", exc)
                except Exception as exc:
                    failure = ("materialize", exc)
            if failure is None and skipped is None and not preparation_cancelled:
                skipped = self._deadline_shortfall(request.deadline, started)
            if failure is None and skipped is None and not preparation_cancelled:
                timeout = reviewer_timeout_seconds(
                    requested=request.timeout_seconds,
                    deadline=request.deadline,
                    limits=self.limits,
                )
                if timeout <= 0:
                    skipped = "reviewer deadline reached before the isolated turn"
                else:
                    prompt = request.prompt
                    if REVIEWER_EXECUTION_PROMPT_MARKER not in prompt:
                        prompt += reviewer_execution_guidance(candidate)
                    elif str(candidate) not in prompt:
                        prompt += reviewer_scratch_guidance(candidate)
                    reviewer_request = replace(
                        request, workdir=candidate, timeout_seconds=timeout, prompt=prompt
                    )
                    try:
                        try:
                            (candidate / ".scratch-tmp").mkdir(parents=True, exist_ok=True)
                        except OSError:
                            pass
                        # The authoritative per-turn environment is the
                        # ``turn_environment`` context above. Keep the adapter
                        # hook in sync for adapters that still receive an
                        # explicit environment, without touching os.environ.
                        environment_target = _apply_adapter_environment(
                            self.native, current_environment()
                        )
                        try:
                            result = self.native.run_turn(reviewer_request)
                        finally:
                            _clear_adapter_environment(environment_target)
                    except (FutureCancelledError, ReviewerExecutionError) as exc:
                        if isinstance(exc, FutureCancelledError) or exc.stage == "cancelled":
                            preparation_cancelled = True
                        else:
                            failure = ("reviewer", exc)
                    except Exception as exc:
                        failure = ("reviewer", exc)
            # Stop every Reviewer-owned process before the final comparison.
            stop_processes()
            if result is not None or failure is not None or preparation_cancelled:
                violations: list[dict[str, Any]] = []
                try:
                    if protected.completed_trees:
                        violations = protected.violations()
                except Exception as exc:
                    failure = failure or ("compare", exc)
                if violations:
                    record = self._record_violation(
                        protected,
                        evidence_root,
                        call_id,
                        request,
                        violations=violations,
                        guard_before=guard_before,
                    )
                    self.last_violation = record
                    # A restore is trusted only when the comparison proves the
                    # protected view was actually put back. Missing verification
                    # fails closed, even though the restore call itself reported
                    # success.
                    restore_ok = bool(record.get("restored")) and record.get("verified") is True
                    self.candidate_untrusted = not restore_ok
                    recovery = dict(record.get("recovery") or {})
                    restore_detail = record.get("restore_error")
                    if not restore_ok and not restore_detail:
                        post_restore = record.get("post_restore_violations") or []
                        if post_restore:
                            paths = sorted(
                                {
                                    path
                                    for row in post_restore
                                    for path in (row.get("changed_paths") or [])
                                }
                            )
                            roots = sorted(
                                {str(row.get("root")) for row in post_restore if row.get("root")}
                            )
                            parts = [
                                "post-restore changes: " + ", ".join(paths[:20])
                                if paths
                                else "post-restore verification failed"
                            ]
                            if roots:
                                parts.append("roots: " + ", ".join(roots[:5]))
                            restore_detail = "; ".join(parts)[:1000]
                    if not restore_ok and not restore_detail:
                        if recovery.get("quiet_recheck_changed"):
                            restore_detail = "post-restore quiet recheck changed the protected view"
                        elif recovery.get("quiet_recheck_done"):
                            restore_detail = "post-restore quiet recheck did not verify"
                        else:
                            restore_detail = "reviewer restoration was not verified"
                    integrity = {
                        "type": "ReviewerIntegrityError",
                        "reason": "reviewer_mutation" if restore_ok else "reviewer_restore_failed",
                        "detail": "reviewer mutated a protected view" if restore_ok else
                                  (restore_detail or "reviewer restoration failed"),
                        "evidence": str(evidence_root / f"{call_id}.json"),
                        # #46 fail-closed default: a violation stays a
                        # primary reviewer_mutation/reviewer_restore_failed
                        # failure.  Only the admitted side-call caller may
                        # promote this latch after it has verified all four
                        # recovery conditions and counted the recovery.
                        "recovered": False,
                        "recovery_candidate": bool(
                            restore_ok
                            and record.get("recoverable")
                            and self.processes_terminated
                        ),
                        "recovery_action": recovery.get("action"),
                        "recovery": recovery,
                    }
                    self.integrity_failures.append(integrity)
                    result = self._discarded(
                        request,
                        violations,
                        restore_error=None if restore_ok else restore_detail,
                    )
        except Exception as exc:
            failure = failure or ("isolation", exc)
        finally:
            stop_processes()
            if failure is not None and (
                result is None or "reviewer_mutation" not in result.stderr_tail
            ):
                stage, exc = failure
                if isinstance(exc, ReviewerProtectionError) and self.launch_policy is not None:
                    self.launch_policy.enforcement_error = str(exc)
                self.candidate_untrusted = stage == "restore"
                result = self._failure_result(request, stage, exc)
                record = self._record_failure(request, call_id, stage, exc)
                self.last_failure = record
            elif skipped is not None and result is None:
                result = self._skipped(request, skipped)
                self.last_failure = self._record_skip(request, call_id, skipped)
            elif preparation_cancelled and result is None:
                result = TurnResult(
                    status="cancelled", final_text="", output_source=self.id,
                    exit_code=None, stderr_tail="reviewer preparation cancelled",
                )
            if restore_ok:
                self._delete_copy(call_root, request.deadline, call_id)
            if self.launch_policy is not None:
                self._record_isolation(call_id, request)
        return result if result is not None else self._failure_result(
            request, "isolation", RuntimeError("reviewer produced no result")
        )

    def _record_isolation(self, call_id: str, request: TurnRequest) -> None:
        policy = self.launch_policy
        assert policy is not None
        root = self.root / "reviewer-isolation-evidence"
        root.mkdir(parents=True, exist_ok=True)
        row = {"schema": "long-horizon/reviewer-isolation/v1", "call_id": call_id,
               "review_mode": request.review_mode,
               "review_isolation": "uid_drop" if policy.mode == "uid_drop" and policy.proofs
               and policy.enforcement_error is None else
               "unverified" if policy.mode == "uid_drop" else "degraded",
               "reason": policy.enforcement_error or policy.reason,
               "uid": policy.uid if policy.mode == "uid_drop" and policy.proofs else None,
               "gid": policy.gid if policy.mode == "uid_drop" else None,
               "launch_mechanism": "setpriv" if policy.mode == "uid_drop" else None,
               "child_proofs": policy.proofs}
        (root / f"{call_id}.json").write_text(json.dumps(row, sort_keys=True) + "\n")

    def _record_violation(
        self,
        protected: _ProtectedTreeGroup,
        evidence_root: Path,
        call_id: str,
        request: TurnRequest,
        *,
        violations: list[dict[str, Any]] | None = None,
        guard_before: str | None = None,
    ) -> dict[str, Any]:
        violations = violations if violations is not None else protected.violations()
        restored, restore_errors = protected.restore()
        restore_error = "; ".join(
            f"{row.get('root')}: {row.get('error')}" for row in restore_errors
        )[:1000]
        try:
            post_restore = protected.violations()
        except Exception as exc:
            post_restore = []
            restore_error = (restore_error or f"{type(exc).__name__}: {exc}")[:1000]
        verified = bool(restored) and not post_restore and not restore_error
        recoverable = bool(violations) and all(bool(row.get("recoverable")) for row in violations)
        quiet_seconds = 0.0
        quiet_recheck_changed = False
        quiet_recheck_done = False
        guard_after: str | None = None
        completed_roots = [tree.root for tree in protected.completed_trees]
        if verified and recoverable and guard_before is not None:
            quiet_seconds = recovery_quiet_seconds()
            quiet_recheck_done = True
            unchanged, guard_after = revalidate_recovered_view(
                completed_roots,
                expected=guard_before,
                quiet_seconds=quiet_seconds,
            )
            quiet_recheck_changed = not unchanged
            if unchanged:
                register_recovered_view(completed_roots, guard_after)
            else:
                verified = False
        record = {
            "schema": REVIEWER_MUTATION_SCHEMA,
            "guidance_version": REVIEWER_EXECUTION_GUIDANCE_VERSION,
            "call_id": call_id,
            "at_unix": time.time(),
            "review_mode": request.review_mode,
            "verdict_discarded": True,
            "restored": restored,
            "verified": verified,
            "recoverable": recoverable,
            "restore_error": restore_error or None,
            "restore_errors": restore_errors,
            "violations": violations,
            "post_restore_violations": post_restore,
            "recovery": {
                "action": (
                    "rebuild_from_sealed_candidate"
                    if any(tree.use_checkpoint for tree in protected.trees)
                    else "restore_captured_view"
                ),
                "restored": bool(restored),
                "verified": verified,
                "quiet_period_seconds": quiet_seconds,
                "quiet_recheck_changed": quiet_recheck_changed,
                "quiet_recheck_done": quiet_recheck_done,
                "result": "recovered" if verified and recoverable else (
                    "verified_not_recoverable" if verified else "failed"
                ),
            },
        }
        evidence_root.mkdir(parents=True, exist_ok=True)
        path = evidence_root / f"{call_id}.json"
        path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return record

    def _record_failure(
        self, request: TurnRequest, call_id: str, stage: str, exc: BaseException
    ) -> dict[str, Any]:
        record = {
            "schema": REVIEWER_FAILURE_SCHEMA,
            "call_id": call_id,
            "at_unix": time.time(),
            "review_mode": request.review_mode,
            "stage": stage,
            "error_type": type(exc).__name__,
            "detail": (str(exc) or type(exc).__name__)[:2000],
            "verdict_discarded": True,
        }
        root = self.root / "reviewer-failure-evidence"
        try:
            root.mkdir(parents=True, exist_ok=True)
            (root / f"{call_id}.json").write_text(
                json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
        except Exception:
            pass
        return record

    def _record_skip(self, request: TurnRequest, call_id: str, reason: str) -> dict[str, Any]:
        record = {
            "schema": REVIEWER_SKIP_SCHEMA,
            "call_id": call_id,
            "at_unix": time.time(),
            "review_mode": request.review_mode,
            "reason": reason,
            "verdict_discarded": True,
        }
        root = self.root / "reviewer-skip-evidence"
        try:
            root.mkdir(parents=True, exist_ok=True)
            (root / f"{call_id}.json").write_text(
                json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
        except Exception:
            pass
        return record

    def _delete_copy(self, call_root: Path, deadline: float | None, call_id: str) -> None:
        # A guard follows its view: when the disposable view is destroyed, any
        # guard that still names it must disappear with it.
        discard_recovered_views((call_root,))
        if not call_root.exists() and not call_root.is_symlink():
            return
        try:
            _bounded_timeout(deadline, 300.0, stage="delete")
            shutil.rmtree(call_root)
        except Exception as exc:
            record = {
                "schema": REVIEWER_CLEANUP_SCHEMA,
                "call_id": call_id,
                "at_unix": time.time(),
                "root": str(call_root),
                "error_type": type(exc).__name__,
                "detail": (str(exc) or type(exc).__name__)[:2000],
            }
            try:
                root = self.root / "reviewer-cleanup-evidence"
                root.mkdir(parents=True, exist_ok=True)
                (root / f"{call_id}.json").write_text(
                    json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
                )
            except Exception:
                pass

    def _discarded(
        self,
        request: TurnRequest,
        violations: list[dict[str, Any]],
        *,
        restore_error: str | None = None,
    ) -> TurnResult:
        paths = sorted({path for row in violations for path in row.get("changed_paths", [])})
        detail = "reviewer_mutation: protected tree changed" + (
            ": " + ", ".join(paths[:20]) if paths else ""
        )
        if restore_error:
            detail += f"; restore failed: {restore_error}"
        decision = (
            ReviewDecisionCapture(
                verdict_status="malformed",
                plan_challenge_status="unsupported",
                raw_text="",
                error=detail,
            )
            if request.review_mode == "acceptance"
            else ReviewDecisionCapture(verdict_status="not_applicable", plan_challenge_status="not_applicable")
        )
        return TurnResult(
            status="output_missing",
            final_text="",
            output_source=str(getattr(self.native, "id", "isolated-reviewer")),
            exit_code=None,
            stderr_tail=detail,
            review_decision=decision,
        )

    def _failure_result(self, request: TurnRequest, stage: str, exc: BaseException) -> TurnResult:
        detail = f"reviewer_execution_failed: {stage}: {type(exc).__name__}: {exc}"[:2000]
        decision = (
            ReviewDecisionCapture(
                verdict_status="malformed",
                plan_challenge_status="unsupported",
                raw_text="",
                error=detail,
            )
            if request.review_mode == "acceptance"
            else ReviewDecisionCapture(verdict_status="not_applicable", plan_challenge_status="not_applicable")
        )
        return TurnResult(
            status="output_missing",
            final_text="",
            output_source=str(getattr(self.native, "id", "isolated-reviewer")),
            exit_code=None,
            stderr_tail=detail,
            review_decision=decision,
        )

    def _skipped(self, request: TurnRequest, reason: str) -> TurnResult:
        detail = reason if reason.startswith("reviewer skipped") else "reviewer skipped: " + reason
        decision = (
            ReviewDecisionCapture(
                verdict_status="malformed",
                plan_challenge_status="unsupported",
                raw_text="",
                error=detail,
            )
            if request.review_mode == "acceptance"
            else ReviewDecisionCapture(verdict_status="not_applicable", plan_challenge_status="not_applicable")
        )
        return TurnResult(
            status="timeout",
            final_text="",
            output_source=str(getattr(self.native, "id", "isolated-reviewer")),
            exit_code=None,
            stderr_tail=detail,
            review_decision=decision,
        )


class RecoveredViewHandoffAdapter:
    """End recovered-view guards when a view is handed back to its Worker.

    The final guard check runs immediately before the Worker turn. A delayed
    writer that changed the view between recovery and this handoff therefore
    still causes a hard failure; once the handoff is valid, ordinary Worker
    edits are no longer compared against the pre-recovery fingerprint.
    """

    def __init__(self, native: Any, *, protected_roots: Iterable[Path] = ()) -> None:
        self.native = native
        self.protected_roots = tuple(
            Path(root).expanduser().absolute() for root in protected_roots if root
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self.native, name)

    def run_turn(self, request: TurnRequest) -> TurnResult:
        if request.role == "worker" and request.workdir is not None:
            release_recovered_views((request.workdir, *self.protected_roots))
        return self.native.run_turn(request)


__all__ = [
    "CONTROLLER_METADATA_PREFIXES",
    "DEFAULT_REVIEWER_MEMORY_SHARE",
    "DEFAULT_REVIEWER_TIMEOUT_FRACTION",
    "ENV_REVIEWER_MEMORY_SHARE",
    "ENV_REVIEWER_TIMEOUT_FRACTION",
    "ENV_REVIEWER_TIMEOUT_MAX_SECONDS",
    "ENV_REVIEWER_TIMEOUT_MIN_SECONDS",
    "PROCESS_KILL_GUIDANCE_VERSION",
    "PROTECTED_VIEW_RECOVERY_SCHEMA",
    "PROTECTED_VIEW_SCHEMA",
    "RecoveredViewHandoffAdapter",
    "RecoveredViewIntegrityError",
    "SCRATCH_GUIDANCE_MARKER",
    "IsolatedExecutableReviewer",
    "MAX_REVIEWER_TIMEOUT_SECONDS",
    "MIN_REVIEWER_TIMEOUT_SECONDS",
    "NonDegradableIntegrityError",
    "REVIEWER_EXECUTION_GUIDANCE_VERSION",
    "REVIEWER_EXECUTION_PROMPT_MARKER",
    "REVIEWER_MUTATION_SCHEMA",
    "ReviewerExecutionLimits",
    "SIDE_CALL_INTEGRITY_STATE_SCHEMA",
    "clear_side_call_integrity_state",
    "configure_side_call_integrity_state",
    "discard_recovered_views",
    "cpu_slots",
    "materialize_checkpoint",
    "protected_view_roots",
    "record_side_call_recovery",
    "recovery_quiet_seconds",
    "recovered_view_identity",
    "register_recovered_view",
    "release_recovered_views",
    "revalidate_recovered_view",
    "reviewer_environment",
    "reviewer_execution_guidance",
    "reviewer_execution_limits",
    "reviewer_scratch_guidance",
    "reviewer_memory_share",
    "reviewer_scratch_environment",
    "reviewer_scratch_path",
    "reviewer_scratch_shell_prefix",
    "reviewer_timeout_seconds",
    "side_call_integrity_state_path",
    "terminate_reviewer_descendants",
    "terminate_reviewer_processes",
    "verify_recovered_views",
]
