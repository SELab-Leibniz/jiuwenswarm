"""Pure payload and text assembly for Execution Kernel artifacts."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from .goal_tracker import sha256_text
from .schemas import ROLE_RESULT_SCHEMA, AdapterProbe, TurnResult


def round_key(
    loop_id: str, round_number: int, input_checkpoint: str, prompt: str
) -> str:
    """Diagnostic content hash for one round; never used for skip or resume."""
    prompt_sha = sha256_text(prompt)
    return sha256_text(f"{loop_id}:{round_number}:{input_checkpoint}:{prompt_sha}")


def synthesized_worker_summary(
    *,
    turn: TurnResult,
    checkpoint_commit: str,
    changed_paths: list[str],
) -> str:
    changed = "\n".join(f"- `{path}`" for path in changed_paths) or "- None"
    return f"""# Worker Summary (controller-synthesized)

The worker did not return a usable final output for this round
(turn status: `{turn.status}`, exit code: `{turn.exit_code}`).
The filesystem state was still captured as checkpoint `{checkpoint_commit}`.

## Changed Paths
{changed}

Verification evidence is missing. This checkpoint must be treated as
partial work; the loop cannot complete on this round.
"""


def synthesized_review(*, turn: TurnResult) -> str:
    return f"""# Review (controller-synthesized)

The reviewer did not return a usable final output for this round
(turn status: `{turn.status}`, exit code: `{turn.exit_code}`).
The checkpoint remains durable, but review evidence is incomplete and this
round must continue.
"""


def skipped_reviewer_review(*, turn: TurnResult, checkpoint_commit: str) -> str:
    return f"""# Review (controller-synthesized)

The reviewer was not invoked because the worker returned no usable output
(turn status: `{turn.status}`, exit code: `{turn.exit_code}`) and checkpoint
`{checkpoint_commit}` contains no changes for this round. Verification still
ran, but this round cannot complete and must continue.
"""


def feedback_text(review_text: str, reasons: list[str]) -> str:
    if not reasons:
        return review_text
    reason_lines = "\n".join(f"- {reason}" for reason in reasons)
    return f"{review_text.rstrip()}\n\n## Gate Reasons\n{reason_lines}\n"


def role_result_payload(
    *,
    role: str,
    round_number: int,
    probe: AdapterProbe,
    turn: TurnResult | None,
    prompt: str | None,
    round_key_value: str,
    checkpoint_commit: str,
    artifact_root: Path,
    skip_reason: str | None = None,
) -> dict[str, object]:
    invoked = turn is not None
    if invoked != (prompt is not None):
        raise ValueError("invoked role results require both turn and prompt")
    return {
        "schema": ROLE_RESULT_SCHEMA,
        "role": role,
        "round": round_number,
        "invoked": invoked,
        "skip_reason": skip_reason,
        "adapter_id": probe.adapter_id,
        "adapter_version": probe.version,
        "status": turn.status if turn is not None else None,
        "output_source": turn.output_source if turn is not None else None,
        "exit_code": turn.exit_code if turn is not None else None,
        "duration_seconds": turn.duration_seconds if turn is not None else None,
        "prompt_sha256": sha256_text(prompt) if prompt is not None else None,
        "round_key": round_key_value,
        "checkpoint": checkpoint_commit,
        "stdout_tail": turn.stdout_tail if turn is not None else None,
        "stderr_tail": turn.stderr_tail if turn is not None else None,
        "telemetry": asdict(turn.telemetry) if turn is not None and turn.telemetry else None,
        "session_id": turn.session_id if turn is not None else None,
        "native_store_ref": (
            artifact_ref(turn.native_store_path, artifact_root)
            if turn is not None
            else None
        ),
        "agent_events_ref": (
            artifact_ref(turn.events_path, artifact_root)
            if turn is not None
            else None
        ),
        "event_parse_status": turn.event_parse_status if turn is not None else None,
        "event_count": turn.event_count if turn is not None else None,
    }


def artifact_ref(path: Path | None, artifact_root: Path) -> str | None:
    if path is None:
        return None
    try:
        return path.relative_to(artifact_root).as_posix()
    except ValueError as exc:
        raise ValueError(f"adapter artifact is outside loop directory: {path}") from exc


def adapter_manifest_entry(probe: AdapterProbe) -> dict[str, object]:
    return {
        "id": probe.adapter_id,
        "available": probe.available,
        "cli_version": probe.version,
        "binary_name": probe.binary_name,
        "model": probe.model,
        "model_source": probe.model_source,
        "provider": probe.provider,
        "reasoning_variant": probe.reasoning_variant,
        "reasoning_variant_source": probe.reasoning_variant_source,
        "profile": probe.profile,
        "isolation_mode": probe.isolation_mode,
    }
