# Copyright (c) 2026 Chrys. All rights reserved.

"""One durable order for every candidate gate attempt and archive entry.

The controller owns an output root across invocations. Gate files are written
per invocation while archive bundles live under a possibly different directory,
so neither filenames nor independent per-store counters can order candidates
across both stores. This module keeps one monotonically increasing sequence in
that controller output root and increments it under a POSIX file lock.
"""

from __future__ import annotations

import errno
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from pact_core.artifacts import write_json_atomic
from pact_core.runtime import RuntimeStateError

SCHEMA = "harbor-runtime/candidate-global-sequence/v1"
SEQUENCE_FILENAME = "candidate-global-sequence.json"
_LOCK_POLL_SECONDS = 0.01


class GlobalSequence:
    """A persistent, locked counter rooted in the controller output directory."""

    def __init__(self, root: Path, *, lock_timeout_seconds: float = 10.0) -> None:
        self.root = root
        self.path = root / SEQUENCE_FILENAME
        self.lock_timeout_seconds = float(lock_timeout_seconds)

    @contextmanager
    def _file_lock(self) -> Iterator[None]:
        try:
            import fcntl
        except ImportError as error:
            raise RuntimeStateError("candidate sequence locking requires POSIX") from error
        self.root.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_name(self.path.name + ".lock")
        if self.path.is_symlink() or lock_path.is_symlink():
            raise RuntimeStateError("candidate sequence file is a symlink")
        try:
            descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        except OSError as error:
            if error.errno == errno.ELOOP:
                raise RuntimeStateError("candidate sequence lock file is a symlink") from error
            raise
        with os.fdopen(descriptor, "a+b") as lock:
            deadline = time.monotonic() + self.lock_timeout_seconds
            while True:
                try:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise RuntimeStateError("candidate sequence lock wait timed out") from None
                    time.sleep(_LOCK_POLL_SECONDS)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _read(self) -> int:
        if not self.path.exists():
            return 0
        try:
            if self.path.is_symlink() or not self.path.is_file() or self.path.stat().st_size > 65536:
                raise ValueError("candidate sequence file is not canonical")
            value = json.loads(self.path.read_bytes())
        except (OSError, ValueError) as error:
            raise RuntimeStateError("candidate sequence evidence is unreadable") from error
        if not isinstance(value, dict) or value.get("schema") != SCHEMA:
            raise RuntimeStateError("candidate sequence evidence has an unsupported schema")
        sequence = value.get("sequence")
        if type(sequence) is not int or sequence < 0:
            raise RuntimeStateError("candidate sequence evidence is invalid")
        return sequence

    def next(self) -> int:
        """Allocate the next global sequence, durably, with a cross-process lock."""
        with self._file_lock():
            sequence = self._read() + 1
            write_json_atomic(
                self.path,
                {
                    "schema": SCHEMA,
                    "sequence": sequence,
                    "updated_unix": time.time(),
                },
            )
            return sequence


def next_global_sequence(root: Path) -> int:
    """Convenience wrapper for one allocation from ``root``."""
    return GlobalSequence(root).next()
