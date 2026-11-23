# Copyright (c) 2026 Chrys. All rights reserved.

"""Bounded candidate file publication helpers."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

EXCLUDED = frozenset({".git", ".pact", ".pact-io", "node_modules", ".venv", ".lake", "__pycache__", ".semantic-search"})
MAX_FILE = 8 * 1024 * 1024
MAX_TOTAL = 512 * 1024 * 1024


def source_files(root: Path) -> tuple[set[str], list[str]]:
    included, skipped, total = set(), [], 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDED and not (Path(directory) / d).is_symlink())
        for name in sorted(files):
            if name in EXCLUDED:
                continue
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            if (
                path.is_symlink()
                or not path.is_file()
                or path.stat().st_size > MAX_FILE
                or not os.access(path, os.R_OK)
            ):
                skipped.append(relative)
                continue
            total += path.stat().st_size
            if total > MAX_TOTAL or len(included) >= 50_000:
                raise ValueError("terminal source view exceeds its frozen size limit")
            included.add(relative)
    return included, skipped


def _safe_path(root: Path, relative: str) -> Path:
    path = root / relative
    if Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise ValueError("invalid managed source path")
    # Do not follow a task-created directory link into unrelated terminal state.
    if any(parent.is_symlink() for parent in path.parents if parent != root and parent.is_relative_to(root)):
        raise ValueError("managed source parent became a symbolic link")
    return path


def copy_files(source: Path, target: Path, names: set[str], previous: set[str]) -> None:
    for relative in sorted(previous - names):
        destination = _safe_path(target, relative)
        if destination.is_file() or destination.is_symlink():
            destination.unlink()
    for relative in sorted(names):
        origin, destination = _safe_path(source, relative), _safe_path(target, relative)
        if origin.is_symlink() or not origin.is_file():
            raise ValueError("managed source ceased to be a regular file")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.is_symlink():
            raise ValueError("managed destination became a symbolic link")
        # Linux refuses to overwrite an inode currently used by an executable.
        # Atomic replacement also keeps readers from observing a partial copy.
        descriptor, temporary_name = tempfile.mkstemp(prefix=".long-horizon-", dir=destination.parent)
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            shutil.copy2(origin, temporary)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)

