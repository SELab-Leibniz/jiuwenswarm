"""A trial budget counts attempted role calls before invoking an adapter.

The ledger is separate from canonical Work State. It can therefore be committed
while Core holds its own operation lock, including during reviewer JSON repair.
Unix deadlines survive process boundaries; restarting never grants a new budget.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
import math
from pathlib import Path
import time
from typing import cast
import uuid

from ..adapters.base import AgentAdapter, AdapterProbe, TurnRequest, TurnResult
from ..artifacts import read_json, write_json_atomic
from .types import BenchConfig
from ..runtime.schemas import RuntimeContractError, RuntimeStateError

LEDGER_SCHEMA = "pact-core/benchmark-budget/v1"
_REVIEW_PURPOSES = frozenset({"diagnostic", "feedback", "acceptance"})


@dataclass(frozen=True, slots=True)
class BudgetWindow:
    started_at: float
    deadline: float
    execution_deadline: float
    reserve_seconds: float


class BenchBudget:
    def __init__(self, root: Path, config: BenchConfig, work_id: str, *, clock: Callable[[], float] = time.time) -> None:
        self.config = config
        self.work_id = work_id
        self.clock = clock
        self.trial_dir = root / config.trial_id
        self.trial_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.trial_dir / "budget.json"
        self.identity = {"work_id": work_id, "configuration": config.payload()}
        with self._lock("budget"):
            if self.path.exists():
                self._read()
            else:
                started = clock()
                write_json_atomic(self.path, {"schema": LEDGER_SCHEMA, "identity": self.identity,
                    "started_at": started, "deadline": started + config.budget_seconds,
                    "work_inputs": None, "calls": [], "windows": [],
                    "usage": {"status": "unknown", "tokens": None, "cost": None}})

    @contextmanager
    def _lock(self, name: str) -> Iterator[None]:
        try:
            import fcntl
        except ImportError as exc:
            raise RuntimeStateError("Benchmark trial locking requires POSIX") from exc
        with (self.trial_dir / f"{name}.lock").open("a+b") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeStateError("Benchmark trial is already owned by another caller") from exc
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    @contextmanager
    def execution(self) -> Iterator[None]:
        """Only one workflow may spend a trial budget at a time."""
        with self._lock("execution"):
            yield

    def _read(self) -> dict[str, object]:
        value = read_json(self.path)
        if set(value) != {"schema", "identity", "started_at", "deadline", "work_inputs", "calls", "windows", "usage"} or value.get("schema") != LEDGER_SCHEMA:
            raise RuntimeContractError("Unsupported benchmark budget schema")
        if value.get("identity") != self.identity:
            raise RuntimeContractError("Benchmark trial belongs to different inputs or budget settings")
        for key in ("started_at", "deadline"):
            number = value[key]
            if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number):
                raise RuntimeContractError("Invalid benchmark budget clock")
        for key in ("calls", "windows"):
            items = value[key]
            if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                raise RuntimeContractError(f"Invalid benchmark budget {key}")
        if value["deadline"] != cast(float, value["started_at"]) + self.config.budget_seconds:
            raise RuntimeContractError("Benchmark trial deadline does not match its original budget")
        if value["work_inputs"] is not None and not isinstance(value["work_inputs"], dict):
            raise RuntimeContractError("Invalid benchmark fixed inputs")
        if value["usage"] != {"status": "unknown", "tokens": None, "cost": None}:
            raise RuntimeContractError("Unsupported benchmark usage evidence")
        call_ids: set[str] = set()
        for call in cast(list[dict[str, object]], value["calls"]):
            required = {"id", "role", "adapter_id", "purpose", "started_at", "timeout_seconds", "status"}
            finished = {"finished_at", "detail", "observed_duration_seconds", "usage"}
            if set(call) != required | (set() if call.get("status") == "started" else finished):
                raise RuntimeContractError("Invalid benchmark call record")
            if call.get("role") not in ("worker", "reviewer") or call.get("status") not in (
                "started", "completed", "timeout", "output_missing", "spawn_failed", "error"):
                raise RuntimeContractError("Invalid benchmark call role or status")
            for key in ("id", "adapter_id", "purpose"):
                if not isinstance(call[key], str) or not call[key]:
                    raise RuntimeContractError("Invalid benchmark call identity")
            call_id = cast(str, call["id"])
            if call_id in call_ids:
                raise RuntimeContractError("Duplicate benchmark call identity")
            call_ids.add(call_id)
        for window in cast(list[dict[str, object]], value["windows"]):
            if set(window) != {"started_at", "deadline", "reserve_seconds"}:
                raise RuntimeContractError("Invalid benchmark window record")
        return value

    def bind_work_inputs(self, inputs: dict[str, object]) -> None:
        """The same trial cannot be resumed under a revised user contract."""
        with self._lock("budget"):
            value = self._read()
            existing = value["work_inputs"]
            if existing is not None and existing != inputs:
                raise RuntimeContractError("Benchmark trial requirement, policy or adapters changed")
            if existing is None:
                value["work_inputs"] = inputs
                write_json_atomic(self.path, value)

    def snapshot(self) -> dict[str, object]:
        with self._lock("budget"):
            value = self._read()
        calls = cast(list[dict[str, object]], value["calls"])
        return {**value, "role_calls": len(calls),
                "worker_calls": sum(call.get("role") == "worker" for call in calls),
                "reviewer_calls": sum(call.get("role") == "reviewer" for call in calls),
                "remaining_seconds": max(0.0, cast(float, value["deadline"]) - self.clock())}

    def window(self, seconds: float | None = None) -> BudgetWindow:
        if seconds is not None and (isinstance(seconds, bool) or not isinstance(seconds, (float, int))
                                    or not math.isfinite(seconds) or seconds <= 0):
            raise RuntimeContractError("window_seconds must be positive and finite")
        with self._lock("budget"):
            value = self._read()
            started = self.clock()
            deadline = min(cast(float, value["deadline"]), started + seconds) if seconds is not None else cast(float, value["deadline"])
            reserve = min(max(0.0, deadline - started) * 0.1, 300.0)
            window = BudgetWindow(started, deadline, deadline - reserve, reserve)
            cast(list[dict[str, object]], value["windows"]).append({
                "started_at": started, "deadline": deadline, "reserve_seconds": reserve})
            write_json_atomic(self.path, value)
            return window

    def purpose_deadline(self, window: BudgetWindow, purpose: str) -> float:
        """Share the final time reserve between useful review purposes.

        A one-call trial can only produce a candidate, so retaining time for a
        second model call would not buy a review opportunity. Larger trials keep
        the existing execution boundary, including after a review attempt; this
        avoids extending a window's Worker/check budget on resume.
        """
        if purpose in _REVIEW_PURPOSES or (self.config.max_role_calls == 1 and purpose == "implementation"):
            return window.deadline
        return window.execution_deadline

    def stop_reason(self, deadline: float, *, purpose: str | None = None) -> str | None:
        """Return a hard limit, or a purpose-specific scheduling reservation.

        Without a purpose, reservations do not imply that the trial must stop:
        the remaining call may still perform useful diagnosis or acceptance.
        """
        with self._lock("budget"):
            return self._stop_reason(self._read(), deadline, purpose=purpose)

    def _stop_reason(self, value: dict[str, object], deadline: float, *, purpose: str | None = None) -> str | None:
        if self.clock() >= cast(float, value["deadline"]):
            return "trial_budget_exhausted"
        if self.clock() >= deadline:
            return "window_budget_exhausted"
        calls = cast(list[dict[str, object]], value["calls"])
        limit = self.config.max_role_calls
        if limit is not None and len(calls) >= limit:
            return "role_budget_exhausted"
        if purpose is not None and limit is not None:
            # This is one shared opportunity per trial, derived from the durable
            # attempt ledger. An invalid/interrupted review does not keep useful
            # Worker work locked out or create another quota after restart.
            reserve = limit >= 2 and not any(call["purpose"] in _REVIEW_PURPOSES for call in calls)
            remaining = limit - len(calls)
            if reserve and remaining == 1 and purpose not in _REVIEW_PURPOSES:
                return "review_reserved"
            # Optional clarification must not consume the only candidate turn.
            # In particular, a two-call trial needs Worker + review, not analysis
            # + review with no implementation in between.
            if (purpose == "acceptance-analysis" and not any(call["purpose"] == "implementation" for call in calls)
                    and remaining <= 1 + int(reserve)):
                return "candidate_reserved"
        return None

    def start_call(self, request: TurnRequest, *, adapter_id: str, purpose: str, deadline: float) -> tuple[str | None, float, str | None]:
        with self._lock("budget"):
            value = self._read()
            stop = self._stop_reason(value, deadline, purpose=purpose)
            if stop is not None:
                return None, 0.0, stop
            timeout = min(request.timeout_seconds, self.config.turn_timeout_seconds,
                          deadline - self.clock(), cast(float, value["deadline"]) - self.clock())
            if timeout <= 0:
                return None, 0.0, "window_budget_exhausted"
            call_id = uuid.uuid4().hex
            cast(list[dict[str, object]], value["calls"]).append({
                "id": call_id, "role": request.role, "adapter_id": adapter_id, "purpose": purpose,
                "started_at": self.clock(), "timeout_seconds": timeout, "status": "started"})
            write_json_atomic(self.path, value)
            return call_id, timeout, None

    def finish_call(self, call_id: str, *, status: str, detail: str = "") -> None:
        with self._lock("budget"):
            value = self._read()
            call = next((item for item in cast(list[dict[str, object]], value["calls"]) if item.get("id") == call_id), None)
            if call is None or call.get("status") != "started":
                raise RuntimeStateError("Benchmark role completion does not match a started call")
            call.update({"status": status, "finished_at": self.clock(), "detail": detail})
            call["observed_duration_seconds"] = max(0.0, self.clock() - cast(float, call["started_at"]))
            call["usage"] = {"status": "unknown", "tokens": None, "cost": None}
            write_json_atomic(self.path, value)


class BudgetedAdapter:
    """Wrap every invocation, including Core-owned reviewer repair turns."""
    def __init__(self, adapter: AgentAdapter, budget: BenchBudget, *, purpose: str, deadline: float) -> None:
        self.adapter = adapter
        self.budget = budget
        self.purpose = purpose
        self.deadline = deadline
        self.id = adapter.id
        self.stopped: str | None = None
        self.results: list[TurnResult] = []

    def probe(self) -> AdapterProbe:
        return self.adapter.probe()

    def run_turn(self, request: TurnRequest) -> TurnResult:
        deadline = min(self.deadline, request.deadline) if request.deadline is not None else self.deadline
        call_id, timeout, stop = self.budget.start_call(request, adapter_id=self.id, purpose=self.purpose, deadline=deadline)
        if call_id is None:
            self.stopped = stop
            return TurnResult(status="timeout", final_text="", output_source="budget", exit_code=None,
                              stderr_tail=f"budget_exhausted: {stop}")
        try:
            result = self.adapter.run_turn(replace(request, timeout_seconds=timeout, deadline=deadline))
        except BaseException as exc:
            self.budget.finish_call(call_id, status="error", detail=f"{type(exc).__name__}: {exc}"[:2000])
            raise
        self.budget.finish_call(call_id, status=result.status)
        self.results.append(result)
        return result
