"""Campaign executor exclusion and operation-bound interruption journals."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
import uuid

from ..artifacts import read_json, write_json_atomic
from ..kernel import workspace_identity
from .schemas import OPERATION_SCHEMA, RuntimeStateError
from .store import CampaignReader


@contextmanager
def campaign_lock(reader: CampaignReader) -> Iterator[None]:
    with _named_lock(reader, reader.campaign_id):
        yield


@contextmanager
def executor_lock(reader: CampaignReader) -> Iterator[None]:
    """One caller drives a Campaign, including the gaps between advances."""
    with _named_lock(reader, f"{reader.campaign_id}.executor"):
        yield


@contextmanager
def _named_lock(reader: CampaignReader, name: str) -> Iterator[None]:
    # POSIX advisory locks release on process death. No stale PID/lock deletion.
    try:
        import fcntl
    except ImportError as exc:
        raise RuntimeStateError("Campaign advisory locking requires POSIX") from exc
    path = reader.paths.runtime_root / "locks" / f"{name}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeStateError("Campaign is locked by another operation") from exc
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def marker_path(reader: CampaignReader) -> Path:
    return reader.paths.campaign_dir / "operation.json"


def require_clear_boundary(reader: CampaignReader) -> None:
    if marker_path(reader).exists():
        raise RuntimeStateError("interrupted operation requires recover, explicit reset or source adopt")


def assert_source_basis(reader: CampaignReader, state: dict[str, object]) -> None:
    if workspace_identity(reader.workspace) != state.get("workspace_basis"):
        raise RuntimeStateError("source basis changed; explicit User adopt is required")


def begin_operation(reader: CampaignReader, state: dict[str, object], *, action: str, parameters: dict[str, object] | None = None) -> str:
    from .recovery import artifact_digest
    require_clear_boundary(reader)
    assert_source_basis(reader, state)
    identity = uuid.uuid4().hex
    write_json_atomic(marker_path(reader), {
        "schema": OPERATION_SCHEMA, "campaign_id": reader.campaign_id,
        "id": identity, "expected_revision": state["revision"],
        "source_basis": state["workspace_basis"], "action": action, "parameters": parameters or {},
        "input_state_ref": state["snapshot_ref"],
        "input_state_sha256": artifact_digest(reader.resolve_ref(state["snapshot_ref"])),
        "phase": "started", "target": None, "result": None, "promotion": None,
        "kernel_invocation": None,
        "created_at": datetime.now(UTC).isoformat(),
    })
    return identity


def read_operation(reader: CampaignReader) -> dict[str, object]:
    marker = read_json(marker_path(reader))
    if marker.get("schema") != OPERATION_SCHEMA or marker.get("campaign_id") != reader.campaign_id:
        raise RuntimeStateError("invalid interrupted operation identity")
    revision = marker.get("expected_revision")
    if (isinstance(revision, bool) or not isinstance(revision, int) or revision < 0
            or not isinstance(marker.get("id"), str) or not marker["id"]
            or not isinstance(marker.get("source_basis"), dict)
            or not isinstance(marker.get("action"), str)):
        raise RuntimeStateError("invalid interrupted operation fields")
    return marker


def finish_operation(reader: CampaignReader, identity: str) -> None:
    operation = read_operation(reader)
    if operation.get("id") != identity:
        raise RuntimeStateError("operation ownership changed")
    archive = reader.paths.campaign_dir / "operations" / f"{identity}.json"
    if archive.exists() and read_json(archive) != operation:
        raise RuntimeStateError("archived operation does not match completed operation")
    write_json_atomic(archive, operation)
    marker_path(reader).unlink()
