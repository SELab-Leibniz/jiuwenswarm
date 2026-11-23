# Copyright (c) 2026 Chrys. All rights reserved.

"""Explicit inspector pause/join/resume of one logical planning invocation."""

from __future__ import annotations

import asyncio
import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from .planning_checkpoint import PlanningCheckpointError


@dataclass
class PlanningCall:
    call_id: str
    stage: str
    key: str
    state: str = "registered"
    attempt: int = 0
    child: asyncio.Task | None = None
    pause_requested: bool = False
    resumed: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass(frozen=True)
class PlanningCallResult:
    value: object
    call_id: str


class PlanningCalls:
    """A paused call stays pending; only an explicit resume starts another child.

    A logical proposal may contain more than one native invocation. Cancelling its
    child coroutine joins the current native writer through the native worker's
    cancellation contract. No failed/cancelled native session ID is restored.
    A global cancellation never enters the local resume path.
    """

    def __init__(self, session_dir: Path, binding: str, deadline: float) -> None:
        self.binding, self.deadline = binding, deadline
        self.root = session_dir.absolute() / "planning-calls" / binding
        self.active: dict[str, PlanningCall] = {}
        self.history: dict[str, PlanningCall] = {}
        self._condition = asyncio.Condition()
        self.persistence_errors: list[str] = []

    def _publish(self, call: PlanningCall) -> None:
        for directory in [self.root.parent, self.root]:
            if directory.is_symlink():
                raise PlanningCheckpointError("planning call directory is a symlink")
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            if not stat.S_ISDIR(directory.stat().st_mode) or directory.stat().st_mode & 0o077:
                raise PlanningCheckpointError("planning call directory must be private")
        path = self.root / (call.call_id + ".json")
        if path.is_symlink():
            raise PlanningCheckpointError("planning call record is a symlink")
        row = {
            "schema": "long-horizon/planning-call/v1",
            "binding": self.binding,
            "call_id": call.call_id,
            "stage": call.stage,
            "input_key": call.key,
            "state": call.state,
            "attempt": call.attempt,
            "deadline_unix": self.deadline,
            "paused_is_valid_result": False,
            "native_session_restored": False,
        }
        temporary = self.root / (".call-" + uuid4().hex + ".tmp")
        try:
            with temporary.open("x") as handle:
                os.chmod(temporary, 0o600)
                json.dump(row, handle, sort_keys=True)
                handle.write("\n")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    async def _state(self, call, state):
        async with self._condition:
            call.state = state
            try:
                self._publish(call)
            except Exception as error:
                self.persistence_errors.append(type(error).__name__)
            self._condition.notify_all()

    async def run(self, stage, key, invoke, *, guard):
        if any(call.key == key for call in self.active.values()):
            raise PlanningCheckpointError("duplicate active planning input")
        call = PlanningCall(uuid4().hex, stage, key)
        self._publish(call)  # Validate private metadata storage before any native child starts.
        self.active[call.call_id] = self.history[call.call_id] = call
        try:
            while True:
                guard()
                call.attempt += 1
                call.child = asyncio.create_task(invoke())
                await self._state(call, "running")
                try:
                    result = await asyncio.shield(call.child)
                except asyncio.CancelledError:
                    call.child.cancel()
                    await asyncio.gather(call.child, return_exceptions=True)
                    owner = asyncio.current_task()
                    if owner.cancelling() or not call.pause_requested:
                        await self._state(call, "cancelled")
                        raise
                    await self._state(call, "paused_joined")
                    await call.resumed.wait()
                    call.resumed.clear()
                    call.pause_requested = False
                    # The same frozen input and original deadline are rechecked.
                    guard()
                    continue
                except BaseException:
                    await self._state(call, "failed")
                    raise
                await self._state(call, "returned_unvalidated")
                return PlanningCallResult(result, call.call_id)
        finally:
            if call.child is not None and not call.child.done():
                call.child.cancel()
                await asyncio.gather(call.child, return_exceptions=True)
            if call.state in {"running", "paused_joined", "registered"}:
                await self._state(call, "cancelled")
            async with self._condition:
                self.active.pop(call.call_id, None)
                self._condition.notify_all()

    def mark_validated(self, call_id):
        call = self.history[call_id]
        if call.state != "returned_unvalidated":
            raise PlanningCheckpointError("validated call is not a returned invocation")
        call.state = "validated_checkpoint_saved"
        try:
            self._publish(call)
        except Exception as error:
            self.persistence_errors.append(type(error).__name__)

    async def control(self, command):
        if not isinstance(command, dict):
            raise PlanningCheckpointError("planning call control must be an object")
        if (
            command.get("binding") != self.binding
            or command.get("deadline_unix") != self.deadline
            or not isinstance(command.get("call_id"), str)
            or not re.fullmatch(r"[0-9a-f]{32}", command["call_id"])
            or not isinstance(command.get("action"), str)
            or command["action"] not in ("pause", "resume")
        ):
            raise PlanningCheckpointError("planning call control identity mismatch")
        async with self._condition:
            call = self.active.get(command["call_id"])
            if call is None:
                raise PlanningCheckpointError("planning call is no longer active")
            if command["action"] == "pause":
                if call.state != "running" or call.child is None:
                    raise PlanningCheckpointError("only a running call can be paused")
                call.pause_requested = True
                call.child.cancel()
                await self._condition.wait_for(lambda: call.state == "paused_joined" or call.call_id not in self.active)
                return {
                    "state": call.state,
                    "writers_joined": call.state == "paused_joined",
                    "completed_before_pause": call.call_id not in self.active,
                }
            if call.state != "paused_joined" or not call.pause_requested:
                raise PlanningCheckpointError("resume requires an inspected joined pause")
            call.resumed.set()
            return {
                "state": "explicit_resume_submitted",
                "same_input_key": call.key,
                "deadline_unix": self.deadline,
                "extra_budget_seconds": 0,
            }
