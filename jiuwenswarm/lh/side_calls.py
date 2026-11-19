# Copyright (c) 2026 Chrys. All rights reserved.

"""Admitted, cancellable read-only calls shared by native and portable plugins."""

from __future__ import annotations

import asyncio
import hashlib
import os
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from pact_core.runtime import RuntimeContractError
from pact_core.schemas import TurnRequest

from .ports import RoleBoundary, ScopedAbort


class SideCallFailure(RuntimeError):
    """A failed optional investigation, distinct from mutation or budget refusal."""


def view_identity(snapshot: Any) -> str:
    digest = hashlib.sha256()
    for index, root in enumerate(snapshot.roots):
        view = Path(root.view_root)
        for base, dirs, files in os.walk(view, followlinks=False):
            links = [name for name in dirs if (Path(base) / name).is_symlink()]
            dirs[:] = sorted(name for name in dirs if not (Path(base) / name).is_symlink())
            for name in sorted([*files, *links]):
                path = Path(base) / name
                relative = f"{index}:{path.relative_to(view).as_posix()}"
                digest.update(relative.encode() + b"\x00")
                if path.is_symlink():
                    digest.update(b"link\x00" + os.readlink(path).encode() + b"\x00")
                else:
                    digest.update(b"file\x00" + hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


class SideCalls:
    def __init__(self) -> None:
        self._side_adapters: list[Any] = []
        self._memory_provider: Any = None
        self._memory_identity = "none"

    def set_memory_provider(self, provider: Any, *, identity: str) -> None:
        if not identity.strip():
            raise RuntimeContractError("a memory provider requires a frozen corpus/configuration identity")
        self._memory_provider, self._memory_identity = provider, identity

    def evidence_configuration(self) -> dict[str, str]:
        from .localization import localization_configuration

        return {"memory": self._memory_identity, "localization": localization_configuration()}

    async def recall(self, context: Any, original_task: str) -> str:
        if self._memory_provider is None:
            return ""
        return await self._memory_provider(context, original_task)

    def _remember_execution(self, binding: Any, args: Any, context: dict[str, Any]) -> None:
        self._side_binding, self._side_args, self._side_context = binding, args, context

    async def investigate(self, context: Any, prompt: str, *, stage: str) -> str:
        # Fresh native adapters start their own turn counters at one. Give each
        # side call a separate namespace/output root so concurrent proposals
        # cannot overwrite another call's transport or worker artifacts.
        binding, args, execution = self._side_binding, self._side_args, dict(self._side_context)
        call_id = uuid4().hex
        call_root = Path(context.session_dir) / "side-calls" / call_id
        try:
            adapter = RoleBoundary(
                self.make_adapter(
                    "reviewer",
                    binding,
                    replace(args, output=call_root),
                    **{
                        **execution,
                        "invocation_id": context.session_id + "-" + call_id,
                        "abort": ScopedAbort(execution["abort"]),
                    },
                )
            )
        finally:
            self._remember_execution(binding, args, execution)
        if not adapter.probe().available:
            raise RuntimeContractError("read-only investigation adapter unavailable")
        before = await asyncio.to_thread(view_identity, context.snapshot)
        ticket = context.admission.admit(stage)
        self._side_adapters.append(adapter)
        status, detail = "error", ""
        try:
            response = await asyncio.to_thread(
                adapter.run_turn,
                TurnRequest(
                    role="reviewer",
                    review_mode="feedback",
                    workdir=Path(context.snapshot.roots[0].view_root),
                    timeout_seconds=ticket.timeout_seconds,
                    artifact_dir=call_root / "transport",
                    prompt=prompt + "\n\nRead-only investigation: use only this assigned repository view. "
                    "Do not modify files or Git metadata, install dependencies, follow paths into the primary "
                    "checkout, or use private grading artifacts. Return findings through the response only.",
                ),
            )
            if await asyncio.to_thread(view_identity, context.snapshot) != before:
                raise RuntimeContractError("read-only side call mutated its assigned repository view")
            if self._side_context["abort"].is_set():
                raise self.artifacts.stopped("invocation stopped during investigation")
            if response.status != "completed":
                raise SideCallFailure(f"{stage} ended with {response.status}")
            if not response.final_text.strip() or len(response.final_text) > 150_000:
                raise SideCallFailure("side call must return nonempty bounded evidence")
            status = "completed"
            return response.final_text
        except asyncio.CancelledError:
            detail = "cancelled"
            await adapter.cancel_current_turn()
            raise
        except BaseException as exc:
            detail = type(exc).__name__
            raise
        finally:
            context.admission.finish(ticket, status=status, detail=detail)
            self._side_adapters.remove(adapter)

    async def _cancel_side_calls(self) -> None:
        for adapter in tuple(self._side_adapters):
            await adapter.cancel_current_turn()
