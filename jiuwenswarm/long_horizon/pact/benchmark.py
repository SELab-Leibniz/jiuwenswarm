# Copyright (c) 2026 Chrys. All rights reserved.
# Adapted from Chrys 3f78185e298800b3e3f735f932a7ee2abb7bc25f; all inference uses native Jiuwen.

"""Trusted benchmark configuration and export presentation, without scheduling."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from pact_core.artifacts import read_json, write_json_atomic
from pact_core.benchmark.types import BenchConfig
from pact_core.kernel import default_worktree_root
from pact_core.runtime import RuntimeContractError, WorkSnapshot
from pact_core.runtime.export import ExportReceipt, output_directory

from jiuwenswarm.long_horizon.pact.configuration import DefaultRunOptions

if TYPE_CHECKING:
    from pact_core.benchmark.types import BenchResult
    from pact_core.runtime.export import LatestCandidateExport

_INPUT_SCHEMA = "jiuwenswarm-pact/benchmark-input/v1"
_RESULT_SCHEMA = "jiuwenswarm-pact/benchmark-result/v1"


@dataclass(frozen=True, slots=True)
class BenchmarkBinding:
    """Frozen host inputs kept separate from mutable benchmark progress."""

    config: BenchConfig
    submission_dir: Path
    acceptance_brief: str | None
    max_turns: int

    def payload(self) -> dict[str, object]:
        return {
            "schema": _INPUT_SCHEMA,
            "configuration": self.config.payload(),
            "submission_dir": str(self.submission_dir),
            "acceptance_brief": self.acceptance_brief,
            "max_turns": self.max_turns,
        }

    def preflight(self, workspace: Path, worktree_root: Path | None, invocation_id: str) -> None:
        output_directory(self.submission_dir / invocation_id, (workspace, worktree_root or default_worktree_root()))

    def validate_options(self, state: WorkSnapshot, options: DefaultRunOptions) -> None:
        options.validate_policy(state)
        for key, value in {
            "turn_timeout_seconds": options.turn_timeout_seconds,
            "review_interval": options.review_interval,
            "review_each_round": options.review_each_round,
        }.items():
            if value is not None and self.config.payload()[key] != value:
                raise RuntimeContractError(f"existing trial uses a different {key}")
        if options.max_rounds is not None and options.max_rounds != self.max_turns:
            raise RuntimeContractError("existing trial uses a different max_rounds")

    @classmethod
    def load(cls, path: Path, *, options: DefaultRunOptions, max_turns_default: int = 3) -> BenchmarkBinding:
        source = path.expanduser().resolve(strict=True)
        value = read_json(source)
        if value.pop("schema", None) != "pact-core/benchmark-config/v1":
            raise RuntimeContractError("unsupported benchmark configuration schema")
        submission = _submission_path(value.pop("submission_dir", None), source.parent)
        brief = _brief(value.pop("acceptance_brief", None))
        config = BenchConfig.parse(value)
        payload = config.payload()
        for key, item in {
            "turn_timeout_seconds": options.turn_timeout_seconds,
            "review_interval": options.review_interval,
            "review_each_round": options.review_each_round,
        }.items():
            if item is not None:
                payload[key] = item
        return cls(
            BenchConfig.parse(payload),
            submission,
            brief,
            _max_turns(options.max_rounds if options.max_rounds is not None else max_turns_default),
        )

    @classmethod
    def read(cls, state: WorkSnapshot) -> BenchmarkBinding:
        value = state.advisory.get("jiuwenswarm_benchmark_input")
        if (
            not isinstance(value, dict)
            or set(value) != {"schema", "configuration", "submission_dir", "acceptance_brief", "max_turns"}
            or value.get("schema") != _INPUT_SCHEMA
            or not isinstance(value.get("configuration"), dict)
        ):
            raise RuntimeContractError("unsupported or missing Chrys benchmark input")
        submission = value["submission_dir"]
        if not isinstance(submission, str) or not Path(submission).is_absolute():
            raise RuntimeContractError("persisted submission_dir must be absolute")
        return cls(
            BenchConfig.parse(value["configuration"]),
            Path(submission),
            _brief(value["acceptance_brief"]),
            _max_turns(value["max_turns"]),
        )


def _submission_path(value: object, root: Path) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeContractError("submission_dir must be a nonempty path")
    path = Path(value).expanduser()
    return (path if path.is_absolute() else root / path).absolute()


def _brief(value: object) -> str | None:
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise RuntimeContractError("acceptance_brief must be nonempty text")
    return value


def _max_turns(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise RuntimeContractError("max_rounds must be a positive integer")
    return value


@dataclass(frozen=True, slots=True)
class BenchmarkInvocation:
    """Observed budget and sealed export; neither grants acceptance authority."""

    task_id: str
    trial_id: str
    stop_reason: str
    budget: dict[str, object]
    receipt: ExportReceipt | None
    export_diagnostics: tuple[dict[str, object], ...]
    artifact: Path

    def summary_lines(self) -> list[str]:
        receipt = self.receipt
        return [
            "workflow: benchmark",
            f"task_id: {self.task_id}",
            f"trial_id: {self.trial_id}",
            f"role_calls: {self.budget.get('role_calls', 'unknown')}",
            f"trial_deadline: {self.budget.get('deadline', 'unknown')}",
            f"export_status: {'exported' if receipt else 'no_exportable_candidate'}",
            f"export_receipt: {receipt.artifact if receipt else 'none'}",
            f"export_checkpoint: {receipt.candidate.checkpoint if receipt else 'none'}",
            f"export_accepted: {str(receipt.accepted).lower() if receipt else 'false'}",
            "external_grade: not_run",
            f"benchmark_result: {self.artifact}",
        ]


def record_benchmark_result(
    binding: BenchmarkBinding,
    result: BenchResult,
    selection: LatestCandidateExport,
    *,
    invocation_id: str,
    configuration_failure: str | None = None,
    workspace: Path,
    worktree_root: Path | None,
) -> BenchmarkInvocation:
    """Write a new host report, referencing existing Core facts and receipts."""
    state = result.snapshot
    artifact = binding.submission_dir / f"{state.work_id}-{invocation_id}-result.json"
    if artifact.exists() or artifact.is_symlink():
        raise RuntimeContractError("benchmark result destination already exists")
    diagnostics = tuple(asdict(item) for item in selection.diagnostics)
    report = {
        "schema": _RESULT_SCHEMA,
        "invocation_id": invocation_id,
        "work_id": state.work_id,
        "revision": state.revision,
        "task_id": binding.config.task_id,
        "trial_id": binding.config.trial_id,
        "stop_reason": "configuration_drift" if configuration_failure else result.stop_reason,
        "configuration_failure": configuration_failure,
        "status": state.status,
        "internal_accepted": state.acceptance is not None,
        "delivered": state.delivery is not None,
        "candidate": asdict(state.candidate),
        "acceptance": asdict(state.acceptance) if state.acceptance else None,
        "delivery": asdict(state.delivery) if state.delivery else None,
        "public_verification": asdict(state.verification) if state.verification else None,
        "export": asdict(selection.receipt) if selection.receipt else None,
        "export_diagnostics": diagnostics,
        "budget": result.budget,
        "external_grade": {"status": "not_run", "reward": None},
    }
    output_directory(artifact, (workspace, worktree_root or default_worktree_root()))
    write_json_atomic(artifact, json.loads(json.dumps(report, ensure_ascii=True, default=str)))
    return BenchmarkInvocation(
        binding.config.task_id,
        binding.config.trial_id,
        "configuration_drift" if configuration_failure else result.stop_reason,
        result.budget,
        selection.receipt,
        diagnostics,
        artifact,
    )
