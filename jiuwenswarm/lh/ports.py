"""Public control-flow ports. No coding-agent package is imported here.

Native configuration, models and sessions are opaque at this boundary. PACT
owns Work state and acceptance; plugins only perform admitted role calls and
produce source-bound clarification evidence.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, Protocol

from pact_core.adapters.base import AgentAdapter
from pact_core.runtime import RuntimeContractError


@dataclass(frozen=True, slots=True)
class Capabilities:
    isolated_worker: bool = False
    isolated_executable_reviewer: bool = False
    frozen_clarification: bool = False
    cancellable_calls: bool = False
    # Construction alias for older plugins; both names carry the same
    # review-isolation guarantee and are normalized to one another.
    read_only_reviewer: bool = False

    def __post_init__(self) -> None:
        if self.isolated_executable_reviewer or self.read_only_reviewer:
            object.__setattr__(self, "isolated_executable_reviewer", True)
            object.__setattr__(self, "read_only_reviewer", True)

    def require_long_horizon(self) -> None:
        if not all(
            (
                self.isolated_worker,
                self.isolated_executable_reviewer,
                self.frozen_clarification,
                self.cancellable_calls,
            )
        ):
            raise RuntimeContractError(
                "plugin must enforce isolated execution, isolated executable review, frozen clarification "
                "and cancellation"
            )


@dataclass(frozen=True, slots=True)
class PluginSpec:
    id: str
    input_key: str
    progress_key: str
    input_schema: str
    progress_schema: str
    result_schema: str
    execution_schema: str
    configuration_key: str
    execution_key: str
    suspension_directory: str
    request_namespace: str
    work_id_prefix: str
    brief_source: str

    @classmethod
    def for_agent(cls, name: str) -> PluginSpec:
        if not name or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for c in name):
            raise ValueError("plugin id must be a lowercase safe identifier")
        return cls(
            name,
            f"{name}_long_horizon_benchmark",
            f"{name}_long_horizon_progress",
            f"{name}-pact/long-horizon-benchmark-input/v1",
            f"{name}-pact/long-horizon-benchmark-progress/v1",
            f"{name}-pact/benchmark-result/v1",
            f"{name}-pact/execution-binding/v1",
            f"{name}_configuration",
            f"{name}_execution_configuration",
            f"{name}-budget-suspensions",
            f"{name}-long-horizon-benchmark",
            f"{name}-pact-",
            f"{name} long-horizon clarification of the original task against frozen S0",
        )


@dataclass(frozen=True, slots=True)
class ExecutionBinding:
    fingerprint: str
    summary: dict[str, object]
    model_context: Any = None
    native: Any = None


class RoleAdapter(AgentAdapter, Protocol):
    budget_failure: str | None
    configuration_failure: str | None

    async def cancel_current_turn(self) -> None: ...


class ScopedAbort(threading.Event):
    """Native call cancellation is local; an explicit parent stop still propagates."""

    def __init__(self, parent):
        super().__init__()
        self.parent = parent

    def is_set(self):
        return super().is_set() or self.parent.is_set()

    def wait(self, timeout=None):
        import time

        deadline = None if timeout is None else time.monotonic() + timeout
        while not self.is_set():
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return False
            super().wait(0.05 if remaining is None else min(0.05, remaining))
        return True


@dataclass(frozen=True, slots=True)
class ExternalQuestion:
    """Opaque, stable question identity supplied by a native driver."""

    identity: str
    text: str
    options: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ExternalAnswer:
    status: Literal["answer", "pending", "timeout"]
    text: str = ""


class ExternalQuestionChannel(Protocol):
    async def post(self, question: ExternalQuestion, *, cancelled: asyncio.Event) -> None: ...

    async def await_answer(self, identity: str, *, timeout: float,
                           cancelled: asyncio.Event) -> ExternalAnswer: ...


@dataclass
class RoleBoundary:
    """Normalize native cancellation to a status the pinned Core ledger accepts."""

    native: RoleAdapter

    @property
    def id(self) -> str:
        return self.native.id

    @property
    def budget_failure(self) -> str | None:
        return self.native.budget_failure

    @property
    def configuration_failure(self) -> str | None:
        return self.native.configuration_failure

    def probe(self) -> Any:
        return self.native.probe()

    def reviewer_process_roots(self) -> tuple[int, ...]:
        roots = getattr(self.native, "reviewer_process_roots", None)
        return tuple(roots()) if callable(roots) else ()

    @property
    def reviewer_processes_shared(self) -> bool:
        return bool(getattr(self.native, "reviewer_processes_shared", False))

    def run_turn(self, request: Any) -> Any:
        try:
            result = self.native.run_turn(request)
        except Exception as exc:
            from .transport_failure import partial_stream_failure

            result = partial_stream_failure(exc, request)
            if result is None:
                raise
        if result.status == "cancelled":
            return replace(
                result,
                status="output_missing",
                final_text="",
                stderr_tail=f"native_status=cancelled; {result.stderr_tail}",
            )
        return result

    async def cancel_current_turn(self) -> None:
        await self.native.cancel_current_turn()


class SnapshotRoot(Protocol):
    view_root: str


class Snapshot(Protocol):
    roots: tuple[SnapshotRoot, ...]


class ArtifactStore(Protocol):
    def save_requirement_input(self, *, revision: int, messages: tuple[str, ...]) -> None: ...
    def save_result(self, result: Any, *, requirement_messages: tuple[str, ...]) -> None: ...


class Admission(Protocol):
    admitted: list[str]
    refused: str | None


class Bundle(Protocol):
    document_sha256: str
    manifest_sha256: str
    status: str
    text: str

    def record(self, output_root: Path) -> dict[str, object]: ...


class ArtifactPort(Protocol):
    def configure_snapshot_resources(self, *, max_total_bytes: int) -> None: ...

    exhausted: type[Exception]
    stopped: type[Exception]

    def create_store(self, session_dir: Path) -> ArtifactStore: ...
    def stage_record(self, **kwargs: Any) -> tuple[dict[str, object], Snapshot]: ...
    def evidence_checkpoint(self, **kwargs: Any) -> dict[str, object]: ...
    def load_stage_snapshot(self, output: Path, record: dict[str, object]) -> Snapshot: ...
    def read_clarification_bundle(self, turn_root: Path, *, original_task: str) -> Bundle: ...
    def make_admission(self, budget: Any, **kwargs: Any) -> Admission: ...


@dataclass(frozen=True, slots=True)
class ClarificationContext:
    profile: Any
    snapshot: Snapshot
    session_id: str
    session_dir: Path
    admission: Admission
    deadline: float | None = None
    checkpoint: tuple[Path, str] | None = None


class Plugin(Protocol):
    spec: PluginSpec
    capabilities: Capabilities
    artifacts: ArtifactPort

    def configuration(self, profile: str) -> dict[str, object]: ...
    def resolve_execution(self, workspace: Path, args: Any) -> ExecutionBinding: ...
    def make_adapter(
        self,
        role: Literal["worker", "reviewer"],
        binding: ExecutionBinding,
        args: Any,
        *,
        loop: asyncio.AbstractEventLoop,
        invocation_id: str,
        abort: threading.Event,
    ) -> RoleAdapter: ...
    async def clarify(self, context: ClarificationContext, original_task: str) -> Any: ...

    def evidence_configuration(self) -> dict[str, str]: ...

    def set_memory_provider(self, provider: Any, *, identity: str) -> None: ...

    async def recall(self, context: ClarificationContext, original_task: str) -> str: ...

    async def investigate(self, context: ClarificationContext, prompt: str, *, stage: str) -> str: ...

    async def generate_pact_input(
        self, context: ClarificationContext, result: Any, original_task: str, *, hints: str
    ) -> dict[str, Any]: ...

    async def cancel(self) -> None: ...
