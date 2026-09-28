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
from pathlib import Path

from pact_core.runtime.schemas import RuntimeStateError

from jiuwenswarm.long_horizon.pact.public_verification import isolated_verify_command, pinned_commands

_SKIP_PREFIXES = (".pact", ".pact-io", ".git")


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
        completed = subprocess.run(  # noqa: S602 - public verify commands are shell strings by contract
            command, shell=True, cwd=cwd, env=command_env, stdin=subprocess.DEVNULL, check=False
        )
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
