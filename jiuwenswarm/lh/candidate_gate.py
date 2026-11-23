# Copyright (c) 2026 Chrys. All rights reserved.
"""Export a sealed candidate only with its exact supplied public-check receipt.

Passing public checks establishes export eligibility, never task acceptance or
an independent grade. Receipts survive explicit resume in Core advisory state.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast

from pact_core.artifacts import write_json_atomic
from pact_core.runtime import RuntimeStateError
from pact_core.runtime.types import CandidateRef, VerificationEvidence

from .capacity import DEFAULT_CAPACITY_SCALE, capacity_scale, scaled
from .check_outputs import read_check_outputs
from .checkpoint_guard import sha256_file
from .harbor_unified.global_sequence import GlobalSequence


class PublicCandidateGate:
    advisory_key = "long_horizon_public_candidates"

    def __init__(self, path: Path, *, sequence_root: Path | None = None) -> None:
        self.path = path
        # Gate files live under <output>/candidate-gates, so the default root is
        # the controller output that also owns the archive sequence.
        self.sequence_root = sequence_root if sequence_root is not None else path.parent.parent
        self.entries: list[tuple[CandidateRef, VerificationEvidence, str, str]] = []
        self.attempts: list[dict[str, Any]] = []
        self._sequence = self._read_sequence()
        self.write()

    def _read_sequence(self) -> int:
        """Recover the durable attempt sequence from all persisted gate files."""
        highest = 0
        try:
            paths = list(self.path.parent.glob("*.json"))
        except OSError:
            return 0
        for path in paths:
            try:
                if path.is_symlink() or not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
                    continue
                value = json.loads(path.read_bytes())
            except (OSError, ValueError):
                continue
            rows = value.get("attempts") if isinstance(value, dict) else None
            if not isinstance(rows, list):
                continue
            for row in rows:
                sequence = row.get("sequence") if isinstance(row, dict) else None
                if type(sequence) is int and sequence > highest:
                    highest = sequence
        return highest

    def observed(self, candidate: CandidateRef) -> bool:
        value = json.loads(json.dumps(asdict(candidate), default=str))
        return any(row["candidate"] == value for row in self.attempts)

    @staticmethod
    def _digest(path: Path) -> str:
        if path.is_symlink() or not path.is_file():
            raise RuntimeStateError("public verification artifact is missing or is a symlink")
        return sha256_file(path)

    def record(self, state: Any, stage: str) -> bool:
        candidate, verification = state.candidate, state.verification
        bound = verification is not None and verification.candidate == candidate
        passed = bound and verification.status == "passed"
        digest = self._digest(verification.artifact) if passed else None
        self._sequence += 1
        row = {
            "stage": stage,
            "sequence": self._sequence,
            "global_sequence": GlobalSequence(self.sequence_root).next(),
            "at": time.time(),
            "candidate": asdict(candidate),
            "status": verification.status if bound else "missing",
            "eligible": bool(passed),
            "verification": asdict(verification) if bound else None,
            "verification_sha256": digest,
            "checks": [
                {key: check.get(key) for key in ("name", "version", "status", "exit_code")}
                for check in (verification.check_results if bound else ())
            ],
        }
        self.attempts.append(json.loads(json.dumps(row, default=str)))
        if passed:
            self.entries.append((candidate, verification, stage, cast(str, digest)))
        self.write()
        return bool(passed)

    @property
    def selected(self) -> tuple[CandidateRef, VerificationEvidence, str, str] | None:
        return self.entries[-1] if self.entries else None

    def saved_entries(self) -> dict[str, Any]:
        payload = {
            "schema": "long-horizon/public-candidate-history/v1",
            "entries": [
                {
                    "candidate": asdict(candidate),
                    "verification": asdict(verification),
                    "stage": stage,
                    "verification_sha256": digest,
                }
                for candidate, verification, stage, digest in self.entries
            ],
        }

        return json.loads(json.dumps(payload, default=str))

    def restore(self, state: Any, workspace: Path) -> None:
        saved = state.advisory.get(self.advisory_key)
        if saved is None:
            return
        if not isinstance(saved, dict) or saved.get("schema") != "long-horizon/public-candidate-history/v1":
            raise RuntimeStateError("unsupported public candidate history")
        try:
            for entry in saved["entries"]:
                candidate = CandidateRef(**{**entry["candidate"], "artifact": Path(entry["candidate"]["artifact"])})
                raw = entry["verification"]
                verification_candidate = CandidateRef(
                    **{**raw["candidate"], "artifact": Path(raw["candidate"]["artifact"])}
                )
                verification = VerificationEvidence(
                    **{
                        **raw,
                        "candidate": verification_candidate,
                        "artifact": Path(raw["artifact"]),
                        "check_results": tuple(raw["check_results"]),
                    }
                )
                root = (workspace / ".pact").resolve()
                for artifact in (candidate.artifact, verification.artifact):
                    if not artifact.resolve(strict=True).is_relative_to(root):
                        raise RuntimeStateError("public candidate history escapes Core storage")
                if (
                    candidate.work_id != state.work_id
                    or verification.candidate != candidate
                    or verification.status != "passed"
                    or self._digest(verification.artifact) != entry["verification_sha256"]
                ):
                    raise RuntimeStateError("public candidate history or verification evidence changed")
                self.entries.append((candidate, verification, entry["stage"], entry["verification_sha256"]))
        except (KeyError, TypeError, ValueError, OSError) as exc:
            raise RuntimeStateError("invalid public candidate history") from exc
        self.write()

    def write(self) -> None:
        selected = self.selected
        write_json_atomic(
            self.path,
            {
                "schema": "long-horizon/public-candidate-gate/v1",
                "scope": "supplied public checks only; not independent task success",
                "sequence": self._sequence,
                "attempts": self.attempts,
                "selected_candidate": asdict(selected[0]) if selected else None,
                "selected_stage": selected[2] if selected else None,
                "status": "candidate_validated" if selected else "no_valid_candidate",
            },
        )

    def export(self, runtime: Any, destination: Path) -> Any:
        if self.selected is None:
            return None
        candidate, verification, _, digest = self.selected
        if (
            verification.candidate != candidate
            or verification.status != "passed"
            or self._digest(verification.artifact) != digest
        ):
            raise RuntimeStateError("public gate evidence lost its exact candidate binding")
        # Core independently revalidates canonical history, sealed bytes and source basis.
        return runtime.export_candidate(candidate, destination)

    def feedback(self, candidate: CandidateRef) -> str:
        for row in reversed(self.attempts):
            if row["candidate"].get("tree") != candidate.tree:
                continue
            verification = row.get("verification") or {}
            if capacity_scale() == DEFAULT_CAPACITY_SCALE:
                # The default product path is the historical tail-only
                # feedback; check-log access is an explicit study opt-in.
                return (
                    "Host public candidate check: "
                    + row["status"]
                    + "\n"
                    + str(verification.get("stdout_tail", ""))[-3000:]
                    + "\n"
                    + str(verification.get("stderr_tail", ""))[-3000:]
                )
            outputs = read_check_outputs(
                verification.get("artifact"),
                maximum=None,
            )
            # The wheel's tail is always kept: check logs can be unavailable
            # or stale, and a failure summary usually lives in that tail.
            stdout_tail = str(verification.get("stdout_tail", ""))[-scaled(3000):]
            stderr_tail = str(verification.get("stderr_tail", ""))[-scaled(3000):]
            complete = [part for part in (stdout_tail, stderr_tail) if part]
            if outputs.stdout:
                complete.append(outputs.stdout)
            if outputs.stderr:
                complete.append(outputs.stderr)
            references = "" if outputs.complete else (
                "\n[complete check logs: "
                + json.dumps(list(outputs.references), ensure_ascii=False)
                + "]"
            )
            return (
                "Host public candidate check: "
                + row["status"]
                + "\n"
                + "\n".join(complete)
                + references
            )
        return "No passed host public check exists for this candidate."
