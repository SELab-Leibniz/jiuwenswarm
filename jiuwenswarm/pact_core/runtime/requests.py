"""Durable caller identity; plan evolution never creates a second work identity."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from ..artifacts import read_json, write_json_atomic
from ..kernel import workspace_identity
from .boundary import _named_lock
from .schemas import REQUEST_IDENTITY_SCHEMA, RuntimeContractError, RuntimeStateError
from .store import CampaignReader


@dataclass(frozen=True)
class RequestIdentity:
    campaign_id: str
    source_basis: dict[str, object]
    replay: bool
    record: dict[str, object]


def publish_creation_directory(reader: CampaignReader, identity: dict[str, object]) -> None:
    """Publish a new directory with ownership already present, under Campaign lock.

    A failed first ownership write must not strand an unowned canonical directory.
    Abandoned temporary siblings are never used as recovery evidence.
    """
    destination = reader.paths.campaign_dir
    destination.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".creating-", dir=destination.parent) as temporary:
        pending = Path(temporary)
        write_json_atomic(pending / "creation-request.json", identity)
        if destination.exists():
            raise RuntimeContractError("request identity conflicts with existing Campaign ownership")
        pending.rename(destination)


@contextmanager
def request_identity(workspace: Path, request_id: str, inputs: dict[str, object], *, campaign_id: str | None) -> Iterator[RequestIdentity]:
    if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 512:
        raise RuntimeContractError("request_id must be a non-empty string of at most 512 characters")
    key = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
    digest = hashlib.sha256(json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    selected = campaign_id or f"request-{key[:24]}"
    reader = CampaignReader(workspace, selected)
    path = reader.paths.runtime_root / "requests" / f"{key}.json"
    with _named_lock(reader, f"request-{key}"):
        if path.exists():
            record = read_json(path)
            if (record.get("schema") != REQUEST_IDENTITY_SCHEMA or record.get("request_id") != request_id
                    or not isinstance(record.get("campaign_id"), str)
                    or not isinstance(record.get("source_basis"), dict)):
                raise RuntimeStateError("invalid durable request identity")
            if record.get("inputs_sha256") != digest or campaign_id is not None and record["campaign_id"] != campaign_id:
                raise RuntimeContractError("request identity conflicts with immutable creation inputs")
            basis = record["source_basis"]
            assert isinstance(basis, dict)
            yield RequestIdentity(str(record["campaign_id"]), dict(basis), True, record)
            return
        basis = workspace_identity(workspace)
        record = {"schema": REQUEST_IDENTITY_SCHEMA, "request_id": request_id,
            "campaign_id": selected, "inputs_sha256": digest, "source_basis": basis}
        write_json_atomic(path, record)
        yield RequestIdentity(selected, basis, False, record)
