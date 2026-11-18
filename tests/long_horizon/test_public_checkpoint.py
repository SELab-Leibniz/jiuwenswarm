"""Offline resume checks for independently reviewed public verifiers."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from jiuwenswarm.long_horizon.clarification import public_checkpoint
from jiuwenswarm.long_horizon.clarification.public_checkpoint import (
    PublicCheckpointError, PublicCriterionStore,
)
from jiuwenswarm.long_horizon.clarification.types import PactAcceptanceCriterion
from jiuwenswarm.long_horizon.clarification.verification import PublicVerification


def sha(value) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()


@pytest.fixture
def identity():
    return {
        "snapshot_manifest_hash": "1" * 64,
        "prompt_sha256": "2" * 64,
        "worker_identity": {"model": "fixture-model", "config_sha256": "3" * 64, "temperature": 1},
    }


@pytest.fixture
def criterion():
    return PactAcceptanceCriterion(id="api-value", text="The public value API returns the requested value.")


@pytest.fixture
def verification(criterion):
    return PublicVerification.model_validate({
        "schema": "jiuwenswarm/pact-public-verification/v1",
        "status": "ready", "reason": "Reviewed against the inspected public API and requirement.",
        "checks": [{
            "id": "check-value", "command": "python - <<'PY'\nfrom api import value\nassert value() == 2\nPY",
            "target_ac_ids": [criterion.id], "rationale": "Calls the documented API and asserts its value.",
        }],
    })


@pytest.fixture
def provenance():
    return {
        "independent_review": True,
        "generation": {"stage": "public-verification-ac1", "session_id": "native-generation"},
        "review": {"stage": "public-verification-review-ac1", "session_id": "native-review"},
    }


@pytest.fixture
def store(tmp_path, identity):
    return PublicCriterionStore(tmp_path / "checkpoints", identity)


def test_only_complete_reviewed_criterion_is_reused_with_command_bytes_intact(
    store, criterion, verification, provenance,
):
    assert store.load(criterion) is None
    path = store.save(criterion, verification, provenance)
    loaded = store.load(criterion)
    assert loaded == verification
    assert loaded.checks[0].command.encode() == verification.checks[0].command.encode()
    record = json.loads(path.read_bytes())
    assert record["status"] == "reviewed"
    assert record["criterion"] == criterion.model_dump(mode="json")
    assert record["identity_sha256"] == sha(record["identity"])
    assert record["criterion_sha256"] == sha(record["criterion"])
    assert record["value_sha256"] == sha(record["value"])
    assert path == store.directory / f"{sha(record['criterion'])}.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.root.stat().st_mode) == 0o700


def test_receipt_binds_exact_stored_bytes_not_reencoded_json(
    store, criterion, verification, provenance,
):
    assert store.load_with_receipt(criterion) is None
    path = store.save(criterion, verification, provenance)
    record = json.loads(path.read_bytes())
    # A legal formatting change must affect the file receipt, not the value.
    formatted = (json.dumps(record, indent=2, sort_keys=False) + "\n").encode()
    path.write_bytes(formatted)
    value, receipt = store.load_with_receipt(criterion)
    assert value == verification
    assert receipt == {
        "path": str(path), "sha256": hashlib.sha256(formatted).hexdigest(),
        "identity_sha256": sha(record["identity"]),
        "criterion_sha256": sha(record["criterion"]),
    }
    assert Path(receipt["path"]).is_absolute()
    assert receipt["sha256"] != sha(record)
    assert store.load(criterion) == value


def test_receipt_uses_one_nofollow_read_and_never_reopens_by_path(
    store, criterion, verification, provenance, monkeypatch,
):
    path = store.save(criterion, verification, provenance)
    expected_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    original_open = os.open
    opens = []

    def observed_open(name, flags, *args, **kwargs):
        if name == path.name:
            opens.append((flags, kwargs.get("dir_fd")))
        return original_open(name, flags, *args, **kwargs)

    def forbidden_path_read(*_args, **_kwargs):
        raise AssertionError("receipt must use the same secure file read")

    monkeypatch.setattr(os, "open", observed_open)
    monkeypatch.setattr(Path, "read_bytes", forbidden_path_read)
    value, receipt = store.load_with_receipt(criterion)
    assert value == verification
    assert receipt["sha256"] == expected_sha256
    assert len(opens) == 1
    assert opens[0][0] & os.O_NOFOLLOW
    assert opens[0][1] is not None


@pytest.mark.parametrize("attestation", [{}, {"independent_review": False}, {"independent_review": 1}])
def test_unreviewed_values_are_never_saved(store, criterion, verification, attestation):
    with pytest.raises(PublicCheckpointError):
        store.save(criterion, verification, attestation)
    assert store.load(criterion) is None


def test_identical_save_preserves_original_review_receipts(store, criterion, verification, provenance):
    path = store.save(criterion, verification, provenance)
    before = path.read_bytes()
    previous_mtime = path.stat().st_mtime_ns
    assert store.save(criterion, verification, {"independent_review": True, "other": "new receipt"}) == path
    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == previous_mtime


def test_conflicting_accepted_value_cannot_be_overwritten(store, criterion, verification, provenance):
    path = store.save(criterion, verification, provenance)
    before = path.read_bytes()
    changed = verification.model_copy(update={"reason": "A different reviewed replacement."})
    with pytest.raises(PublicCheckpointError, match="conflicting"):
        store.save(criterion, changed, provenance)
    assert path.read_bytes() == before
    assert store.load(criterion) == verification


@pytest.mark.parametrize("field", ["snapshot_manifest_hash", "prompt_sha256", "worker_identity"])
def test_snapshot_full_prompt_and_worker_changes_isolate_cache(
    tmp_path, identity, criterion, verification, provenance, field,
):
    initial = PublicCriterionStore(tmp_path / "cache", identity)
    original = initial.save(criterion, verification, provenance)
    changed = json.loads(json.dumps(identity))
    changed[field] = {"model": "a-different-model"} if field == "worker_identity" else "9" * 64
    updated = PublicCriterionStore(tmp_path / "cache", changed)
    assert updated.directory != initial.directory
    assert updated.load(criterion) is None
    # Even explicitly transplanting a valid record cannot make it reusable.
    forged = updated.directory / original.name
    forged.write_bytes(original.read_bytes())
    forged.chmod(0o600)
    with pytest.raises(PublicCheckpointError, match="identity mismatch"):
        updated.load(criterion)


def test_full_criterion_text_is_part_of_identity(store, criterion, verification, provenance):
    original = store.save(criterion, verification, provenance)
    changed = criterion.model_copy(update={"text": criterion.text + " Negative inputs must raise ValueError."})
    assert store.load(changed) is None
    forged = store.directory / f"{sha(changed.model_dump(mode='json'))}.json"
    forged.write_bytes(original.read_bytes())
    forged.chmod(0o600)
    with pytest.raises(PublicCheckpointError, match="criterion mismatch"):
        store.load(changed)


def test_identity_order_is_canonical_and_mutating_callers_cannot_change_it(tmp_path, identity, criterion):
    store = PublicCriterionStore(tmp_path / "cache", identity)
    reversed_identity = {key: identity[key] for key in reversed(identity)}
    same = PublicCriterionStore(tmp_path / "cache", reversed_identity)
    assert store.directory == same.directory
    identity["worker_identity"]["model"] = "changed after construction"
    exposed = store.identity
    exposed["worker_identity"]["model"] = "changed detached property"
    assert store.identity["worker_identity"]["model"] == "fixture-model"
    assert store.load(criterion) is None


@pytest.mark.parametrize("mutation", [
    lambda r: r.update(schema="chrys/reviewed-public-criterion/v1"),
    lambda r: r.update(status="generated"),
    lambda r: r.update(identity_sha256="0" * 64),
    lambda r: r["identity"]["worker_identity"].update(temperature=True),
    lambda r: r.update(criterion_sha256="0" * 64),
    lambda r: r["criterion"].update(text="Changed criterion obligation"),
    lambda r: r.update(value_sha256="0" * 64),
    lambda r: r["value"].update(reason="Tampered value"),
    lambda r: r["provenance"].update(independent_review=False),
    lambda r: r.update(unknown="unexpected record field"),
    lambda r: r.pop("provenance"),
])
def test_corrupt_records_fail_closed_and_cannot_be_replaced(
    store, criterion, verification, provenance, mutation,
):
    path = store.save(criterion, verification, provenance)
    record = json.loads(path.read_bytes())
    mutation(record)
    path.write_text(json.dumps(record))
    broken = path.read_bytes()
    with pytest.raises(PublicCheckpointError):
        store.load(criterion)
    with pytest.raises(PublicCheckpointError):
        store.save(criterion, verification, provenance)
    assert path.read_bytes() == broken


@pytest.mark.parametrize("mutation", [
    lambda r: r["value"].update(status="unavailable", checks=[]),
    lambda r: r["value"]["checks"][0].update(target_ac_ids=["foreign-criterion"]),
    lambda r: r["value"]["checks"][0].update(command="true"),
    lambda r: r["value"]["checks"][0].update(command="python -m pytest || true"),
    lambda r: r["value"]["checks"][0].update(unknown="extra field"),
    lambda r: r["value"].update(reason="  noncanonical whitespace  "),
])
def test_recomputed_hash_cannot_bypass_schema_coverage_or_command_guards(
    store, criterion, verification, provenance, mutation,
):
    path = store.save(criterion, verification, provenance)
    record = json.loads(path.read_bytes())
    mutation(record)
    record["value_sha256"] = sha(record["value"])
    path.write_text(json.dumps(record))
    with pytest.raises(PublicCheckpointError):
        store.load(criterion)


@pytest.mark.parametrize("command", ["true", "python -m pytest || true"])
def test_non_verifiers_are_rejected_before_save(store, criterion, verification, provenance, command):
    verification.checks[0].command = command
    with pytest.raises(PublicCheckpointError):
        store.save(criterion, verification, provenance)
    assert store.load(criterion) is None


@pytest.mark.parametrize("payload", [b"", b"[1, 2]", b'{"schema": "a", "schema": "b"}',
    b'{"number": NaN}', b'{"number": 1e999}', b"\xff", b"{" * 1100])
def test_malformed_bounded_json_fails_closed(store, criterion, verification, provenance, payload):
    path = store.save(criterion, verification, provenance)
    path.write_bytes(payload)
    with pytest.raises(PublicCheckpointError):
        store.load(criterion)


def test_oversized_record_is_rejected_before_decoding(store, criterion, verification, provenance):
    path = store.save(criterion, verification, provenance)
    with path.open("r+b") as stream:
        stream.truncate(public_checkpoint.MAX_CHECKPOINT_BYTES + 1)
    with pytest.raises(PublicCheckpointError, match="size limit"):
        store.load(criterion)


@pytest.mark.parametrize("kind", ["symlink", "dangling", "hardlink", "directory", "fifo"])
def test_unsafe_leaf_is_never_read_or_overwritten(
    store, criterion, verification, provenance, tmp_path, kind,
):
    path = store.save(criterion, verification, provenance)
    target = tmp_path / "outside.json"
    target.write_bytes(path.read_bytes())
    target.chmod(0o600)
    before = target.read_bytes()
    path.unlink()
    if kind == "symlink":
        path.symlink_to(target)
    elif kind == "dangling":
        path.symlink_to(tmp_path / "absent.json")
    elif kind == "hardlink":
        os.link(target, path)
    elif kind == "directory":
        path.mkdir()
    else:
        os.mkfifo(path, mode=0o600)
    with pytest.raises(PublicCheckpointError):
        store.load(criterion)
    with pytest.raises(PublicCheckpointError):
        store.save(criterion, verification, provenance)
    assert target.read_bytes() == before


def test_symlink_root_or_ancestor_is_rejected_before_creation(tmp_path, identity):
    outside = tmp_path / "outside"
    outside.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(outside, target_is_directory=True)
    for root in (alias, alias / "new-child"):
        with pytest.raises(PublicCheckpointError, match="directory"):
            PublicCriterionStore(root, identity)
    assert list(outside.iterdir()) == []


def test_swapped_identity_directory_cannot_redirect_load_or_save(
    store, criterion, verification, provenance, tmp_path,
):
    path = store.save(criterion, verification, provenance)
    moved = tmp_path / "moved-records"
    store.directory.rename(moved)
    store.directory.symlink_to(moved, target_is_directory=True)
    before = (moved / path.name).read_bytes()
    with pytest.raises(PublicCheckpointError):
        store.load(criterion)
    with pytest.raises(PublicCheckpointError):
        store.save(criterion, verification, provenance)
    assert (moved / path.name).read_bytes() == before


def test_insecure_record_permissions_fail_closed(store, criterion, verification, provenance):
    path = store.save(criterion, verification, provenance)
    path.chmod(0o644)
    with pytest.raises(PublicCheckpointError, match="owner-only"):
        store.load(criterion)


def test_simultaneous_conflicting_reviews_preserve_first_accepted_record(
    tmp_path, identity, criterion, verification, provenance,
):
    stores = [PublicCriterionStore(tmp_path / "cache", identity) for _ in range(2)]
    values = [verification, verification.model_copy(update={"reason": "Independent conflicting review."})]
    barrier = Barrier(2)

    def save(index):
        barrier.wait(timeout=5)
        try:
            return stores[index].save(criterion, values[index], provenance)
        except PublicCheckpointError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(save, range(2)))
    assert sum(isinstance(outcome, Path) for outcome in outcomes) == 1
    assert sum(isinstance(outcome, PublicCheckpointError) for outcome in outcomes) == 1
    winner = next(index for index, outcome in enumerate(outcomes) if isinstance(outcome, Path))
    assert stores[0].load(criterion) == values[winner]
    assert len(list(stores[0].directory.iterdir())) == 1


@pytest.mark.parametrize("patch", [
    {"snapshot_manifest_hash": "not a hash"}, {"prompt_sha256": ""},
    {"worker_identity": {}}, {"worker_identity": None},
    {"worker_identity": {"model": "fixture", "temperature": float("nan")}},
    {"worker_identity": {1: "non-string key"}},
])
def test_invalid_or_incomplete_identities_are_not_checkpointable(tmp_path, identity, patch):
    identity.update(patch)
    with pytest.raises(PublicCheckpointError):
        PublicCriterionStore(tmp_path / "cache", identity)
    assert not (tmp_path / "cache").exists()
