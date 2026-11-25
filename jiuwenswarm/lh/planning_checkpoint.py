# Copyright (c) 2026 Chrys. All rights reserved.

"""Reuse completed advisory stages within one frozen Work after an explicit resume."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any
from uuid import uuid4

from jiuwenswarm.pact_core.runtime import RuntimeContractError


def _bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


class PlanningCheckpointError(RuntimeContractError):
    """Corrupt own-work evidence must not degrade into an invalid model proposal."""


class NativeArtifactInvalid(PlanningCheckpointError):
    """A native model artifact failed its own contract; the evidence itself is intact.

    It is never saved as completed planning. Callers that can proceed without
    that optional artifact (public verification) may degrade explicitly.
    """


class PlanningCheckpoint:
    """Own-work stage evidence; never a shared memory or a substitute for validation."""

    def __init__(self, session_dir: Path, binding: dict[str, Any], *, max_bytes: int = 200_000) -> None:
        self.max_bytes = max_bytes
        self.root = session_dir.absolute() / "completed-planning"
        if self.root.is_symlink():
            raise PlanningCheckpointError("planning checkpoint path is a symlink")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.stat().st_mode & 0o077:
            raise PlanningCheckpointError("planning checkpoint directory must be owner-only")
        self.binding = hashlib.sha256(_bytes({**binding, "work_scope": str(self.root)})).hexdigest()
        self.reused: list[dict[str, str]] = []

    def _path(self, stage: str) -> Path:
        return self.root / f"{self.binding}-{hashlib.sha256(stage.encode()).hexdigest()}.json"

    def has(self, stage: str) -> bool:
        """Report durable presence without reading or accepting the payload.

        Callers use this only to classify whether a public-review stage was
        entered before an outer timeout. A corrupt record still fails closed
        when the normal ``read`` path consumes it.
        """
        try:
            self._path(stage).lstat()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise PlanningCheckpointError("invalid planning checkpoint evidence") from exc
        return True

    def read(self, stage: str) -> Any | None:
        path = self._path(stage)
        try:
            before = path.lstat()
        except FileNotFoundError:
            return None
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as source:
                opened = os.fstat(source.fileno())

                def identity(info):
                    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_nlink != 1
                    or opened.st_mode & 0o077
                    or opened.st_size > self.max_bytes
                    or identity(before) != identity(opened)
                ):
                    raise ValueError("unsafe checkpoint identity")
                raw = source.read(self.max_bytes + 1)
                after = os.fstat(source.fileno())
                if (
                    len(raw) > self.max_bytes
                    or len(raw) != opened.st_size
                    or identity(opened) != identity(after)
                    or identity(opened) != identity(path.lstat())
                ):
                    raise ValueError("checkpoint changed during read")
            row = json.loads(raw)
            if (
                row["schema"] != "long-horizon/completed-planning-stage/v1"
                or row["binding"] != self.binding
                or row["stage"] != stage
                or row["payload_sha256"] != hashlib.sha256(_bytes(row["payload"])).hexdigest()
            ):
                raise ValueError("checkpoint binding or payload mismatch")
        except (OSError, ValueError, KeyError, TypeError, RecursionError) as exc:
            raise PlanningCheckpointError("invalid planning checkpoint evidence") from exc
        self.reused.append({"stage": stage, "path": str(path), "payload_sha256": row["payload_sha256"]})
        return row["payload"]

    def save(self, stage: str, payload: Any) -> None:
        row = {
            "schema": "long-horizon/completed-planning-stage/v1",
            "binding": self.binding,
            "stage": stage,
            "payload": payload,
            "payload_sha256": hashlib.sha256(_bytes(payload)).hexdigest(),
        }
        data = _bytes(row)
        if len(data) > self.max_bytes:
            raise PlanningCheckpointError("planning checkpoint exceeds evidence bound")
        path = self._path(stage)
        if path.exists() or path.is_symlink():
            if self.read(stage) != payload:
                raise PlanningCheckpointError("completed planning checkpoint is immutable")
            return
        # Publish only complete bytes. A crash while writing leaves a temporary
        # file rather than a corrupt accepted checkpoint. Linking is exclusive,
        # so even competing writers cannot replace prior evidence.
        temporary = self.root / (".planning-" + uuid4().hex + ".tmp")
        try:
            with temporary.open("xb") as handle:
                os.chmod(temporary, 0o600)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                if self.read(stage) != payload:
                    raise PlanningCheckpointError("completed planning checkpoint is immutable") from None
        finally:
            temporary.unlink(missing_ok=True)
            directory_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
