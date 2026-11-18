"""One budgeted fresh Worker call after a proven native stream interruption.

The native worker owns execution and session admission. A failed native session
is never resumed or replayed: recovery inspects the current checkout in a fresh
session, within the original turn and trial deadlines. Original receipts stay
in place, and the extra call is charged before it runs.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

from pact_core.artifacts import write_json_atomic
from pact_core.schemas import TurnRequest

POLICY = "jiuwen-native-stream-recovery/v1;max_calls_per_trial=1;fresh_session;original_deadlines"
RECOVERY_ADAPTER = "jiuwenswarm-transport-recovery/v1"
_STREAM_FAILURE = re.compile(
    r"^\[181001\] model call failed, reason: (?:"
    r"openAI API async stream error: ReadError(?:\b|$)|"
    r"LLM stream timeout: stream frame timeout: stage=(?:idle_chunk|first_chunk),)"
)


def recovery_configuration() -> str:
    return POLICY + ";source_sha256=" + hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def stream_failure(directory: Path, *, stage: str) -> dict[str, Any] | None:
    """Classify machine terminal evidence, never model prose or reward zero."""
    try:
        raw_terminal = (directory / "terminal-result.json").read_bytes()
        raw_metadata = (directory / "metadata.json").read_bytes()
        terminal, metadata = json.loads(raw_terminal), json.loads(raw_metadata)
        error = terminal.get("error") or {}
        if not (
            terminal.get("status") == "failed"
            and type(terminal.get("exit_code")) is int
            and terminal["exit_code"] == 1
            and error.get("code") == "RUNTIME_ERROR"
            and isinstance(error.get("message"), str)
            and _STREAM_FAILURE.match(error["message"])
            and metadata.get("stage") == stage
            and metadata.get("failure_code") == "NATIVE_FAILED"
            and metadata.get("timed_out") is False
            and metadata.get("signal") is None
            and type(metadata.get("actual_exit_code")) is int
            and metadata.get("actual_exit_code") == 1
            and not metadata.get("budget_stop_observed", False)
        ):
            return None
        return {
            "terminal_sha256": hashlib.sha256(raw_terminal).hexdigest(),
            "metadata_sha256": hashlib.sha256(raw_metadata).hexdigest(),
            "failure": error["message"],
            "failed_session_id": terminal.get("session_id"),
        }
    except (OSError, ValueError, AttributeError, TypeError):
        return None


class RecoveringJiuwenWorker:
    def __init__(self, native: Any) -> None:
        self.native = native
        self.budget = None
        self.receipts: list[Path] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self.native, name)

    def _identity(self) -> tuple[str, str, str]:
        return (
            self.native.source_commit,
            hashlib.sha256(Path(self.native.binary).read_bytes()).hexdigest(),
            hashlib.sha256(Path(self.native.config_template).read_bytes()).hexdigest(),
        )

    async def run(self, **kwargs: Any) -> Any:
        from jiuwenswarm.long_horizon.worker import WorkerExecutionError

        started = time.monotonic()
        started_unix = self.budget.clock() if self.budget is not None else time.time()
        identity = self._identity()
        try:
            return await self.native.run(**kwargs)
        except WorkerExecutionError as failure:
            # Startup, protocol, cancellation, OOM, hard timeout and budget
            # failures keep their native handling. Only implementation calls
            # admitted by this trial's ledger can use the recovery allowance.
            directory = Path(kwargs["output_dir"])
            evidence = stream_failure(directory, stage=kwargs["stage"])
            budget = self.budget
            if (
                failure.code != "NATIVE_FAILED"
                or evidence is None
                or kwargs["stage"] != "benchmark-worker"
                or kwargs.get("read_only", False)
                or kwargs.get("session_id") is not None
                or budget is None
                or self._identity() != identity
                or Path(failure.output_dir).resolve() != directory.resolve()
            ):
                raise
            ledger = budget.snapshot()
            if any(call["adapter_id"] == RECOVERY_ADAPTER for call in ledger["calls"]):
                raise
            if not any(
                call["role"] == "worker" and call["purpose"] == "implementation" and call["status"] == "started"
                for call in ledger["calls"]
            ):
                raise
            remaining = float(kwargs["timeout_seconds"]) - (time.monotonic() - started)
            deadline = min(float(ledger["deadline"]), started_unix + float(kwargs["timeout_seconds"]))
            request = TurnRequest(
                role="worker",
                prompt=kwargs["prompt"],
                workdir=kwargs["workspace"],
                timeout_seconds=max(0.001, remaining),
                deadline=deadline,
            )
            call_id, timeout, refused = budget.start_call(
                request, adapter_id=RECOVERY_ADAPTER, purpose="transport-recovery", deadline=deadline
            )
            receipt = {
                "schema": "long-horizon/native-transport-recovery/v1",
                "policy": POLICY,
                "configuration": recovery_configuration(),
                "work_id": budget.work_id,
                "trial_id": budget.config.trial_id,
                "parent": evidence,
                "parent_usage": getattr(getattr(failure, "result", None), "usage", None),
                "trial_deadline": ledger["deadline"],
                "turn_deadline": deadline,
                "call_id": call_id,
                "refused": refused,
                "status": "started" if call_id else "refused",
                "native_session_continuation": False,
            }
            receipt_path = directory / "transport-recovery.json"
            if call_id is None:
                write_json_atomic(receipt_path, receipt)
                self.receipts.append(receipt_path)
                raise
            recovery_dir = directory / "transport-recovery-1"
            prompt = kwargs["prompt"] + (
                "\n\n[TRANSPORT_RECOVERY]\nThe preceding native call ended with a proven model stream interruption. "
                "This is a fresh session in the same current candidate checkout, not a replay of P0 or a new trial. "
                "Inspect the existing changes and public check results before continuing. Preserve correct work; "
                "do not reset, overwrite or copy back the original source. Re-run relevant public checks before "
                "claiming readiness. The original requirements, constraints and deadline remain in force."
            )
            status, detail = "error", "recovery did not complete"
            try:
                write_json_atomic(receipt_path, receipt)
                self.receipts.append(receipt_path)
                result = await self.native.run(
                    **{**kwargs, "prompt": prompt, "output_dir": recovery_dir, "timeout_seconds": timeout}
                )
                status = "completed" if result.status == "completed" and result.exit_code == 0 else "error"
                detail = "fresh native session after stream interruption"
                receipt.update(status=status, recovery_session_id=result.session_id, usage=result.usage)
                return result
            except BaseException as exc:
                detail = type(exc).__name__
                receipt.update(status="error", error_type=type(exc).__name__)
                raise
            finally:
                budget.finish_call(call_id, status=status, detail=detail)
                receipt["native_receipts"] = {
                    name: hashlib.sha256((recovery_dir / name).read_bytes()).hexdigest()
                    for name in ("metadata.json", "terminal-result.json", "usage.json")
                    if (recovery_dir / name).is_file()
                }
                write_json_atomic(receipt_path, receipt)
