# Copyright (c) 2026 Chrys. All rights reserved.

"""Bounded error metadata; never inspect exception messages, arguments or locals."""

from __future__ import annotations

import errno
import json
import os
import re
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Iterable


def safe_failure(error: BaseException, *, trusted_files: Iterable[str], stage: str) -> dict:
    trusted = frozenset(trusted_files)
    frames = []
    trace = error.__traceback__
    while trace is not None:
        code = trace.tb_frame.f_code
        if code.co_filename in trusted:
            frames.append({"file": code.co_filename, "line": trace.tb_lineno, "function": code.co_name})
        trace = trace.tb_next
    number = error.errno if isinstance(error, OSError) and type(error.errno) is int else None
    return {
        "type": type(error).__name__[:128],
        "stage": stage,
        "frames": frames[-32:],
        "errno": number,
        "errno_name": errno.errorcode.get(number) if number is not None else None,
        "subprocess_return_code": error.returncode
        if isinstance(error, subprocess.CalledProcessError) and type(error.returncode) is int
        else None,
    }


@lru_cache(maxsize=1)
def package_source_files() -> tuple[str, ...]:
    """The caller's frozen package files, not filenames suggested by an error."""
    return tuple(str(p) for p in Path(__file__).parent.glob("*.py") if p.is_file() and not p.is_symlink())


def public_gap(code: str) -> str:
    return code if re.fullmatch(r"[a-z][a-z0-9_]{0,95}", code) else "evidence_failure"


def write_metadata(root: Path, name: str, value: dict) -> bool:
    """Best-effort own metadata, immutable filename and no permission recovery."""
    try:
        if root.is_symlink() or root.resolve(strict=True) != root or Path(name).name != name:
            return False
        fd = os.open(root / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, sort_keys=True, allow_nan=False)
            stream.write("\n")
        return True
    except Exception:
        return False
