# Copyright (c) 2026 Chrys. All rights reserved.

"""Recover own sealed Core candidates without granting acceptance or a grade."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from pact_core.kernel import default_worktree_root, read_work_candidate
from pact_core.runtime import RuntimeContractError, WorkRuntime, control_plane

from ..acceptance_timing import AcceptanceTimings
from ..check_prerequisites import observed_command
from ..checkpoint_guard import (
    CandidateTooLarge,
    ControllerMemoryPressure,
    check_controller_memory,
    prepare_checkpoint,
)
from ..snapshot_resources import SnapshotResources
from .global_sequence import GlobalSequence
from .safe_failure import package_source_files, safe_failure, write_metadata


def default_archive_root(output: Path) -> Path:
    """Reuse the existing SDK request/log mount; local runs retain local artifacts."""
    output = output.resolve()
    request = output.parent / "request.json"
    harbor_request = False
    try:
        if request.is_symlink() or request.stat().st_size > 65536:
            write_metadata(
                output,
                "candidate-archive-root-gap.json",
                {"schema": "harbor-runtime/archive-root-gap/v1", "reason": "request_not_canonical_or_bounded"},
            )
            return output / "candidate-artifacts"
        data = json.loads(request.read_bytes())
        if type(data) is not dict:
            return output / "candidate-artifacts"
        harbor_request = data.get("schema") == "harbor-runtime/agent-request/v1"
        evidence = Path(data["evidence_root"])
        if (
            data["schema"] == "harbor-runtime/agent-request/v1"
            and data["mode"] == "lh"
            and data["root"] == str(output.parent)
            and evidence.is_absolute()
            and evidence.resolve(strict=True) == evidence
            and evidence.is_dir()
        ):
            return evidence / "runtime-evidence" / "candidate-artifacts"
        if data.get("schema") == "harbor-runtime/agent-request/v1":
            write_metadata(
                output,
                "candidate-archive-root-gap.json",
                {"schema": "harbor-runtime/archive-root-gap/v1", "reason": "sdk_archive_destination_unavailable"},
            )
    except FileNotFoundError:
        if harbor_request:
            write_metadata(
                output,
                "candidate-archive-root-gap.json",
                {"schema": "harbor-runtime/archive-root-gap/v1", "reason": "sdk_archive_destination_unavailable"},
            )
    except (OSError, ValueError, TypeError, KeyError):
        write_metadata(
            output,
            "candidate-archive-root-gap.json",
            {"schema": "harbor-runtime/archive-root-gap/v1", "reason": "sdk_request_read_failed"},
        )
    return output / "candidate-artifacts"


def _candidate(value) -> dict:
    return {
        "work_id": value.work_id,
        "checkpoint": value.checkpoint,
        "tree": value.tree,
        "artifact": str(value.artifact),
    }


class CandidateArchive:
    def __init__(
        self,
        root: Path,
        *,
        deadline_unix: float | None,
        max_total_bytes: int,
        protected: tuple[Path, ...] = (),
        sequence_root: Path | None = None,
    ):
        self.root = root
        # One controller-owned counter orders gate attempts and archive entries.
        # The archive root may be outside <output>, so callers pass that output
        # explicitly; direct archive users fall back to the archive's parent.
        self.sequence_root = sequence_root if sequence_root is not None else root.parent
        self.deadline = deadline_unix
        self.limits = SnapshotResources(max_total_bytes)
        self.by_checkpoint = {}
        self.sequence = 0
        self.trusted = (*package_source_files(), WorkRuntime.save_candidate.__code__.co_filename)
        self.storage_failure = None
        self.disabled = False
        try:
            self.root = root.resolve()
            self.sequence_root = self.sequence_root.expanduser().resolve()
            if root.is_symlink():
                raise ValueError("candidate_archive_root_symlink")
            if any(self.root.is_relative_to(p.resolve()) or p.resolve().is_relative_to(self.root) for p in protected):
                raise ValueError("candidate_archive_overlaps_protected_source")
            self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
            self.sequence = self._read_sequence()
        except Exception as error:
            self.disabled = True
            self.storage_failure = safe_failure(error, trusted_files=self.trusted, stage="archive_initialize")

    def _read_sequence(self) -> int:
        """Recover the durable archive sequence from prior bundle sidecars."""
        highest = 0
        for sidecar in self.root.glob("*/archive.json"):
            try:
                if sidecar.is_symlink() or not sidecar.is_file() or sidecar.stat().st_size > 65536:
                    continue
                value = json.loads(sidecar.read_bytes())
                sequence = value.get("sequence") if isinstance(value, dict) else None
                if type(sequence) is int and sequence > highest:
                    highest = sequence
            except (OSError, ValueError):
                continue
        return highest

    def record(self, event: str, **values) -> None:
        if self.disabled:
            return
        value = {
            "schema": "harbor-runtime/candidate-lineage/v1",
            "event": event,
            "observed_unix": time.time(),
            "absolute_deadline_unix": self.deadline,
            "independent_reward": None,
            **values,
        }
        try:
            fd = os.open(self.root / "events.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "a") as stream:
                stream.write(json.dumps(value, allow_nan=False, sort_keys=True) + "\n")
                stream.flush()
        except Exception as error:
            self.storage_failure = safe_failure(error, trusted_files=self.trusted, stage="archive_record")

    def capture(self, runtime: WorkRuntime, state) -> None:
        candidate = state.candidate
        destination = self.root / (candidate.checkpoint + "-" + uuid4().hex)
        try:
            if self.disabled:
                return
            if self.deadline is None or time.time() >= self.deadline:
                self.record(
                    "archive_gap",
                    candidate=_candidate(candidate),
                    reason="original_deadline_unavailable" if self.deadline is None else "original_deadline_spent",
                )
                return
            # Public Core reader validates the sealed checkpoint; never copy the
            # live Worker checkout or infer a candidate from model output.
            sealed = read_work_candidate(candidate.artifact)
            if (
                sealed.namespace_violations
                or sealed.checkpoint != candidate.checkpoint
                or sealed.tree != candidate.tree
            ):
                raise ValueError("candidate_seal_not_exportable")
            # A sealed checkpoint is the reusable unit. Two commits can share a
            # tree while their candidate/verification identity differs, so a tree
            # alone must never reuse an older export receipt.
            key = (candidate.work_id, candidate.checkpoint, candidate.tree)
            if key in self.by_checkpoint:
                self.record(
                    "worker_sealed",
                    candidate=_candidate(candidate),
                    source_snapshot=self.by_checkpoint[key],
                    source_reused_by_sealed_tree=True,
                )
                return
            # This supported API independently validates the canonical candidate,
            # namespace and source basis, and atomically publishes its bundle.
            self.record("worker_sealed_pending_export", candidate=_candidate(candidate), destination=str(destination))
            receipt = WorkRuntime.export_candidate(runtime, candidate, destination)
            total = sum((destination / name).lstat().st_size for name in receipt.artifact_hashes)
            self.limits.check_total(total)
            sequence = self.sequence + 1
            self.sequence = sequence
            global_sequence = GlobalSequence(self.sequence_root).next()
            sidecar = {
                "schema": "harbor-runtime/candidate-archive-entry/v1",
                "sequence": sequence,
                "global_sequence": global_sequence,
                "candidate": _candidate(candidate),
                "export_receipt": str(receipt.artifact),
                "export_receipt_sha256": receipt.artifact_sha256,
                "snapshot": str(receipt.snapshot),
                "source_tree": candidate.tree,
                "internal_accepted": receipt.accepted,
                "independent_reward": None,
            }
            (destination / "archive.json").write_text(
                json.dumps(sidecar, allow_nan=False, sort_keys=True) + "\n", encoding="utf-8"
            )
            link = {
                "candidate_snapshot_id": "git:" + candidate.checkpoint,
                "sequence": sequence,
                "global_sequence": global_sequence,
                "snapshot": str(receipt.snapshot),
                "export_receipt": str(receipt.artifact),
                "export_receipt_sha256": receipt.artifact_sha256,
                "source_tree": candidate.tree,
                "source_bytes": total,
                "internal_accepted": receipt.accepted,
                "independent_reward": None,
                "within_original_deadline": time.time() <= self.deadline,
                "capacity_check": "after_canonical_Core_export_not_a_pre_copy_disk_quota",
            }
            self.by_checkpoint[key] = link
            self.record(
                "worker_sealed",
                candidate=_candidate(candidate),
                source_snapshot=link,
                source_reused_by_sealed_tree=False,
            )
        except Exception as error:
            # Core's original candidate and all previously completed exports are
            # retained. No retry, source restore or permission change is made.
            self.record(
                "archive_gap",
                candidate=_candidate(candidate),
                destination=str(destination),
                error=safe_failure(error, trusted_files=self.trusted, stage="sealed_candidate_export"),
            )


class EvidenceWorkRuntime(WorkRuntime):
    def __init__(self, workspace: Path, *, worktree_root: Path | None, archive: CandidateArchive):
        super().__init__(workspace, worktree_root=worktree_root)
        self.candidate_archive = archive
        self.acceptance_timings: AcceptanceTimings | None = None

    def accept_candidate(self, work_id, *, expected_revision, reviewer, timeout_seconds=3600.0, deadline=None):
        if self.acceptance_timings is not None:
            return self.acceptance_timings.measure(
                "accept", super().accept_candidate, self.workspace, work_id, expected_revision,
                reviewer=reviewer, timeout_seconds=timeout_seconds, deadline=deadline
            )
        return super().accept_candidate(work_id, expected_revision=expected_revision, reviewer=reviewer,
                                        timeout_seconds=timeout_seconds, deadline=deadline)

    def _config(self, store, state, op, *, full, timeout, deadline=None):
        config = super()._config(store, state, op, full=full, timeout=timeout, deadline=deadline)
        runner = Path(control_plane.__file__).with_name("check_runner.py")
        return replace(config, verify_command=observed_command(config, runner))

    def review_candidate(self, work_id, **kwargs):
        from ..method_routing import method_diagnosis

        state = self.read_work(work_id)
        methods, _ = method_diagnosis(state)
        if methods and kwargs.get("verification_evidence") == state.verification:
            kwargs["context"] = (kwargs.get("context") or "") + (
                "\nThe runner recorded failed method prerequisites: " + json.dumps(methods)
                + ". Independently inspect each method and distinguish its setup from candidate behavior. "
                "Propose a corrected check under the same name for each confirmed method issue, "
                "preserving its original requirement and behavioral assertion. A missing temporary "
                "directory is a diagnostic lead; check whether the failure occurred before the assertion. "
                "Identify any separate product failures for Worker repair. "
                "For an unavailable supplemental public_reference comparison, you may suspend that method "
                "with the observed reason and retain the original requirement for independent verification. "
                "A required_public_reference supplied by the task owner remains mandatory."
            )
        return super().review_candidate(work_id, **kwargs)

    def review_checks(self, work_id, *, expected_revision, decisions, review):
        from ..method_routing import method_diagnosis

        state = self.read_work(work_id)
        methods, _ = method_diagnosis(state)
        if methods and isinstance(decisions, list):
            from pact_core.runtime.checks import normalize_decisions

            # Validate the entire transaction before partitioning it, so
            # stale versions, duplicates and malformed decisions still fail.
            normalize_decisions(list(state.checks), decisions, str(review.artifact))
            old = {row["name"]: row for row in state.checks}
            allowed = []
            for decision in decisions:
                previous = old[decision["name"]]
                disposition = "admitted" if decision["action"] == "admit" else "suspended"
                reference_suspension = (
                    decision["action"] == "suspend" and methods.get(decision["name"], {}).get("kind") == "public_reference"
                )
                if ((decision["name"] not in methods or reference_suspension) and
                        (previous["disposition"] != disposition
                         or (previous.get("method_review") or {}).get("reason") != decision["reason"])):
                    allowed.append(decision)
            if not allowed:
                raise RuntimeContractError("a method with failed prerequisites needs a corrected definition")
            decisions = allowed
        return super().review_checks(work_id, expected_revision=expected_revision, decisions=decisions, review=review)

    def _mirror_guard_evidence(self) -> None:
        """Keep the guard evidence beside both the archive and controller output."""
        target = self.candidate_archive.sequence_root
        if target.resolve() == self.candidate_archive.root.resolve():
            return
        target.mkdir(parents=True, exist_ok=True)
        for name in (
            "checkpoint-exclusions.json",
            "candidate-too-large.json",
            "controller-memory-checkpoint.json",
        ):
            source = self.candidate_archive.root / name
            if not source.is_file():
                continue
            destination = target / name
            if destination.resolve() == source.resolve():
                continue
            shutil.copyfile(source, destination)

    def _reject_candidate(self, handle, error: Exception) -> None:
        """Close the started operation so a refused candidate cannot OOM later."""
        reason = "candidate_too_large" if isinstance(error, CandidateTooLarge) else "memory_pressure"
        evidence = getattr(error, "evidence", None)
        try:
            self._mirror_guard_evidence()
        except OSError:
            pass
        try:
            handle.close()
        except Exception:
            pass
        reset = "not_attempted"
        try:
            self.reset_operation(
                handle.work_id,
                expected_revision=handle.expected_revision,
                reason=reason,
                executor_stopped=True,
            )
            reset = "reset"
        except Exception as reset_error:
            reset = f"failed:{type(reset_error).__name__}"
        self.candidate_archive.record(
            reason,
            work_id=handle.work_id,
            operation_id=handle.operation_id,
            evidence=str(evidence) if evidence is not None else None,
            reset=reset,
            detail=str(error)[:1000],
        )

    def save_candidate(self, handle, *, summary: str, advisory: dict[str, object] | None = None):
        evidence_root = self.candidate_archive.root
        if handle.workdir.is_dir():
            try:
                prepare_checkpoint(
                    handle.workdir,
                    evidence_root,
                    base_commit=handle.input_checkpoint,
                    stage="save_candidate",
                )
            except (CandidateTooLarge, ControllerMemoryPressure) as error:
                self._reject_candidate(handle, error)
                raise
            self._mirror_guard_evidence()
        # super closes the handle and publishes the sealed candidate first.
        state = super().save_candidate(handle, summary=summary, advisory=advisory)
        self.candidate_archive.capture(self, state)
        try:
            check_controller_memory(evidence_root, stage="save_candidate_after")
        except ControllerMemoryPressure as error:
            self.candidate_archive.record(
                "controller_memory_pressure",
                candidate=_candidate(state.candidate),
                evidence=str(error.evidence),
            )
            try:
                self._mirror_guard_evidence()
            except OSError:
                pass
            raise
        return state

    def verify_candidate(self, work_id: str, *, expected_revision: int, deadline: float | None = None):
        if self.acceptance_timings is not None:
            return self.acceptance_timings.measure(
                "verify", self._verify_and_archive, self.workspace, work_id, expected_revision, deadline=deadline
            )
        return self._verify_and_archive(work_id, expected_revision=expected_revision, deadline=deadline)

    def _verify_and_archive(self, work_id: str, *, expected_revision: int, deadline: float | None):
        try:
            state = super().verify_candidate(work_id, expected_revision=expected_revision, deadline=deadline)
        except Exception as error:
            self.candidate_archive.record(
                "check_gap",
                work_id=work_id,
                error=safe_failure(error, trusted_files=self.candidate_archive.trusted, stage="public_check"),
            )
            raise
        self.candidate_archive.record(
            "checked",
            candidate=_candidate(state.candidate),
            status=state.verification.status,
            verification_artifact=str(state.verification.artifact),
        )
        return state

    def export_candidate(self, candidate, destination: Path):
        receipt = super().export_candidate(candidate, destination)
        self.candidate_archive.record(
            "exported",
            candidate=_candidate(candidate),
            export_receipt=str(receipt.artifact),
            export_receipt_sha256=receipt.artifact_sha256,
            snapshot=str(receipt.snapshot),
            internal_accepted=receipt.accepted,
        )
        return receipt


def make_evidence_runtime(
    workspace: Path,
    *,
    worktree_root: Path | None,
    archive_root: Path,
    deadline_unix: float | None,
    max_total_bytes: int,
    sequence_root: Path | None = None,
) -> WorkRuntime:
    protected = (workspace, worktree_root or default_worktree_root())
    archive = CandidateArchive(
        archive_root,
        deadline_unix=deadline_unix,
        max_total_bytes=max_total_bytes,
        protected=protected,
        sequence_root=sequence_root,
    )
    return EvidenceWorkRuntime(workspace, worktree_root=worktree_root, archive=archive)


def record_selection_link(output: Path, evidence_root: Path, outcome, *, harbor_trial_id: str) -> None:
    """Link the controller contract to the existing Harbor binding, never score it."""
    value = {
        "schema": "harbor-runtime/selection-link/v1",
        "harbor_trial_id": harbor_trial_id,
        "work_id": outcome.work_id,
        "candidate_available": outcome.candidate_available,
        "internal_failure": outcome.internal_failure,
        "grading_binding": "binding.json",
        "independent_reward": None,
        "selection": None,
    }
    if outcome.selection is not None:
        path = outcome.selection
        if (
            path.is_symlink()
            or not path.resolve(strict=True).is_relative_to(output.resolve())
            or path.stat().st_size > 8 * 1024 * 1024
        ):
            raise ValueError("selection_link_not_canonical")
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        # The private output tree leaves with the agent stage. Keep the exact
        # selection bytes on the host-collected evidence mount instead of naming
        # a path that the post-run scrub will remove.
        retained = evidence_root / "selection.json"
        if retained.exists() or retained.is_symlink():
            if (
                retained.is_symlink()
                or not retained.is_file()
                or hashlib.sha256(retained.read_bytes()).hexdigest() != digest
            ):
                raise ValueError("selection_link_evidence_copy_conflict")
        else:
            temporary = evidence_root / (".selection-" + uuid4().hex)
            try:
                with temporary.open("xb") as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    os.link(temporary, retained)
                except FileExistsError:
                    if hashlib.sha256(retained.read_bytes()).hexdigest() != digest:
                        raise ValueError("selection_link_evidence_copy_conflict") from None
            finally:
                temporary.unlink(missing_ok=True)
        value["selection"] = {"path": str(retained), "sha256": digest}
        report = json.loads(raw)
        export = report.get("export")
        if type(export) is dict:
            value["export"] = {
                key: export[key] for key in ("artifact", "artifact_sha256", "snapshot", "accepted") if key in export
            }
    with (evidence_root / "selection-link.json").open("x") as stream:
        json.dump(value, stream, sort_keys=True)
        stream.write("\n")
