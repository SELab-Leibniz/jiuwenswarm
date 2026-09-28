# Copyright (c) 2026 Chrys. All rights reserved.
# Adapted from Chrys 3f78185e298800b3e3f735f932a7ee2abb7bc25f; all inference uses native Jiuwen.

"""Output-root evidence for the original-task long-horizon benchmark.

Everything here is host evidence beside Core's canonical Work State: stage
snapshots and checkpoints, the clarification bundle bound as an acceptance
brief, per-call admission of clarification side agents into the trial's
BenchBudget, and the atomically published ``selection.json``. None of it grants
acceptance, delivery or an external grade.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import uuid4

from pact_core.artifacts import read_json, write_json_atomic
from pact_core.benchmark.budget import BenchBudget
from pact_core.runtime import RuntimeContractError
from pact_core.runtime.export import ExportReceipt, verify_export
from pact_core.schemas import TurnRequest

from jiuwenswarm.long_horizon.clarification.compat import Workspace
from jiuwenswarm.long_horizon.clarification.artifacts import CLARIFICATION_PHASE_DIR
from jiuwenswarm.long_horizon.clarification.budget import (
    SideCallBudgetExhausted,
    SideCallStatus,
    SideCallTicket,
)
from jiuwenswarm.long_horizon.clarification.checkpoints import save_stage_checkpoint
from jiuwenswarm.long_horizon.clarification.snapshot import WorkspaceSnapshot, WorkspaceSnapshotter

RESULT_SCHEMA = "jiuwenswarm-pact/benchmark-result/v1"
SELECTION_NAME = "selection.json"
CLARIFIER_ADAPTER_ID = "jiuwenswarm-requirement-clarifier"
_CONTROL_NAMES = frozenset({".git", ".pact", ".pact-io"})


class ClarificationStopped(SideCallBudgetExhausted):
    """The invocation owner requested a stop; no further side call is admitted."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def validate_output_root(output: Path, *, protected: tuple[Path, ...]) -> Path:
    """Return an absolute output root that cannot overlap source or Core evidence."""
    path = output.expanduser().absolute()
    if path.exists() and (path.is_symlink() or not path.is_dir()):
        raise RuntimeContractError("output root must be a real directory")
    if any(part.casefold() in _CONTROL_NAMES for part in path.parts):
        raise RuntimeContractError("output root cannot use a control namespace")
    resolved = path.resolve()
    for root in protected:
        root = root.resolve()
        if resolved.is_relative_to(root) or root.is_relative_to(resolved):
            raise RuntimeContractError(f"output root overlaps protected source or evidence: {root}")
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def contained(root: Path, path: Path) -> str:
    """Return ``path`` relative to the output root, rejecting any escape."""
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise RuntimeContractError(f"evidence path escapes the output root: {path}")
    return resolved.relative_to(root).as_posix()


def export_patch_sha256(receipt: ExportReceipt) -> str:
    return sha256_file(receipt.patch)


def receipt_payload(receipt: ExportReceipt) -> dict[str, object]:
    """Encode a receipt in the exact shape upstream ``decode(ExportReceipt, ...)`` reads."""
    return json.loads(json.dumps(asdict(receipt), ensure_ascii=True, default=str))


def stage_record(
    *,
    output_root: Path,
    turn_root: Path,
    stage: str,
    receipt: ExportReceipt,
    metadata: dict[str, object],
) -> tuple[dict[str, object], WorkspaceSnapshot]:
    """Freeze one exported Core candidate as a snapshot plus a hash-sealed checkpoint.

    The snapshot is captured from the sealed export tree, never from a live
    worktree, so later stages read exactly the bytes Core recorded.
    """
    verify_export(receipt)
    snapshot = WorkspaceSnapshotter().capture(
        Workspace.from_cwd(str(receipt.snapshot)),
        turn_root / "snapshots" / f"{stage}-{uuid4().hex}",
        snapshot_id=f"{stage}-{receipt.candidate.tree[:16]}",
        include_git_history=False,
    )
    checkpoint = save_stage_checkpoint(
        turn_root,
        stage=stage,
        status="sealed",
        workspace=snapshot.clarification_workspace(),
        history={},
        metadata={
            **metadata,
            "candidate_checkpoint": receipt.candidate.checkpoint,
            "candidate_tree": receipt.candidate.tree,
            "export_receipt_sha256": receipt.artifact_sha256,
            "patch_sha256": export_patch_sha256(receipt),
        },
        snapshot=snapshot,
    )
    return (
        {
            "stage": stage,
            "candidate_checkpoint": receipt.candidate.checkpoint,
            "candidate_tree": receipt.candidate.tree,
            "export": contained(output_root, receipt.artifact),
            "export_sha256": receipt.artifact_sha256,
            "patch_sha256": export_patch_sha256(receipt),
            "patch_empty": receipt.patch.stat().st_size == 0,
            "snapshot": contained(output_root, Path(snapshot.artifact_root)),
            "snapshot_manifest_sha256": snapshot.manifest_hash,
            "checkpoint": contained(output_root, checkpoint),
            "checkpoint_sha256": sha256_file(checkpoint),
        },
        snapshot,
    )


def evidence_checkpoint(
    *, output_root: Path, turn_root: Path, stage: str, snapshot: WorkspaceSnapshot, metadata: dict[str, object]
) -> dict[str, object]:
    """Seal a stage that reuses an already frozen source snapshot."""
    checkpoint = save_stage_checkpoint(
        turn_root,
        stage=stage,
        status="sealed",
        workspace=snapshot.clarification_workspace(),
        history={},
        metadata=metadata,
        snapshot=snapshot,
    )
    return {
        "stage": stage,
        "snapshot_manifest_sha256": snapshot.manifest_hash,
        "checkpoint": contained(output_root, checkpoint),
        "checkpoint_sha256": sha256_file(checkpoint),
    }


def load_stage_snapshot(output_root: Path, record: dict[str, object]) -> WorkspaceSnapshot:
    """Reload a recorded stage snapshot only when its manifest hash still matches."""
    relative = record.get("snapshot")
    expected = record.get("snapshot_manifest_sha256")
    if not isinstance(relative, str) or not isinstance(expected, str):
        raise RuntimeContractError("stage record has no frozen snapshot")
    snapshot = WorkspaceSnapshotter.load(output_root / relative)
    if snapshot.manifest_hash != expected:
        raise RuntimeContractError("frozen stage snapshot changed")
    return snapshot


@dataclass(frozen=True, slots=True)
class ClarificationBundle:
    """The bound clarification deliverable, validated against the original task bytes."""

    document: Path
    manifest: Path
    text: str
    document_sha256: str
    manifest_sha256: str
    status: str
    empty_reason: str | None

    def record(self, output_root: Path) -> dict[str, object]:
        return {
            "document": contained(output_root, self.document),
            "document_sha256": self.document_sha256,
            "manifest": contained(output_root, self.manifest),
            "manifest_sha256": self.manifest_sha256,
            "status": self.status,
            "empty_reason": self.empty_reason,
        }


def read_clarification_bundle(turn_root: Path, *, original_task: str) -> ClarificationBundle:
    """Validate path, encoding, digest and verbatim task authority; never search for 'latest'."""
    deliverable = turn_root / CLARIFICATION_PHASE_DIR / "deliverable"
    document = deliverable / "clarified-requirement.md"
    manifest_path = deliverable / "manifest.json"
    for path in (document, manifest_path):
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(turn_root.resolve()):
            raise RuntimeContractError(f"clarification bundle file is missing or unsafe: {path.name}")
    raw = document.read_bytes()
    text = raw.decode("utf-8")
    if not text.strip():
        raise RuntimeContractError("clarified requirement is empty")
    manifest = read_json(manifest_path)
    if manifest.get("schema") != "jiuwenswarm/requirement-clarification/deliverable-manifest/v1":
        raise RuntimeContractError("unsupported clarification manifest")
    digest = sha256_bytes(raw)
    if manifest.get("clarified_requirement_sha256") != digest:
        raise RuntimeContractError("clarified requirement differs from its manifest digest")
    if original_task not in text:
        raise RuntimeContractError("clarified requirement does not retain the original task verbatim")
    status = manifest.get("status")
    empty_reason = manifest.get("empty_reason")
    return ClarificationBundle(
        document,
        manifest_path,
        text,
        digest,
        sha256_file(manifest_path),
        status if isinstance(status, str) else "unknown",
        empty_reason if isinstance(empty_reason, str) else None,
    )


class BenchSideCallAdmission:
    """Admit every clarification ``agent.run`` into the trial's original BenchBudget.

    The ledger entry is written before the call and finished after it, so an
    interrupted process still shows a started call. Calls use the reviewer role
    with a ``clarification:<stage>`` purpose: a budget category, not a PACT
    review. A refusal raises before any provider request is made.
    """

    def __init__(
        self,
        budget: BenchBudget,
        *,
        deadline: float,
        workdir: Path,
        adapter_id: str,
        stop_requested: Callable[[], bool],
    ) -> None:
        self._budget = budget
        self._deadline = deadline
        self._workdir = workdir
        self._adapter_id = adapter_id
        self._stop_requested = stop_requested
        self.admitted: list[str] = []
        self.refused: str | None = None

    def admit(self, stage: str) -> SideCallTicket:
        if self._stop_requested():
            self.refused = "invocation_stopped"
            raise ClarificationStopped("invocation stop requested before a clarification side call")
        request = TurnRequest(
            role="reviewer",
            prompt=stage,
            workdir=self._workdir,
            timeout_seconds=self._budget.config.turn_timeout_seconds,
            deadline=self._deadline,
        )
        call_id, timeout, stop = self._budget.start_call(
            request, adapter_id=self._adapter_id, purpose=f"clarification:{stage}", deadline=self._deadline
        )
        if call_id is None:
            self.refused = stop
            raise SideCallBudgetExhausted(f"clarification side call {stage} refused by the trial budget: {stop}")
        self.admitted.append(stage)
        return SideCallTicket(call_id, timeout)

    def finish(self, ticket: SideCallTicket, *, status: SideCallStatus, detail: str = "") -> None:
        self._budget.finish_call(ticket.token, status=status, detail=detail)


def publish_selection(output_root: Path, payload: dict[str, object]) -> Path:
    """Replace ``OUTPUT/selection.json`` atomically after validating its references."""
    if payload.get("schema") != RESULT_SCHEMA:
        raise RuntimeContractError("selection must use the benchmark result schema")
    export = payload.get("export")
    if export is not None:
        if not isinstance(export, dict) or not isinstance(export.get("artifact"), str):
            raise RuntimeContractError("selection export must be an encoded ExportReceipt")
        contained(output_root, Path(export["artifact"]))
    target = output_root / SELECTION_NAME
    write_json_atomic(target, json.loads(json.dumps(payload, ensure_ascii=True, default=str)))
    return target
