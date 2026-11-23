# Copyright (c) 2026 Chrys. All rights reserved.

"""Bounded candidate capture for controller-owned Git worktrees.

A Worker checkout is a controller-owned linked worktree.  Build systems create
unignored caches there (Rust's ``target/`` is the common case), and the Core
checkpoint command ``git add -A`` would otherwise turn those files into a
candidate.  This module computes a conservative exclusion list, writes it to
the repository's ``info/exclude`` (never a tracked ``.gitignore``), records the
list as evidence, and refuses a candidate that is still too large before the
Core checkpoint starts.

The source workspace is never excluded: linked worktrees share the repository
``info/exclude`` file, so source snapshots temporarily hide only the managed
block.  Tracked files are never excluded.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import stat
import subprocess
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from pact_core.artifacts import write_json_atomic

EXCLUSIONS_SCHEMA = "long-horizon/checkpoint-exclusions/v1"
CANDIDATE_TOO_LARGE_SCHEMA = "long-horizon/candidate-too-large/v1"
MEMORY_PRESSURE_SCHEMA = "long-horizon/controller-memory-pressure/v1"
MEMORY_DEGRADED_SCHEMA = "long-horizon/controller-memory-degraded/v1"
ENV_MAX_UNTRACKED_FILE_BYTES = "LH_MAX_UNTRACKED_FILE_BYTES"
ENV_MAX_CANDIDATE_BYTES = "LH_MAX_CANDIDATE_BYTES"
DEFAULT_MAX_UNTRACKED_FILE_BYTES = 50 * 1024 * 1024
DEFAULT_MAX_CANDIDATE_BYTES = 1024 * 1024 * 1024
_BUILD_DIRECTORY_NAMES = frozenset(
    {
        "target",
        "node_modules",
        ".gradle",
        ".mvn/repository",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".venv",
        ".tox",
        ".cargo/registry",
        ".m2",
    }
)
_EXCLUDE_START = "# >>> long-horizon managed build/cache exclusions >>>"
_EXCLUDE_END = "# <<< long-horizon managed build/cache exclusions <<<"
_CHUNK = 1024 * 1024
_PATCHED = False


class CandidateTooLarge(RuntimeError):
    """The candidate exceeds ``LH_MAX_CANDIDATE_BYTES`` before a checkpoint."""

    def __init__(self, estimate: dict[str, Any], evidence: Path | None) -> None:
        self.estimate = estimate
        self.evidence = evidence
        super().__init__(
            "candidate_too_large: "
            f"{estimate['total_bytes']} bytes exceeds limit {estimate['limit_bytes']} bytes"
        )


class ControllerMemoryPressure(RuntimeError):
    """The controller parent reached the effective memory threshold."""

    def __init__(self, evidence: Path, snapshot: dict[str, Any]) -> None:
        self.evidence = evidence
        self.snapshot = snapshot
        memory = snapshot.get("memory") if isinstance(snapshot.get("memory"), dict) else snapshot
        self.memory_snapshot = memory
        super().__init__(
            "memory_pressure: controller effective usage "
            f"{memory.get('effective_bytes')} reached threshold {memory.get('threshold_bytes')}"
        )


@dataclass(frozen=True)
class _Entry:
    kind: str
    path: str
    size: int | None = None
    reason: str | None = None


def _positive_env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def max_untracked_file_bytes() -> int:
    return _positive_env_int(ENV_MAX_UNTRACKED_FILE_BYTES, DEFAULT_MAX_UNTRACKED_FILE_BYTES)


def max_candidate_bytes() -> int:
    return _positive_env_int(ENV_MAX_CANDIDATE_BYTES, DEFAULT_MAX_CANDIDATE_BYTES)


def sha256_file(path: Path, *, chunk_size: int = _CHUNK) -> str:
    """Hash a file without loading it into the controller heap."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _git_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    return environment


def _run_git(workspace: Path, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=workspace,
        env=_git_environment(),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=False,
        check=False,
    )
    decoded = subprocess.CompletedProcess(
        result.args,
        result.returncode,
        result.stdout.decode("utf-8", errors="surrogateescape"),
        result.stderr.decode("utf-8", errors="surrogateescape"),
    )
    if check and result.returncode != 0:
        detail = decoded.stderr.strip() or decoded.stdout.strip() or f"exit code {result.returncode}"
        raise RuntimeError(f"git {' '.join(args)} failed: {detail}")
    return decoded


def _git_paths(workspace: Path, args: list[str], *, check: bool = True) -> Iterator[str]:
    """Stream NUL-separated Git path output without holding it all in memory."""
    process = subprocess.Popen(
        ["git", *args],
        cwd=workspace,
        env=_git_environment(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None and process.stderr is not None
    buffer = b""
    while True:
        chunk = process.stdout.read(64 * 1024)
        if not chunk:
            break
        buffer += chunk
        parts = buffer.split(b"\0")
        buffer = parts.pop()
        for part in parts:
            if part:
                yield part.decode("utf-8", errors="surrogateescape")
    if buffer:
        yield buffer.decode("utf-8", errors="surrogateescape")
    stderr = process.stderr.read().decode("utf-8", errors="surrogateescape")
    code = process.wait()
    if check and code != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {stderr.strip() or f'exit code {code}'}")


def _workspace_root(workspace: Path) -> Path:
    return Path(_run_git(workspace, ["rev-parse", "--show-toplevel"]).stdout.strip()).resolve()


def _exclude_path(workspace: Path) -> Path:
    raw = _run_git(workspace, ["rev-parse", "--git-path", "info/exclude"]).stdout.strip()
    path = Path(raw)
    if not path.is_absolute():
        path = workspace / path
    return path


def _strip_managed_block(text: str) -> str:
    lines = text.splitlines(keepends=True)
    kept: list[str] = []
    inside = False
    for line in lines:
        if line.rstrip("\r\n") == _EXCLUDE_START:
            inside = True
            continue
        if line.rstrip("\r\n") == _EXCLUDE_END:
            inside = False
            continue
        if not inside:
            kept.append(line)
    return "".join(kept)


def _managed_patterns(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="surrogateescape")
    except OSError:
        return []
    patterns: list[str] = []
    inside = False
    for line in text.splitlines():
        if line == _EXCLUDE_START:
            inside = True
            continue
        if line == _EXCLUDE_END:
            inside = False
            continue
        if inside and line and not line.startswith("#"):
            patterns.append(line)
    return patterns


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        mode = 0o644
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", errors="surrogateescape", newline="") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


@contextlib.contextmanager
def suspend_managed_exclusions(workspace: Path) -> Iterator[None]:
    """Hide the managed block while a source workspace is being snapshotted."""
    path = _exclude_path(workspace)
    try:
        original = path.read_text(encoding="utf-8", errors="surrogateescape")
    except OSError:
        yield
        return
    stripped = _strip_managed_block(original)
    if stripped == original:
        yield
        return
    _atomic_write_text(path, stripped)
    try:
        yield
    finally:
        _atomic_write_text(path, original)


def _is_build_directory(relative: str) -> bool:
    return any(relative == name or relative.endswith("/" + name) for name in _BUILD_DIRECTORY_NAMES)


def _is_directory(workspace: Path, relative: str) -> bool:
    path = workspace / relative
    try:
        return path.is_dir() and not path.is_symlink()
    except OSError:
        return False


def _path_is_tracked(workspace: Path, relative: str) -> bool:
    process = subprocess.Popen(
        ["git", "--literal-pathspecs", "ls-files", "-z", "--", relative],
        cwd=workspace,
        env=_git_environment(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    assert process.stdout is not None
    try:
        found = bool(process.stdout.read(1))
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
    return found


def _directory_has_tracked_files(workspace: Path, relative: str) -> bool:
    return _path_is_tracked(workspace, relative)


def _pattern_for(relative: str, *, directory: bool) -> str:
    value = "/" + relative.replace("\\", "\\\\")
    for character in ("*", "?", "["):
        value = value.replace(character, "\\" + character)
    if directory:
        value += "/"
    else:
        if value.endswith(" "):
            value = value[:-1] + "\\ "
    return value


def _entry_from_pattern(pattern: str) -> _Entry | None:
    value = pattern[1:] if pattern.startswith("/") else pattern
    directory = value.endswith("/")
    if directory:
        value = value[:-1]
    value = value.replace("\\ ", " ").replace("\\\\", "\\")
    for character in ("*", "?", "["):
        value = value.replace("\\" + character, character)
    if not value or value.startswith("/") or ".." in PurePosixPath(value).parts:
        return None
    return _Entry("directory" if directory else "file", value)


def _untracked_paths(workspace: Path) -> Iterator[str]:
    yield from _git_paths(workspace, ["ls-files", "--others", "--exclude-standard", "-z"])


def _validate_existing_entries(workspace: Path, entries: list[_Entry]) -> list[_Entry]:
    kept: list[_Entry] = []
    for entry in entries:
        if entry.kind == "directory":
            if not _is_directory(workspace, entry.path):
                continue
            if _directory_has_tracked_files(workspace, entry.path):
                continue
            if not _is_build_directory(entry.path) and not (workspace / entry.path / "CACHEDIR.TAG").is_file():
                # A formerly large file promoted to a directory is handled by
                # the fresh scan; do not retain an unexplained directory.
                continue
            kept.append(entry)
        else:
            path = workspace / entry.path
            try:
                size = path.lstat().st_size
            except OSError:
                continue
            if _path_is_tracked(workspace, entry.path) or size <= max_untracked_file_bytes():
                continue
            kept.append(_Entry("file", entry.path, size=size, reason="large_untracked_file"))
    return kept


def _prune_directories(entries: list[_Entry]) -> list[_Entry]:
    directories = sorted(
        (entry for entry in entries if entry.kind == "directory"),
        key=lambda item: (len(PurePosixPath(item.path).parts), item.path),
    )
    kept: list[_Entry] = []
    for entry in directories:
        if any(entry.path == parent.path or entry.path.startswith(parent.path + "/") for parent in kept):
            continue
        kept.append(entry)
    files: list[_Entry] = []
    for entry in entries:
        if entry.kind != "file":
            continue
        if any(entry.path == parent.path or entry.path.startswith(parent.path + "/") for parent in kept):
            continue
        files.append(entry)
    return sorted([*kept, *files], key=lambda item: (item.kind, item.path))


def apply_workspace_exclusions(workspace: Path) -> dict[str, Any]:
    """Compute and persist controller-owned build/cache exclusions.

    Returns the manifest that callers persist as ``checkpoint-exclusions.json``.
    Only untracked content can be excluded; a directory containing any tracked
    file is retained in full.
    """
    workspace = workspace.expanduser().resolve()
    root = _workspace_root(workspace)
    if root != workspace:
        raise RuntimeError("checkpoint exclusions require the worktree root")
    exclude_path = _exclude_path(workspace)
    existing = _validate_existing_entries(
        workspace,
        [entry for pattern in _managed_patterns(exclude_path) if (entry := _entry_from_pattern(pattern)) is not None],
    )
    directories: dict[str, _Entry] = {
        entry.path: entry for entry in existing if entry.kind == "directory"
    }
    files: dict[str, _Entry] = {entry.path: entry for entry in existing if entry.kind == "file"}
    limit = max_untracked_file_bytes()
    for relative in _untracked_paths(workspace):
        if any(relative == parent or relative.startswith(parent + "/") for parent in directories):
            continue
        parts = PurePosixPath(relative).parts
        if not parts:
            continue
        if parts[-1] == "CACHEDIR.TAG" and len(parts) > 1:
            parent = "/".join(parts[:-1])
            if (
                parent not in directories
                and _is_directory(workspace, parent)
                and not _directory_has_tracked_files(workspace, parent)
            ):
                directories[parent] = _Entry("directory", parent, reason="cachedir_tag")
        for index in range(1, len(parts)):
            parent = "/".join(parts[:index])
            if not _is_build_directory(parent):
                continue
            if (
                parent not in directories
                and _is_directory(workspace, parent)
                and not _directory_has_tracked_files(workspace, parent)
            ):
                directories[parent] = _Entry("directory", parent, reason="build_directory")
        path = workspace / relative
        try:
            info = path.lstat()
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode) and info.st_size > limit:
            files.setdefault(relative, _Entry("file", relative, size=info.st_size, reason="large_untracked_file"))
    entries = _prune_directories([*directories.values(), *files.values()])
    patterns = [_pattern_for(entry.path, directory=entry.kind == "directory") for entry in entries]
    current = ""
    try:
        current = exclude_path.read_text(encoding="utf-8", errors="surrogateescape")
    except OSError:
        pass
    base = _strip_managed_block(current).rstrip("\r\n")
    if patterns:
        block = "\n".join([_EXCLUDE_START, *patterns, _EXCLUDE_END])
        content = (base + "\n" if base else "") + block + "\n"
    else:
        content = base + "\n" if base else ""
    if content != current:
        _atomic_write_text(exclude_path, content)
    return {
        "schema": EXCLUSIONS_SCHEMA,
        "created_at_unix": time.time(),
        "workspace": str(workspace),
        "exclude_path": str(exclude_path),
        "max_untracked_file_bytes": limit,
        "entries": [
            {
                "kind": entry.kind,
                "path": entry.path,
                "reason": entry.reason,
                **({"bytes": entry.size} if entry.size is not None else {}),
            }
            for entry in entries
        ],
        "patterns": patterns,
        "tracked_files_excluded": False,
    }


def estimate_candidate(workspace: Path, *, base_commit: str | None = None) -> dict[str, Any]:
    """Estimate bytes a checkpoint would include without reading file content."""
    workspace = workspace.expanduser().resolve()
    if base_commit is None:
        base_commit = _run_git(workspace, ["rev-parse", "HEAD"]).stdout.strip()
    limit = max_candidate_bytes()
    seen: set[str] = set()
    tracked_bytes = 0
    untracked_bytes = 0
    file_count = 0
    sample: list[dict[str, Any]] = []

    def account(relative: str, *, untracked: bool) -> None:
        nonlocal tracked_bytes, untracked_bytes, file_count
        if not relative or relative in seen:
            return
        seen.add(relative)
        first = PurePosixPath(relative).parts[0].casefold() if PurePosixPath(relative).parts else ""
        if first in {".pact", ".pact-io"}:
            return
        path = workspace / relative
        try:
            info = path.lstat()
        except OSError:
            size = 0
        else:
            size = info.st_size if (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)) else 0
        file_count += 1
        if untracked:
            untracked_bytes += size
        else:
            tracked_bytes += size
        if len(sample) < 200:
            sample.append({"path": relative, "bytes": size, "untracked": untracked})

    for relative in _git_paths(
        workspace, ["diff", "--name-only", "-z", "--no-renames", base_commit, "--", "."]
    ):
        account(relative, untracked=False)
    for relative in _untracked_paths(workspace):
        account(relative, untracked=True)
    total = tracked_bytes + untracked_bytes
    return {
        "schema": CANDIDATE_TOO_LARGE_SCHEMA + "#estimate",
        "base_commit": base_commit,
        "limit_bytes": limit,
        "total_bytes": total,
        "tracked_bytes": tracked_bytes,
        "untracked_bytes": untracked_bytes,
        "file_count": file_count,
        "over_limit": total > limit,
        "sample": sample,
    }


def _pressure_state(observed: Any, snapshot: dict[str, Any]) -> dict[str, Any]:
    """Read the unified MemoryBudget decision, with a conservative duck-typed fallback."""
    state = getattr(observed, "pressure_state", None)
    if callable(state):
        return state()
    memory = snapshot.get("memory") if isinstance(snapshot.get("memory"), dict) else snapshot
    limit = memory.get("limit_bytes")
    effective = memory.get("effective_bytes")
    threshold = memory.get("threshold_bytes")
    degraded = bool(memory.get("degraded"))
    numbers = all(isinstance(value, int) and not isinstance(value, bool) for value in (limit, effective, threshold))
    if not numbers or limit <= 0 or degraded:
        return {
            "assessable": False,
            "at_pressure": False,
            "reason": memory.get("degraded_reason") or "memory_pressure_unassessable",
        }
    return {"assessable": True, "at_pressure": effective >= threshold, "reason": None}


def check_controller_memory(
    evidence_root: Path,
    *,
    stage: str,
    budget: Any | None = None,
) -> dict[str, Any] | None:
    """Refuse a checkpoint only when pressure is observable and actually reached."""
    from .memory_guard import memory_budget, memory_budget_from_environment, write_memory_checkpoint

    if budget is None:
        budget = memory_budget_from_environment()
        if budget.limit_bytes is None:
            budget = memory_budget()
    observed = budget.observe()
    snapshot = observed.snapshot()
    state = _pressure_state(observed, snapshot)
    if not state["assessable"]:
        # An unavailable limit or usage is not pressure. Keep the run alive,
        # but persist why this checkpoint could not make a pressure decision.
        # Evidence is best effort: an unwritable path must not abort the run.
        try:
            evidence_root.mkdir(parents=True, exist_ok=True)
            write_json_atomic(
                evidence_root / "controller-memory-degraded.json",
                {
                    "schema": MEMORY_DEGRADED_SCHEMA,
                    "kind": MEMORY_DEGRADED_SCHEMA,
                    "stage": stage,
                    "pid": os.getpid(),
                    "memory": snapshot,
                    "reason": state["reason"],
                    "action": "continue_without_pressure_refusal",
                    "created_at_unix": time.time(),
                },
            )
        except OSError:
            pass
        return None
    if not state["at_pressure"]:
        return None
    payload = {
        "kind": MEMORY_PRESSURE_SCHEMA,
        "stage": stage,
        "pid": os.getpid(),
        "memory": snapshot,
        "reason": "controller_effective_memory_pressure",
        "action": "refuse_candidate_and_use_existing_fallback_delivery",
    }
    evidence_root.mkdir(parents=True, exist_ok=True)
    path = evidence_root / "controller-memory-checkpoint.json"
    record = write_memory_checkpoint(path, payload)
    raise ControllerMemoryPressure(path, record)


def prepare_checkpoint(
    workspace: Path,
    evidence_root: Path,
    *,
    base_commit: str | None = None,
    stage: str = "checkpoint",
    budget: Any | None = None,
) -> dict[str, Any]:
    """Apply exclusions, persist evidence, and enforce the candidate byte cap."""
    evidence_root = evidence_root.expanduser().resolve()
    evidence_root.mkdir(parents=True, exist_ok=True)
    exclusions = apply_workspace_exclusions(workspace)
    write_json_atomic(evidence_root / "checkpoint-exclusions.json", exclusions)
    check_controller_memory(evidence_root, stage=stage + "_before", budget=budget)
    estimate = estimate_candidate(workspace, base_commit=base_commit)
    if estimate["over_limit"]:
        payload = {
            "schema": CANDIDATE_TOO_LARGE_SCHEMA,
            "created_at_unix": time.time(),
            "stage": stage,
            "workspace": str(workspace.expanduser().resolve()),
            "reason": "candidate_too_large",
            "action": "candidate_not_captured; controller retains prior checkpoint",
            "estimate": estimate,
            "exclusions": {
                "schema": exclusions["schema"],
                "entries": exclusions["entries"],
                "patterns": exclusions["patterns"],
            },
        }
        path = evidence_root / "candidate-too-large.json"
        write_json_atomic(path, payload)
        raise CandidateTooLarge(estimate, path)
    return {"exclusions": exclusions, "estimate": estimate}


def _guard_checkpoint_worker(store: Any, *, round_number: int) -> Any:
    workspace = store.worker_worktree
    check_controller_memory(store.loop_root, stage="core_checkpoint_before")
    exclusions = apply_workspace_exclusions(workspace)
    write_json_atomic(store.loop_root / "checkpoint-exclusions.json", exclusions)
    estimate = estimate_candidate(workspace, base_commit=store.current_commit)
    if estimate["over_limit"]:
        payload = {
            "schema": CANDIDATE_TOO_LARGE_SCHEMA,
            "created_at_unix": time.time(),
            "stage": "core_checkpoint",
            "workspace": str(workspace),
            "reason": "candidate_too_large",
            "action": "checkpoint_refused_before_git_add",
            "estimate": estimate,
            "exclusions": {
                "schema": exclusions["schema"],
                "entries": exclusions["entries"],
                "patterns": exclusions["patterns"],
            },
        }
        path = store.loop_root / "candidate-too-large.json"
        write_json_atomic(path, payload)
        raise CandidateTooLarge(estimate, path)
    return _ORIGINAL_CHECKPOINT_WORKER(store, round_number=round_number)


def install_bounded_digest() -> None:
    """Make Core's evidence digest paths chunked without changing their values."""
    modules = (
        "pact_core.runtime.store",
        "pact_core.runtime.export",
        "pact_core.runtime.control_plane",
        "pact_core.runtime.recovery",
        "pact_core.runtime.bridge",
        "pact_core._work_execution_support",
        "pact_core._work_execution",
    )
    for name in modules:
        try:
            module = __import__(name, fromlist=["*"])
        except ImportError:
            continue
        for attribute in ("digest", "_digest", "artifact_digest"):
            if hasattr(module, attribute):
                setattr(module, attribute, sha256_file)


_ORIGINAL_CHECKPOINT_WORKER: Any = None
_ORIGINAL_WORKSPACE_TREE: Any = None
_ORIGINAL_STATUS: Any = None
_ORIGINAL_VOLATILE: Any = None


def install_checkpoint_guard() -> None:
    """Install process-wide controller guards before any Core checkpoint."""
    global _PATCHED, _ORIGINAL_CHECKPOINT_WORKER, _ORIGINAL_WORKSPACE_TREE, _ORIGINAL_STATUS, _ORIGINAL_VOLATILE
    install_bounded_digest()
    if _PATCHED:
        return
    from pact_core.checkpoint import CheckpointStore

    _ORIGINAL_CHECKPOINT_WORKER = CheckpointStore.checkpoint_worker
    _ORIGINAL_WORKSPACE_TREE = CheckpointStore._workspace_tree
    _ORIGINAL_STATUS = CheckpointStore._status
    _ORIGINAL_VOLATILE = CheckpointStore._untracked_volatile_directories

    def checkpoint_worker(self: Any, *, round_number: int) -> Any:
        return _guard_checkpoint_worker(self, round_number=round_number)

    def workspace_tree(self: Any, workspace: Path, base_commit: str) -> str:
        if Path(workspace).expanduser().resolve() == self.workspace.expanduser().resolve():
            with suspend_managed_exclusions(workspace):
                return _ORIGINAL_WORKSPACE_TREE(self, workspace, base_commit)
        return _ORIGINAL_WORKSPACE_TREE(self, workspace, base_commit)

    def status(self: Any, workspace: Path, *, include_reserved: bool = False) -> str:
        if Path(workspace).expanduser().resolve() == self.workspace.expanduser().resolve():
            with suspend_managed_exclusions(workspace):
                return _ORIGINAL_STATUS(self, workspace, include_reserved=include_reserved)
        return _ORIGINAL_STATUS(self, workspace, include_reserved=include_reserved)

    def untracked_volatile(self: Any) -> list[str]:
        with suspend_managed_exclusions(self.workspace):
            return _ORIGINAL_VOLATILE(self)

    CheckpointStore.checkpoint_worker = checkpoint_worker
    CheckpointStore._workspace_tree = workspace_tree
    CheckpointStore._status = status
    CheckpointStore._untracked_volatile_directories = untracked_volatile
    _PATCHED = True


__all__ = [
    "CANDIDATE_TOO_LARGE_SCHEMA",
    "MEMORY_DEGRADED_SCHEMA",
    "CandidateTooLarge",
    "ControllerMemoryPressure",
    "DEFAULT_MAX_CANDIDATE_BYTES",
    "DEFAULT_MAX_UNTRACKED_FILE_BYTES",
    "ENV_MAX_CANDIDATE_BYTES",
    "ENV_MAX_UNTRACKED_FILE_BYTES",
    "EXCLUSIONS_SCHEMA",
    "apply_workspace_exclusions",
    "check_controller_memory",
    "estimate_candidate",
    "install_bounded_digest",
    "install_checkpoint_guard",
    "max_candidate_bytes",
    "max_untracked_file_bytes",
    "prepare_checkpoint",
    "sha256_file",
    "suspend_managed_exclusions",
]
