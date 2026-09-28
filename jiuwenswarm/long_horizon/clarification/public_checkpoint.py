"""Controller-owned checkpoints for independently reviewed public criteria.

The resume contract follows Chrys 0561cf0f's reviewed-public-criterion cache.
This store does not perform a review: callers must save only the replacement
verifier returned by the independent review, after its source-evidence gates.
No draft or partially completed criterion is eligible for reuse.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .compat import (
    _open_posix_directory,
    atomic_write_owner_only_bytes,
    ensure_owner_only_directory,
)
from .types import PactAcceptanceCriterion
from .verification import PublicVerification

CHECKPOINT_SCHEMA = "jiuwenswarm/reviewed-public-criterion/v1"
MAX_CHECKPOINT_BYTES = 8 * 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_RECORD_FIELDS = {
    "schema", "status", "identity", "identity_sha256", "criterion",
    "criterion_sha256", "value", "value_sha256", "provenance",
}


class PublicCheckpointError(ValueError):
    """A checkpoint is unsafe, corrupt, unreviewed, or conflicts with accepted data."""


def _canonical(value: Any) -> bytes:
    try:
        payload = json.dumps(
            value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, RecursionError) as exc:
        raise PublicCheckpointError("checkpoint data must be finite JSON") from exc
    if len(payload) > MAX_CHECKPOINT_BYTES:
        raise PublicCheckpointError("public criterion checkpoint exceeds the size limit")
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise PublicCheckpointError("checkpoint JSON object keys must be strings")
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
        elif item is not None and not isinstance(item, (str, bool, int, float)):
            raise PublicCheckpointError("checkpoint data must contain only JSON values")
    return payload


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _object(payload: bytes) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise PublicCheckpointError("duplicate checkpoint JSON key")
            result[key] = value
        return result

    def constant(_value: str) -> None:
        raise PublicCheckpointError("checkpoint JSON contains a non-finite number")

    try:
        value = json.loads(payload, object_pairs_hook=pairs, parse_constant=constant)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise PublicCheckpointError("invalid public criterion checkpoint JSON") from exc
    if not isinstance(value, dict):
        raise PublicCheckpointError("public criterion checkpoint must be a JSON object")
    _canonical(value)  # Also rejects overflowing JSON floats such as 1e999.
    return value


def _json_copy(value: dict[str, Any], name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not value:
        raise PublicCheckpointError(f"checkpoint {name} must be a nonempty JSON object")
    if any(not isinstance(key, str) for key in value):
        raise PublicCheckpointError(f"checkpoint {name} keys must be strings")
    return _object(_canonical(value))


def _reviewed_provenance(value: dict[str, Any]) -> dict[str, Any]:
    result = _json_copy(value, "provenance")
    if result.get("independent_review") is not True:
        raise PublicCheckpointError("only independently reviewed public criteria may be saved")
    return result


class PublicCriterionStore:
    """Resume completed criterion reviews under an exact input/runtime identity.

    ``identity`` must contain SHA-256 strings ``snapshot_manifest_hash`` and
    ``prompt_sha256``, plus a nonempty ``worker_identity`` string or JSON object.
    The prompt hash must cover the complete generation context: the original
    requirement, immutable Goal Contract, Initial Plan, runtime evidence, and
    frozen baseline. The worker identity must bind the model and configuration;
    credentials must never be included. Every additional identity field is also
    hashed. A changed identity or full criterion gets its own checkpoint path.

    Corrupt or unsafe existing records fail closed. Files are private and saves
    are atomic and serialized; a conflicting accepted value is never replaced.
    These filesystem checks do not sandbox a hostile process running as owner.
    """

    def __init__(self, root: Path, identity: dict[str, Any]) -> None:
        self._identity = _json_copy(identity, "identity")
        for key in ("snapshot_manifest_hash", "prompt_sha256"):
            value = self._identity.get(key)
            if not isinstance(value, str) or not _SHA256.fullmatch(value):
                raise PublicCheckpointError(f"checkpoint identity requires a SHA-256 {key}")
        worker = self._identity.get("worker_identity")
        if not ((isinstance(worker, str) and worker.strip()) or (isinstance(worker, dict) and worker)):
            raise PublicCheckpointError("checkpoint identity requires a nonempty worker_identity")
        self.identity_sha256 = _digest(self._identity)
        self.root = Path(os.path.abspath(root))
        self.directory = self.root / self.identity_sha256
        try:
            ensure_owner_only_directory(self.root)
            ensure_owner_only_directory(self.directory)
        except OSError as exc:
            raise PublicCheckpointError("unsafe public criterion checkpoint directory") from exc

    @property
    def identity(self) -> dict[str, Any]:
        """Return a detached copy; a store's identity cannot drift after creation."""
        return _object(_canonical(self._identity))

    @staticmethod
    def _criterion(criterion: PactAcceptanceCriterion) -> tuple[dict[str, Any], str]:
        try:
            value = criterion.model_dump(mode="json")
            PactAcceptanceCriterion.model_validate(value, strict=True)
        except (AttributeError, TypeError, ValueError) as exc:
            raise PublicCheckpointError("invalid checkpoint acceptance criterion") from exc
        return value, _digest(value)

    @contextmanager
    def _locked_directory(self, *, exclusive: bool) -> Iterator[int]:
        try:
            fd = _open_posix_directory(self.directory, create=False)
        except OSError as exc:
            raise PublicCheckpointError("unsafe public criterion checkpoint directory") from exc
        try:
            info = os.fstat(fd)
            if info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise PublicCheckpointError("public criterion checkpoint directory must be owner-only")
            fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            yield fd
        except OSError as exc:
            raise PublicCheckpointError("public criterion checkpoint storage failed") from exc
        finally:
            os.close(fd)

    @staticmethod
    def _read(directory_fd: int, name: str) -> tuple[dict[str, Any], str] | None:
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(name, flags, dir_fd=directory_fd)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise PublicCheckpointError("unsafe public criterion checkpoint file") from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise PublicCheckpointError("public criterion checkpoint must be a regular file with one link")
            if info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise PublicCheckpointError("public criterion checkpoint file must be owner-only")
            if info.st_size > MAX_CHECKPOINT_BYTES:
                raise PublicCheckpointError("public criterion checkpoint exceeds the size limit")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                payload = stream.read(MAX_CHECKPOINT_BYTES + 1)
            if len(payload) > MAX_CHECKPOINT_BYTES:
                raise PublicCheckpointError("public criterion checkpoint exceeds the size limit")
            return _object(payload), hashlib.sha256(payload).hexdigest()
        finally:
            os.close(fd)

    @staticmethod
    def _verification(value: dict[str, Any], criterion_id: str) -> PublicVerification:
        try:
            result = PublicVerification.model_validate(value, strict=True)
            result.validate_coverage({criterion_id})
            result.validate_generated_commands()
            if _canonical(result.model_dump(mode="json", by_alias=True)) != _canonical(value):
                raise PublicCheckpointError("checkpoint verification value is not schema-normalized")
        except (TypeError, ValueError) as exc:
            raise PublicCheckpointError("invalid reviewed public verification value") from exc
        return result

    def _validate(
        self, record: dict[str, Any], criterion: dict[str, Any], criterion_sha256: str,
    ) -> PublicVerification:
        if set(record) != _RECORD_FIELDS:
            raise PublicCheckpointError("invalid reviewed public criterion checkpoint fields")
        if record["schema"] != CHECKPOINT_SCHEMA or record["status"] != "reviewed":
            raise PublicCheckpointError("invalid reviewed public criterion checkpoint status/schema")
        if _digest(record["identity"]) != self.identity_sha256 or record["identity_sha256"] != self.identity_sha256:
            raise PublicCheckpointError("reviewed public criterion checkpoint identity mismatch")
        if _digest(record["criterion"]) != criterion_sha256 or record["criterion_sha256"] != criterion_sha256:
            raise PublicCheckpointError("reviewed public criterion checkpoint criterion mismatch")
        if _digest(record["value"]) != record["value_sha256"]:
            raise PublicCheckpointError("reviewed public criterion checkpoint value hash mismatch")
        _reviewed_provenance(record["provenance"])
        return self._verification(record["value"], criterion["id"])

    def load(self, criterion: PactAcceptanceCriterion) -> PublicVerification | None:
        """Return an exact reviewed value, or None only when the record is absent."""
        loaded = self.load_with_receipt(criterion)
        return loaded[0] if loaded is not None else None

    def load_with_receipt(
        self, criterion: PactAcceptanceCriterion,
    ) -> tuple[PublicVerification, dict[str, str]] | None:
        """Return a validated value and provenance of this exact secure file read.

        ``sha256`` hashes the complete stored bytes, including their formatting;
        it is not the semantic value hash. The receipt contains no historical
        usage, so callers can report reuse without charging prior model calls.
        """
        criterion_value, criterion_sha256 = self._criterion(criterion)
        path = self.directory / f"{criterion_sha256}.json"
        with self._locked_directory(exclusive=False) as fd:
            loaded = self._read(fd, path.name)
            if loaded is None:
                return None
            record, file_sha256 = loaded
            value = self._validate(record, criterion_value, criterion_sha256)
            return value, {
                "path": str(path), "sha256": file_sha256,
                "identity_sha256": self.identity_sha256, "criterion_sha256": criterion_sha256,
            }

    def save(
        self, criterion: PactAcceptanceCriterion, value: PublicVerification, provenance: dict[str, Any],
    ) -> Path:
        """Persist a review's validated replacement, never an unreviewed draft.

        The caller explicitly attests ``provenance['independent_review'] is True``
        and should include the independent native review's stage/session receipts.
        Re-saving the same accepted value is a no-op, preserving original evidence.
        """
        criterion_value, criterion_sha256 = self._criterion(criterion)
        reviewed = _reviewed_provenance(provenance)
        value_data = value.model_dump(mode="json", by_alias=True)
        self._verification(value_data, criterion_value["id"])
        record = {
            "schema": CHECKPOINT_SCHEMA, "status": "reviewed",
            "identity": self.identity, "identity_sha256": self.identity_sha256,
            "criterion": criterion_value, "criterion_sha256": criterion_sha256,
            "value": value_data, "value_sha256": _digest(value_data), "provenance": reviewed,
        }
        payload = _canonical(record)
        path = self.directory / f"{criterion_sha256}.json"
        with self._locked_directory(exclusive=True) as fd:
            existing = self._read(fd, path.name)
            if existing is not None:
                existing_record, _file_sha256 = existing
                self._validate(existing_record, criterion_value, criterion_sha256)
                if _canonical(existing_record["value"]) != _canonical(value_data):
                    raise PublicCheckpointError("conflicting reviewed public criterion checkpoint")
                return path
            atomic_write_owner_only_bytes(path, payload, create_parents=False)
        return path
