# Copyright (c) 2026 Chrys. All rights reserved.

# Ported from Chrys 6fcfb3321892735631b49f33236b1e2a988ded2d: src/chrys/service/requirement_clarification/artifacts.py; solver execution is JiuwenSwarm.
"""Owner-only private artifacts for requirement-clarification turns."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from .compat import atomic_write_owner_only_text, surrogate_safe_text
from .compat import ensure_owner_only_directory
from .types import ClarificationResult
from .verification import VERIFICATION_FILE, json_digest

REQUIREMENT_CLARIFICATION_ARTIFACT_DIR = "requirement_clarification"
ARTIFACT_VERSION = 1
INPUT_PHASE_DIR = "01-input"
INITIAL_TRIAL_PHASE_DIR = "02-initial-trial"
CLARIFICATION_PHASE_DIR = "03-clarification"
REPAIR_PHASE_DIR = "04-repair"
OUTCOME_PHASE_DIR = "05-outcome"
PACT_INPUT_PHASE_DIR = "06-pact-input"
_TURN_DIR = re.compile(r"turn_(\d+)\Z")
_WORKFLOW_RECORD_MAX_BYTES = 4 * 1024 * 1024
_PRIVATE_ARTIFACT_MAX_BYTES = 128 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class IncompleteWorkflowArtifacts:
    """Owner-scoped files needed to recover a completed P0 after a crash."""

    turn_number: int
    root: Path
    record: dict[str, object]


class ClarificationArtifactStore:
    """Persist private proposals separately from model-visible session history."""

    def __init__(
        self,
        session_dir: Path,
        turn_number: int,
        *,
        artifact_dir_name: str = REQUIREMENT_CLARIFICATION_ARTIFACT_DIR,
        artifact_subdir: str = "",
    ) -> None:
        self.root = session_dir / artifact_dir_name / f"turn_{turn_number}"
        if artifact_subdir:
            self.root /= artifact_subdir
        ensure_owner_only_directory(self.root)

    def _save_json(self, relative_path: Path, payload: dict[str, object]) -> None:
        path = self.root / relative_path
        ensure_owner_only_directory(path.parent)
        atomic_write_owner_only_text(
            path,
            json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        )

    def _save_text(self, relative_path: Path, payload: str) -> None:
        path = self.root / relative_path
        ensure_owner_only_directory(path.parent)
        atomic_write_owner_only_text(path, surrogate_safe_text(payload))

    def save_snapshot_metadata(self, payload: dict[str, object]) -> None:
        """Persist stable metadata for transient S0/P0 recovery snapshots."""
        self._save_json(
            Path(INPUT_PHASE_DIR) / "workspace-snapshot.json",
            {
                "schema": "jiuwenswarm/requirement-clarification/snapshot-metadata/v1",
                "artifact_version": ARTIFACT_VERSION,
                **payload,
            },
        )

    def save_budget_checkpoint(self, payload: dict[str, object]) -> None:
        """Preserve an incomplete live phase without promoting it as a success."""
        self._save_json(Path("budget-exhausted.json"), {"schema": "jiuwenswarm/workflow-budget/v1", **payload})

    def save_requirement_input(self, *, revision: int, messages: tuple[str, ...]) -> None:
        """Persist the verbatim user-authority input separately from derived guidance."""
        self._save_text(Path(INPUT_PHASE_DIR) / "requirement.md", _render_requirement_input(messages))
        self._save_json(
            Path(INPUT_PHASE_DIR) / "manifest.json",
            {
                "schema": "jiuwenswarm/requirement-clarification/input-manifest/v1",
                "artifact_version": ARTIFACT_VERSION,
                "revision": revision,
                "message_count": len(messages),
                "requirement": f"{INPUT_PHASE_DIR}/requirement.md",
            },
        )

    def save_initial_response(self, *, revision: int, response: str) -> None:
        """Persist the provisional P0 response separately from its transcript."""
        self._save_json(
            Path(INITIAL_TRIAL_PHASE_DIR) / "response.json",
            {
                "schema": "jiuwenswarm/requirement-clarification/phase-response/v1",
                "artifact_version": ARTIFACT_VERSION,
                "phase": "initial_trial",
                "revision": revision,
                "status": "provisional",
                "response": response,
            },
        )

    def save_result(self, result: ClarificationResult, *, requirement_messages: tuple[str, ...]) -> None:
        """Persist both the legacy aggregate and phase-oriented clarification outputs."""
        private = {
            "strategy_version": result.strategy_version,
            "revision": result.revision,
            "status": result.status,
            "empty_reason": result.empty_reason,
            "elapsed_seconds": result.elapsed_seconds,
            "delta": result.delta,
            "selection": result.selection.model_dump(mode="json"),
            "proposals": [proposal.model_dump(mode="json") for proposal in result.proposals],
            "investigations": [investigation.model_dump(mode="json") for investigation in result.investigations],
            "usage_details": list(result.usage_details),
            "warnings": list(result.warnings),
        }
        atomic_write_owner_only_text(
            self.root / "clarification.private.json",
            json.dumps(private, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        )
        proposals_dir = Path(CLARIFICATION_PHASE_DIR) / "candidates"
        # File each candidate under the proposer that produced it. Numbering by
        # position would put proposer 2's proposal in `proposal-1.json` beside
        # proposer 1's failed investigation, which is exactly the pairing the
        # audit trail exists to make.
        indices = result.proposal_sample_indices
        for position, proposal in enumerate(result.proposals):
            index = indices[position] if position < len(indices) else position + 1
            self._save_json(
                proposals_dir / f"proposal-{index}.private.json",
                {
                    "schema": "jiuwenswarm/requirement-clarification/proposal/v2",
                    "artifact_version": ARTIFACT_VERSION,
                    "strategy_version": result.strategy_version,
                    "revision": result.revision,
                    "sample_index": index,
                    "proposal": proposal.model_dump(mode="json"),
                },
            )
        investigations_dir = Path(CLARIFICATION_PHASE_DIR) / "investigations"
        for investigation in result.investigations:
            self._save_json(
                investigations_dir / f"proposal-{investigation.sample_index}.private.json",
                {
                    "schema": "jiuwenswarm/requirement-clarification/investigation/v2",
                    "artifact_version": ARTIFACT_VERSION,
                    "strategy_version": result.strategy_version,
                    "revision": result.revision,
                    "investigation": investigation.model_dump(mode="json"),
                },
            )
        raw_selection = (
            result.raw_selection.model_dump(mode="json") if result.raw_selection is not None else {"reviews": []}
        )
        self._save_json(
            Path(CLARIFICATION_PHASE_DIR) / "decision" / "selection.private.json",
            {
                "schema": "jiuwenswarm/requirement-clarification/selection/v2",
                "artifact_version": ARTIFACT_VERSION,
                "strategy_version": result.strategy_version,
                "revision": result.revision,
                "raw": raw_selection,
                "cleaned": result.selection.model_dump(mode="json"),
            },
        )
        self._save_text(
            Path(CLARIFICATION_PHASE_DIR) / "sources" / "delta.md",
            _ensure_trailing_newline(result.delta),
        )
        clarified_requirement = _render_clarified_requirement(
            requirement_messages,
            result.delta,
            status=result.status,
            empty_reason=result.empty_reason,
        )
        self._save_text(
            Path(CLARIFICATION_PHASE_DIR) / "deliverable" / "clarified-requirement.md",
            clarified_requirement,
        )
        self._save_json(
            Path(CLARIFICATION_PHASE_DIR) / "deliverable" / "manifest.json",
            {
                "schema": "jiuwenswarm/requirement-clarification/deliverable-manifest/v1",
                "artifact_version": ARTIFACT_VERSION,
                "strategy_version": result.strategy_version,
                "revision": result.revision,
                "elapsed_seconds": result.elapsed_seconds,
                "status": result.status,
                "empty_reason": result.empty_reason,
                "proposal_count": len(result.proposals),
                "completed_investigation_count": sum(
                    investigation.status == "completed" for investigation in result.investigations
                ),
                "failed_investigation_count": sum(
                    investigation.status == "failed" for investigation in result.investigations
                ),
                "selected_guidance_count": len(result.selection.guidance_points),
                "is_empty": result.is_empty,
                "warnings": list(result.warnings),
                "artifacts": {
                    "candidates": f"{CLARIFICATION_PHASE_DIR}/candidates/",
                    "investigations": f"{CLARIFICATION_PHASE_DIR}/investigations/",
                    "selection": f"{CLARIFICATION_PHASE_DIR}/decision/selection.private.json",
                    "delta": f"{CLARIFICATION_PHASE_DIR}/sources/delta.md",
                    "clarified_requirement": (f"{CLARIFICATION_PHASE_DIR}/deliverable/clarified-requirement.md"),
                },
                "clarified_requirement_sha256": _text_sha256(clarified_requirement),
            },
        )

    def save_initial_transcript(self, payload: dict[str, object]) -> None:
        atomic_write_owner_only_text(
            self.root / "initial_implementation.private.json",
            json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        )
        self._save_json(
            Path(INITIAL_TRIAL_PHASE_DIR) / "transcript.private.json",
            {
                "schema": "jiuwenswarm/requirement-clarification/transcript/v1",
                "artifact_version": ARTIFACT_VERSION,
                "phase": "initial_trial",
                **payload,
            },
        )

    def save_repair_attempt(
        self,
        *,
        revision: int,
        status: str,
        response: str,
        transcript: dict[str, object],
    ) -> None:
        """Persist one repair attempt without conflating it with the accepted final output."""
        attempt_dir = Path(REPAIR_PHASE_DIR) / "attempts" / f"revision-{revision}"
        self._save_json(
            attempt_dir / "response.json",
            {
                "schema": "jiuwenswarm/requirement-clarification/phase-response/v1",
                "artifact_version": ARTIFACT_VERSION,
                "phase": "repair",
                "revision": revision,
                "status": status,
                "response": response,
            },
        )
        self._save_json(
            attempt_dir / "transcript.private.json",
            {
                "schema": "jiuwenswarm/requirement-clarification/transcript/v1",
                "artifact_version": ARTIFACT_VERSION,
                "phase": "repair",
                "revision": revision,
                **transcript,
            },
        )

    @property
    def pact_input_dir(self) -> Path:
        """Where the accepted Goal Contract and Initial Plan land."""
        return self.root / PACT_INPUT_PHASE_DIR

    def save_pact_generation(self, result: ClarificationResult) -> None:
        """Persist optional PACT inputs without changing the repair artifact contract."""
        metadata: dict[str, object] = {
            "schema": "jiuwenswarm/requirement-clarification/pact-generation/v1",
            "artifact_version": ARTIFACT_VERSION,
            "strategy_version": result.strategy_version,
            "revision": result.revision,
            "usage_details": list(result.usage_details),
            "warnings": list(result.warnings),
        }
        metadata["clarification_status"] = result.status
        if result.pact_input is None and result.status == "degraded" and not result.pact_generation_error:
            metadata.update({"status": "skipped", "error": "clarification degraded before PACT generation"})
            self._save_json(Path(PACT_INPUT_PHASE_DIR) / "generation.private.json", metadata)
            return
        if result.pact_input is None:
            metadata.update(
                {
                    "status": "failed",
                    "error": result.pact_generation_error or "PACT input generation returned no result",
                }
            )
            self._save_json(Path(PACT_INPUT_PHASE_DIR) / "generation.private.json", metadata)
            return

        goal_payload = (
            json.dumps(
                result.pact_input.goal_contract.model_dump(mode="json", by_alias=True),
                ensure_ascii=True,
                indent=2,
            )
            + "\n"
        )
        plan_payload = (
            json.dumps(
                result.pact_input.initial_plan.model_dump(mode="json", by_alias=True),
                ensure_ascii=True,
                indent=2,
            )
            + "\n"
        )
        self._save_text(Path(PACT_INPUT_PHASE_DIR) / "goal-contract.json", goal_payload)
        self._save_text(Path(PACT_INPUT_PHASE_DIR) / "initial-plan.json", plan_payload)
        if result.pact_input.public_verification is not None:
            self._save_json(
                Path(PACT_INPUT_PHASE_DIR) / VERIFICATION_FILE,
                {
                    "schema": "jiuwenswarm/pact-bound-public-verification/v1",
                    "goal_contract_sha256": json_digest(json.loads(goal_payload)),
                    "initial_plan_sha256": json_digest(json.loads(plan_payload)),
                    "verification": result.pact_input.public_verification.model_dump(mode="json", by_alias=True),
                },
            )
        metadata.update(
            {
                "status": "generated",
                "goal_contract_sha256": _text_sha256(goal_payload),
                "initial_plan_sha256": _text_sha256(plan_payload),
            }
        )
        self._save_json(Path(PACT_INPUT_PHASE_DIR) / "generation.private.json", metadata)

    def save_history_checkpoint(self, payload: dict[str, object]) -> None:
        """Persist H0 privately for phase recovery without model exposure."""
        atomic_write_owner_only_text(
            self.root / "h0.private.json",
            json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        )

    def save_workflow_record(self, payload: dict[str, object]) -> None:
        """Atomically replace the durable phase/revision recovery record."""
        atomic_write_owner_only_text(
            self.root / "workflow.json",
            json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        )

    def save_summary(
        self,
        payload: dict[str, object],
        *,
        requirement_messages: tuple[str, ...],
        delta: str,
    ) -> None:
        """Persist the outcome plus immutable views of the requirement used by repair."""
        final_response = str(payload.get("final_response", ""))
        clarification_status = str(payload.get("clarification_status", "completed"))
        empty_reason = payload.get("clarification_empty_reason")
        normalized_reason = str(empty_reason) if empty_reason is not None else None
        clarified_requirement = _render_clarified_requirement(
            requirement_messages,
            delta,
            status=clarification_status,
            empty_reason=normalized_reason,
        )
        clarified_requirement_delta = _render_clarified_requirement_delta(
            requirement_messages,
            delta,
            status=clarification_status,
            empty_reason=normalized_reason,
        )
        self._save_text(
            Path(OUTCOME_PHASE_DIR) / "final-response.md",
            _ensure_trailing_newline(final_response),
        )
        self._save_text(
            Path(OUTCOME_PHASE_DIR) / "clarified-requirement.md",
            clarified_requirement,
        )
        self._save_text(
            Path(OUTCOME_PHASE_DIR) / "clarified-requirement-delta.md",
            clarified_requirement_delta,
        )
        summary = dict(payload)
        summary.pop("final_response", None)
        summary.update(
            {
                "final_response_path": f"{OUTCOME_PHASE_DIR}/final-response.md",
                "clarified_requirement_path": f"{OUTCOME_PHASE_DIR}/clarified-requirement.md",
                "clarified_requirement_delta_path": f"{OUTCOME_PHASE_DIR}/clarified-requirement-delta.md",
                "clarified_requirement_sha256": _text_sha256(clarified_requirement),
                "clarified_requirement_delta_sha256": _text_sha256(clarified_requirement_delta),
            }
        )
        atomic_write_owner_only_text(
            self.root / "summary.json",
            json.dumps(summary, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        )
        self._save_json(
            Path(OUTCOME_PHASE_DIR) / "summary.json",
            {
                "schema": "jiuwenswarm/requirement-clarification/final-summary/v1",
                "artifact_version": ARTIFACT_VERSION,
                **summary,
            },
        )


def _ensure_trailing_newline(value: str) -> str:
    return value + ("\n" if value and not value.endswith("\n") else "")


def _render_requirement_input(messages: tuple[str, ...]) -> str:
    original = messages[0] if messages else "[No requirement message was recorded.]"
    sections = ["# Requirement Input", "## Original Requirement", original]
    if len(messages) > 1:
        sections.extend(("## Amendments", *_render_amendments(messages[1:])))
    return "\n\n".join(sections) + "\n"


def _empty_delta_text(*, status: str, empty_reason: str | None) -> str:
    if status == "degraded":
        return "Clarification failed or degraded; the original requirement is retained unchanged."
    if empty_reason == "requirement_complete":
        return "No additional repository-specific clarification was needed after investigation."
    if empty_reason == "selector_rejected":
        return "No candidate clarification passed selection and confidence checks."
    return "No clarification delta was produced."


def _render_clarified_requirement(
    messages: tuple[str, ...],
    delta: str,
    *,
    status: str = "completed",
    empty_reason: str | None = None,
) -> str:
    original = messages[0] if messages else "[No requirement message was recorded.]"
    sections = ["# Clarified Requirement", "## Original Requirement", original]
    if len(messages) > 1:
        sections.extend(("## Amendments", *_render_amendments(messages[1:])))
    sections.extend(("## Clarification Delta", delta or _empty_delta_text(status=status, empty_reason=empty_reason)))
    return "\n\n".join(sections) + "\n"


def _render_clarified_requirement_delta(
    messages: tuple[str, ...],
    delta: str,
    *,
    status: str = "completed",
    empty_reason: str | None = None,
) -> str:
    original = messages[0] if messages else "[No requirement message was recorded.]"
    sections = [
        "# Clarified Requirement Delta",
        "## Original Requirement",
        original,
        "## Clarification Delta",
        delta or _empty_delta_text(status=status, empty_reason=empty_reason),
    ]
    return "\n\n".join(sections) + "\n"


def _render_amendments(amendments: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(f"### Amendment {index}\n\n{message}" for index, message in enumerate(amendments, start=1))


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="backslashreplace")).hexdigest()


def prune_workflow_artifacts_after_turn(
    session_dir: Path,
    target_turn: int,
    *,
    artifact_dir_name: str = REQUIREMENT_CLARIFICATION_ARTIFACT_DIR,
) -> None:
    """Remove workflow artifacts belonging to turns discarded by rollback."""
    root = session_dir / artifact_dir_name
    if not root.is_dir() or root.is_symlink():
        return
    for path in root.iterdir():
        match = _TURN_DIR.fullmatch(path.name)
        if match is None or int(match.group(1)) <= target_turn or path.is_symlink():
            continue
        if path.is_dir():
            shutil.rmtree(path)


def latest_incomplete_workflow(session_dir: Path) -> IncompleteWorkflowArtifacts | None:
    """Return the newest bounded non-terminal workflow record, if any."""
    root = session_dir / REQUIREMENT_CLARIFICATION_ARTIFACT_DIR
    if not root.is_dir() or root.is_symlink():
        return None
    candidates: list[tuple[int, Path]] = []
    for path in root.iterdir():
        match = _TURN_DIR.fullmatch(path.name)
        if match is not None and path.is_dir() and not path.is_symlink():
            candidates.append((int(match.group(1)), path))
    for turn_number, path in sorted(candidates, reverse=True):
        record_path = path / "workflow.json"
        if not record_path.is_file() or record_path.is_symlink():
            continue
        try:
            if record_path.stat().st_size > _WORKFLOW_RECORD_MAX_BYTES:
                continue
            value = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            if value.get("terminal") is False:
                return IncompleteWorkflowArtifacts(turn_number=turn_number, root=path, record=value)
            # A newer terminal workflow proves the session progressed beyond
            # any older abandoned record; never resurrect an earlier turn.
            return None
    return None


def load_private_json(path: Path) -> dict[str, object]:
    """Load one bounded, non-symlink owner artifact as a mapping."""
    if not path.is_file() or path.is_symlink() or path.stat().st_size > _PRIVATE_ARTIFACT_MAX_BYTES:
        raise OSError(f"unsafe or oversized workflow artifact: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"workflow artifact is not a mapping: {path}")
    return value


def mark_workflow_recovered(artifacts: IncompleteWorkflowArtifacts, *, detail: str, conflicted: bool) -> None:
    """Terminalize a crash record after recovery or conflict detection."""
    payload = dict(artifacts.record)
    payload.update(
        {
            "phase": "conflicted" if conflicted else "degraded",
            "terminal": True,
            "detail": detail,
            "recovered_after_crash": not conflicted,
        }
    )
    atomic_write_owner_only_text(
        artifacts.root / "workflow.json",
        json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
    )
