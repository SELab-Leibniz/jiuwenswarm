"""Portable evidence format for plugins without native snapshot services."""

from __future__ import annotations

import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from pact_core.artifacts import read_json, write_json_atomic
from pact_core.runtime import RuntimeContractError
from pact_core.runtime.export import verify_export
from pact_core.schemas import TurnRequest

from .evidence import contained, sha256_bytes
from .snapshot_resources import SnapshotResources


class ClarificationBudgetExhausted(RuntimeError):
    pass


class ClarificationStopped(ClarificationBudgetExhausted):
    pass


@dataclass(frozen=True)
class Ticket:
    token: str
    timeout_seconds: float


class Admission:
    def __init__(self, budget: Any, *, deadline: float, workdir: Path, adapter_id: str, stop_requested: Any) -> None:
        self.budget, self.deadline, self.workdir = budget, deadline, workdir
        self.adapter_id, self.stop_requested = adapter_id, stop_requested
        self.admitted: list[str] = []
        self.refused: str | None = None

    def admit(self, stage: str) -> Ticket:
        if self.stop_requested():
            self.refused = "invocation_stopped"
            raise ClarificationStopped("invocation stopped before clarification call")
        request = TurnRequest(
            role="reviewer",
            prompt=stage,
            workdir=self.workdir,
            timeout_seconds=self.budget.config.turn_timeout_seconds,
            deadline=self.deadline,
        )
        call_id, timeout, stop = self.budget.start_call(
            request, adapter_id=self.adapter_id, purpose=f"clarification:{stage}", deadline=self.deadline
        )
        if call_id is None:
            self.refused = stop
            raise ClarificationBudgetExhausted(f"clarification call refused: {stop}")
        self.admitted.append(stage)
        return Ticket(call_id, timeout)

    def finish(self, ticket: Ticket, *, status: str, detail: str = "") -> None:
        self.budget.finish_call(ticket.token, status=status, detail=detail)


@dataclass(frozen=True)
class Root:
    view_root: str


@dataclass(frozen=True)
class Snapshot:
    artifact_root: str
    manifest_hash: str
    roots: tuple[Root, ...]


@dataclass(frozen=True)
class Clarification:
    text: str
    status: str = "completed"
    diagnostics: dict[str, object] | None = None


class Store:
    def __init__(self, session_dir: Path) -> None:
        self.root = session_dir / "requirement_clarification" / "turn_1"

    def save_requirement_input(self, *, revision: int, messages: tuple[str, ...]) -> None:
        write_json_atomic(
            self.root / "input.json",
            {"schema": "long-horizon/requirement-input/v1", "revision": revision, "messages": list(messages)},
        )

    def save_result(self, result: Clarification, *, requirement_messages: tuple[str, ...]) -> None:
        directory = self.root / "deliverable"
        directory.mkdir(parents=True, exist_ok=True)
        text = "Original task:\n" + "\n".join(requirement_messages) + "\n\nAdvisory clarification:\n" + result.text
        document = directory / "clarified-requirement.md"
        document.write_text(text, encoding="utf-8")
        diagnostics = {}
        if result.diagnostics is not None:
            artifact = self.root / "private" / "three-proposals.json"
            write_json_atomic(artifact, result.diagnostics)
            diagnostics = {
                "diagnostics": "private/three-proposals.json",
                "diagnostics_sha256": sha256_bytes(artifact.read_bytes()),
            }
        write_json_atomic(
            directory / "manifest.json",
            {
                "schema": "long-horizon/clarification/v1",
                "clarified_requirement_sha256": sha256_bytes(document.read_bytes()),
                "status": result.status,
                **diagnostics,
            },
        )


@dataclass(frozen=True)
class Bundle:
    document: Path
    manifest: Path
    text: str
    document_sha256: str
    manifest_sha256: str
    status: str

    def record(self, output_root: Path) -> dict[str, object]:
        return {
            "document": contained(output_root, self.document),
            "manifest": contained(output_root, self.manifest),
            "document_sha256": self.document_sha256,
            "manifest_sha256": self.manifest_sha256,
            "status": self.status,
        }


class FileArtifacts:
    def __init__(self) -> None:
        self._snapshot_resources = SnapshotResources()

    def configure_snapshot_resources(self, *, max_total_bytes: int) -> None:
        self._snapshot_resources = SnapshotResources(max_total_bytes)

    exhausted = ClarificationBudgetExhausted
    stopped = ClarificationStopped

    def create_store(self, session_dir: Path) -> Store:
        return Store(session_dir)

    def make_admission(self, budget: Any, **kwargs: Any) -> Admission:
        return Admission(budget, **kwargs)

    def stage_record(
        self, *, output_root: Path, turn_root: Path, stage: str, receipt: Any, metadata: dict[str, object]
    ) -> tuple[dict[str, object], Snapshot]:
        payload = verify_export(receipt)
        directory = turn_root / "snapshots" / f"{stage}-{uuid4().hex}"
        view = directory / "view"
        view.mkdir(parents=True)
        files: dict[str, str] = {}
        total_bytes = 0
        file_modes: dict[str, int] = {}
        omitted = dict(payload.get("opaque_symlinks", {}))
        # Opaque external fixtures stay metadata. No symlink is followed into the view.
        for base, dirs, names in os.walk(receipt.snapshot, followlinks=False):
            dirs[:] = [name for name in dirs if not (Path(base) / name).is_symlink()]
            for name in names:
                source = Path(base) / name
                relative = source.relative_to(receipt.snapshot).as_posix()
                if source.is_symlink() or relative in omitted:
                    omitted.setdefault(relative, {"model_visible": False})
                    continue
                self._snapshot_resources.check_total(total_bytes + source.stat().st_size)
                target = view / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
                total_bytes += target.stat().st_size
                self._snapshot_resources.check_total(total_bytes)
                target.chmod(stat.S_IMODE(source.stat().st_mode))
                files[relative] = sha256_bytes(target.read_bytes())
                file_modes[relative] = stat.S_IMODE(target.stat().st_mode)
        manifest = directory / "manifest.json"
        write_json_atomic(
            manifest,
            {
                "schema": "long-horizon/snapshot/v2",
                "files": files,
                "file_modes": file_modes,
                "omitted_symlinks": omitted,
            },
        )
        snapshot = Snapshot(str(directory), sha256_bytes(manifest.read_bytes()), (Root(str(view)),))
        record = self.evidence_checkpoint(
            output_root=output_root, turn_root=turn_root, stage=stage, snapshot=snapshot, metadata=metadata
        )
        record.update(
            candidate_checkpoint=receipt.candidate.checkpoint,
            candidate_tree=receipt.candidate.tree,
            export=contained(output_root, receipt.artifact),
            export_sha256=receipt.artifact_sha256,
            patch_sha256=sha256_bytes(receipt.patch.read_bytes()),
            patch_empty=receipt.patch.stat().st_size == 0,
            snapshot=contained(output_root, directory),
            snapshot_resources=self._snapshot_resources.record(),
        )
        return record, snapshot

    def validate(self, snapshot: Snapshot) -> None:
        directory = Path(snapshot.artifact_root)
        manifest = directory / "manifest.json"
        if manifest.is_symlink() or sha256_bytes(manifest.read_bytes()) != snapshot.manifest_hash:
            raise RuntimeContractError("frozen snapshot manifest changed")
        payload = read_json(manifest)
        view = directory / "view"
        expected = payload["files"]
        actual = {}
        total_bytes = 0
        for path in view.rglob("*"):
            if path.is_file() and not path.is_symlink():
                total_bytes += path.stat().st_size
                self._snapshot_resources.check_total(total_bytes)
                actual[path.relative_to(view).as_posix()] = sha256_bytes(path.read_bytes())
        if actual != expected or any(p.is_symlink() for p in view.rglob("*")):
            raise RuntimeContractError("frozen snapshot content changed")
        if payload.get("schema") == "long-horizon/snapshot/v2":
            modes = {relative: stat.S_IMODE((view / relative).stat().st_mode) for relative in actual}
            if modes != payload.get("file_modes"):
                raise RuntimeContractError("frozen snapshot file modes changed")
        elif payload.get("schema") != "long-horizon/snapshot/v1":
            raise RuntimeContractError("unsupported frozen snapshot schema")

    def load_stage_snapshot(self, output: Path, record: dict[str, object]) -> Snapshot:
        directory = output / str(record["snapshot"])
        contained(output, directory)
        snapshot = Snapshot(str(directory), str(record["snapshot_manifest_sha256"]), (Root(str(directory / "view")),))
        self.validate(snapshot)
        return snapshot

    def evidence_checkpoint(
        self, *, output_root: Path, turn_root: Path, stage: str, snapshot: Snapshot, metadata: dict[str, object]
    ) -> dict[str, object]:
        self.validate(snapshot)
        checkpoint = turn_root / "checkpoints" / f"{stage}-{uuid4().hex}.json"
        write_json_atomic(
            checkpoint,
            {
                "schema": "long-horizon/checkpoint/v1",
                "stage": stage,
                "snapshot": contained(output_root, Path(snapshot.artifact_root)),
                "snapshot_manifest_sha256": snapshot.manifest_hash,
                "metadata": {**metadata, "snapshot_resources": self._snapshot_resources.record()},
            },
        )
        return {
            "stage": stage,
            "snapshot_manifest_sha256": snapshot.manifest_hash,
            "checkpoint": contained(output_root, checkpoint),
            "checkpoint_sha256": sha256_bytes(checkpoint.read_bytes()),
        }

    def read_clarification_bundle(self, turn_root: Path, *, original_task: str) -> Bundle:
        directory = turn_root / "deliverable"
        document, manifest = directory / "clarified-requirement.md", directory / "manifest.json"
        for path in (document, manifest):
            if path.is_symlink() or not path.is_file():
                raise RuntimeContractError("unsafe or missing clarification bundle")
            contained(turn_root, path)
        text = document.read_text(encoding="utf-8")
        payload = read_json(manifest)
        digest = sha256_bytes(document.read_bytes())
        if (
            payload.get("schema") != "long-horizon/clarification/v1"
            or payload.get("clarified_requirement_sha256") != digest
        ):
            raise RuntimeContractError("clarification manifest differs from its document")
        if original_task not in text:
            raise RuntimeContractError("clarification must retain the original task verbatim")
        return Bundle(document, manifest, text, digest, sha256_bytes(manifest.read_bytes()), str(payload["status"]))
