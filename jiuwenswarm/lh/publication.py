"""Publish a sealed Core export into the caller's workspace without touching its Git index."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from threading import Event

from jiuwenswarm.pact_core._runtime_serialization import decode
from jiuwenswarm.pact_core.runtime.export import ExportReceipt, verify_export
from jiuwenswarm.pact_core.runtime.types import AcceptanceReceipt, CandidateRef


def git_env() -> dict[str, str]:
    return {
        **{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
    }


def _git(
    core: Path, live: Path, *args: str, data: bytes | None = None, remaining: Callable[[], float | None]
) -> bytes:
    # The caller can be a subdirectory, an ordinary directory, or a dirty checkout.
    return subprocess.run(
        ["git", f"--git-dir={core / '.git'}", f"--work-tree={live}", *args],
        cwd=live,
        env=git_env(),
        input=data,
        stdin=subprocess.DEVNULL if data is None else None,
        capture_output=True,
        check=True,
        timeout=remaining(),
    ).stdout


def caller_tracked(
    live: Path,
    *,
    ignored_only: bool = False,
    remaining: Callable[[], float | None] = lambda: None,
) -> list[bytes]:
    """List the caller index for discovery, including force-added ignored files."""
    options = dict(cwd=live, env={**git_env(), "LC_ALL": "C"}, stdin=subprocess.DEVNULL, capture_output=True)
    probe = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"], **options, check=False, timeout=remaining()
    )
    if probe.stdout.strip() != b"true":
        if probe.returncode and b"not a git repository" not in probe.stderr.lower():
            raise ValueError("cannot inspect caller repository")
        return []
    flags = ["--ignored", "--exclude-standard"] if ignored_only else []
    result = subprocess.run(
        ["git", "ls-files", "--cached", *flags, "-z", "--", "."], **options, check=True, timeout=remaining()
    )
    return result.stdout.split(b"\0")[:-1]


def _open_blob(path: str, flags: int) -> int:
    return os.open(path, flags | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))


def _matches(live: Path, core: Path, tree: str, remaining: Callable[[], float | None]) -> bool:
    expected = {}
    for entry in _git(core, core, "ls-tree", "-rz", tree, remaining=remaining).split(b"\0")[:-1]:
        metadata, name = entry.split(b"\t", 1)
        mode, kind, oid = metadata.split()
        if kind != b"blob":
            return False
        expected[name] = mode, oid

    # Use indexes only for discovery. Read all expected files even if
    # newly ignored. The S0 copy explicitly excludes .pact control directories.
    visible = _git(core, live, "ls-files", "--cached", "--others", "--exclude-standard", "-z", remaining=remaining)
    names = set(visible.split(b"\0")[:-1]) | expected.keys() | set(caller_tracked(live, remaining=remaining))
    for name in names:
        remaining()
        relative = Path(os.fsdecode(name))
        if ".pact" in relative.parts:
            continue
        path = live / relative
        if path.parent.resolve() != path.parent:
            # The baseline index can still list descendants of a directory that
            # the accepted tree replaced with a symlink. Never follow that link.
            if name not in expected and any(
                expected.get(os.fsencode(parent), (None,))[0] == b"120000" for parent in relative.parents
            ):
                continue
            return False
        try:
            info = path.lstat()
        except (FileNotFoundError, NotADirectoryError):
            if name in expected:
                return False
            continue
        if name not in expected:
            if stat.S_ISDIR(info.st_mode) and not os.path.lexists(path / ".git"):
                continue
            return False
        mode, oid = expected[name]
        if stat.S_ISLNK(info.st_mode) and mode == b"120000":
            data = os.fsencode(os.readlink(path))
            digest = hashlib.new("sha1" if len(oid) == 40 else "sha256", f"blob {len(data)}\0".encode() + data)
        elif stat.S_ISREG(info.st_mode) and mode == (b"100755" if info.st_mode & stat.S_IXUSR else b"100644"):
            digest = hashlib.new("sha1" if len(oid) == 40 else "sha256", f"blob {info.st_size}\0".encode())
            with open(path, "rb", opener=_open_blob) as stream:
                if not os.path.samestat(info, os.fstat(stream.fileno())):
                    return False
                while chunk := stream.read(1024 * 1024):
                    remaining()
                    digest.update(chunk)
        else:
            return False
        current = path.lstat()
        if (
            not os.path.samestat(info, current)
            or info.st_ctime_ns != current.st_ctime_ns
            or info.st_mode != current.st_mode
            or digest.hexdigest().encode() != oid
        ):
            return False
    remaining()
    return True


def apply_candidate(
    selection: Path,
    live: Path,
    core: Path,
    baseline: str,
    *,
    work_id: str,
    deadline: float | None,
    cancelled: Event | None = None,
) -> tuple[list[str], str | None]:
    """Return changed paths and, for accepted work, the verified caller tree."""

    def remaining() -> float | None:
        if cancelled is not None and cancelled.is_set():
            raise asyncio.CancelledError()
        if deadline is None:
            return None
        left = deadline - time.time()
        if left <= 0:
            raise TimeoutError("accepted candidate publication exceeded its deadline")
        return min(left, 30.0)

    core = core.resolve(strict=True)
    report = json.loads(selection.read_bytes())
    if not isinstance(report, dict):
        raise ValueError("selection must be a Core report")
    receipt = decode(ExportReceipt, report["export"])
    candidate = receipt.candidate
    if (
        receipt.source_workspace != core
        or receipt.source_basis.get("workspace_tree") != baseline
        or receipt.work_id != work_id
        or report.get("work_id") != work_id
        or candidate.work_id != work_id
        or decode(CandidateRef, report["candidate"]) != candidate
        or report.get("internal_accepted") is not receipt.accepted
    ):
        raise ValueError("selection does not bind this Work, baseline and exported candidate")
    if not receipt.accepted:
        # Partial candidates can still be published after the model budget expires.
        deadline = None
    remaining()
    payload = verify_export(receipt)
    if payload.get("candidate_artifact") != str(candidate.artifact):
        raise ValueError("export refers to a different candidate artifact")
    if receipt.accepted:
        acceptance = decode(AcceptanceReceipt, report.get("acceptance"))
        if acceptance.work_id != work_id or acceptance.candidate != candidate:
            raise ValueError("acceptance refers to a different candidate")
    if report.get("delivered") or report.get("delivery") is not None:
        delivery = report.get("delivery") or {}
        if (
            not isinstance(delivery, dict)
            or not receipt.accepted
            or report.get("delivered") is not True
            or delivery.get("checkpoint") != candidate.checkpoint
            or delivery.get("status") not in {"applied", "no_changes"}
        ):
            raise ValueError("delivery does not bind the accepted checkpoint")
    tree = (
        _git(
            core,
            core,
            "rev-parse",
            "--verify",
            "--end-of-options",
            f"{candidate.checkpoint}^{{tree}}",
            remaining=remaining,
        )
        .decode("ascii")
        .strip()
    )
    if tree != candidate.tree:
        raise ValueError("exported checkpoint tree differs from the selected tree")
    if receipt.accepted:
        if _matches(live, core, tree, remaining):
            return [], tree
        if not _matches(live, core, baseline, remaining):
            raise ValueError("caller workspace differs from both S0 and the accepted candidate")

    patch = receipt.patch.read_bytes()
    if hashlib.sha256(patch).hexdigest() != receipt.artifact_hashes["candidate.patch"]:
        raise ValueError("candidate patch changed after export verification")
    names = _git(core, core, "diff", "--name-only", "--no-renames", "-z", baseline, tree, remaining=remaining)
    if patch:
        _git(core, live, "apply", "--binary", "--whitespace=nowarn", "-", data=patch, remaining=remaining)
    if receipt.accepted and not _matches(live, core, tree, remaining):
        raise ValueError("published workspace differs from the accepted candidate")
    return [os.fsdecode(name) for name in names.split(b"\0")[:-1]], tree if receipt.accepted else None
