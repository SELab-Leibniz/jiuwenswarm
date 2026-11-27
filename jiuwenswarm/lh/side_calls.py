# Copyright (c) 2026 Chrys. All rights reserved.

"""Admitted, cancellable read-only calls shared by native and portable plugins."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import time
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from jiuwenswarm.pact_core.artifacts import write_json_atomic
from jiuwenswarm.pact_core.runtime import RuntimeContractError
from jiuwenswarm.pact_core.schemas import TurnRequest

from .capacity import scaled
from .ports import RoleBoundary, ScopedAbort
from .reviewer_execution import (
    CONTROLLER_METADATA_PREFIXES,
    PROTECTED_VIEW_SCHEMA,
    IsolatedExecutableReviewer,
    NonDegradableIntegrityError,
    record_side_call_recovery,
    recovered_view_identity,
    recovery_quiet_seconds,
    register_recovered_view,
    revalidate_recovered_view,
    reviewer_execution_guidance,
    reviewer_scratch_path,
    verify_recovered_views,
)
from .reviewer_privilege import ReviewerLaunchPolicy, ReviewerProtectionError


class SideCallFailure(RuntimeError):
    """A failed optional investigation, distinct from mutation or budget refusal."""


class SideCallRecovered(RuntimeError):
    """A recovered side-call failure that a caller must handle on its degraded path."""

    def __init__(self, recovery: dict[str, Any]) -> None:
        self.recovery = recovery
        super().__init__("side call recovered after a protected-view mutation")


class SideCallIntegrityError(RuntimeContractError, NonDegradableIntegrityError):
    """A frozen-view mutation must win over concurrent optional or budget failures.

    The error is also the evidence record: it names the changed paths, whether
    the content hash or Git state check failed, and exactly what recovery did.
    Callers may only degrade when ``recovered`` is true; every other case stays
    a hard controller failure.
    """

    def __init__(
        self,
        message: str,
        *,
        changed_paths: Any = (),
        branch: str = "",
        checks: dict[str, bool] | None = None,
        recovery: dict[str, Any] | None = None,
        recovered: bool | None = None,
        protected_roots: Any = (),
        before_identity: str | None = None,
        after_identity: str | None = None,
        processes_terminated: bool = True,
        changed_path_count: int | None = None,
        changed_paths_truncated: bool = False,
    ) -> None:
        paths = tuple(str(path) for path in changed_paths)
        self.changed_paths = paths
        self.changed_path_count = changed_path_count if changed_path_count is not None else len(paths)
        self.changed_paths_truncated = bool(changed_paths_truncated)
        self.branch = branch
        self.checks = dict(checks or {})
        self.recovery = dict(recovery or {})
        self.recovered = bool(recovered)
        self.recovered_explicit = recovered is not None
        # Set by the side-call wrapper/caller.  The latch record starts as a
        # failed #46 integrity record and only becomes "recovered" after
        # recover_side_call_integrity has admitted and counted this recovery.
        self.latch_record: dict[str, Any] | None = None
        self.evidence_path: Path | None = None
        self.protected_roots = tuple(str(path) for path in protected_roots)
        self.before_identity = before_identity
        self.after_identity = after_identity
        self.processes_terminated = bool(processes_terminated)
        detail = message
        if paths:
            detail += "; changed_paths=" + ", ".join(paths[:20])
            if self.changed_path_count > len(paths):
                detail += f" (+{self.changed_path_count - len(paths)} more)"
        if branch:
            detail += "; branch=" + branch
        if self.recovery:
            detail += "; recovery=" + str(self.recovery.get("result", "unknown"))
        super().__init__(detail)

    @property
    def recovery_verified(self) -> bool:
        return bool(self.recovered and self.recovery.get("verified"))


def _controller_metadata(relative: str) -> bool:
    """True for controller-owned runtime metadata under a protected view.

    ``CONTROLLER_METADATA_PREFIXES`` is the single source of truth for these
    paths. They are written by the controller while a Work is running (budget
    ledgers, admission state, suspension latches), so they are outside the
    source view whose integrity a recovered guard protects. Source files and
    ``.git`` remain fully in scope.
    """
    first = relative.split("/", 1)[0]
    return first in CONTROLLER_METADATA_PREFIXES


def view_identity(snapshot: Any, *, ignore_git_index: bool = False) -> str:
    digest = hashlib.sha256()
    for index, root in enumerate(snapshot.roots):
        view = Path(root.view_root)
        for base, dirs, files in os.walk(view, followlinks=False):
            links = [name for name in dirs if (Path(base) / name).is_symlink()]
            dirs[:] = sorted(
                name
                for name in dirs
                if not (Path(base) / name).is_symlink()
                and not _controller_metadata((Path(base) / name).relative_to(view).as_posix())
            )
            for name in sorted([*files, *links]):
                path = Path(base) / name
                rel = path.relative_to(view).as_posix()
                # Git refreshes its stat cache even for read-only status
                # queries. The logical HEAD/refs/worktree state is checked by
                # the protected-view scope; raw index bytes are not content.
                # Lock files and every other .git entry remain in scope.
                if ignore_git_index and rel == ".git/index":
                    continue
                # Controller-owned metadata is not part of the protected source
                # view. Writing it must never invalidate the guard it records.
                if _controller_metadata(rel):
                    continue
                relative = f"{index}:{rel}"
                digest.update(relative.encode() + b"\x00")
                if path.is_symlink():
                    digest.update(b"link\x00" + os.readlink(path).encode() + b"\x00")
                else:
                    digest.update(b"file\x00" + hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


MAX_SIDE_CALL_INTEGRITY_RECOVERIES = 3


def _accept_latch_recovery(exc: SideCallIntegrityError, recovery: dict[str, Any]) -> None:
    """Mark the latched record only after the caller admitted and counted it."""
    latch = getattr(exc, "latch_record", None)
    if isinstance(latch, dict):
        latch["recovered"] = True
        latch["recovery_action"] = recovery.get("action") or latch.get("recovery_action")
        latch["recovery_attempt"] = recovery.get("attempt")
        nested = latch.get("recovery")
        if isinstance(nested, dict):
            nested["accepted"] = True
            nested["attempt"] = recovery.get("attempt")
    evidence = getattr(exc, "evidence_path", None)
    if evidence is None:
        return
    try:
        payload = json.loads(Path(evidence).read_text(encoding="utf-8"))
        payload["recovered"] = True
        payload["recovery_attempt"] = recovery.get("attempt")
        payload["recovery_max_attempts"] = recovery.get("max_attempts")
        nested = payload.get("recovery")
        payload["recovery"] = {
            **(nested if isinstance(nested, dict) else {}),
            "accepted": True,
            "recovered": True,
            "result": "recovered",
        }
        write_json_atomic(Path(evidence), payload)
    except (OSError, TypeError, ValueError):
        # The in-memory latch is authoritative for this run; a missing or
        # malformed evidence copy must not turn an admitted recovery into a
        # hard failure.
        pass


def _recovery_owners(controller: Any = None, plugin: Any = None) -> list[Any]:
    owners: list[Any] = []
    for owner in (plugin, getattr(controller, "_plugin", None) if controller is not None else None, controller):
        if owner is not None and owner not in owners:
            owners.append(owner)
    return owners


def recover_side_call_integrity(
    exc: SideCallIntegrityError,
    *,
    stage: str,
    controller: Any = None,
    plugin: Any = None,
) -> dict[str, Any] | None:
    """Shared bounded-recovery decision for every ``SideCalls.investigate`` caller.

    The wrapper already stopped the turn, rebuilt the protected view and ran
    the quiet-period recheck.  Here we verify that proof, enforce the per-run
    cap, and return evidence only when a caller may take its own degraded path.
    """
    recovery = dict(getattr(exc, "recovery", {}) or {})
    if getattr(exc, "recovered_explicit", False):
        recovered = bool(getattr(exc, "recovered", False))
    else:
        recovered = bool(
            getattr(exc, "recovered", False)
            or recovery.get("result") == "recovered"
            or recovery.get("verified") is True
        )
    if not recovered:
        return None
    if not getattr(exc, "processes_terminated", False):
        return None
    if recovery.get("verified") is False:
        return None
    if recovery.get("quiet_recheck_changed") is True:
        return None
    before = getattr(exc, "before_identity", None)
    after = getattr(exc, "after_identity", None)
    if before is not None and after is not None and before != after:
        return None
    owners = _recovery_owners(controller, plugin)
    used = max(
        (
            value
            for owner in owners
            if isinstance((value := getattr(owner, "_side_call_integrity_recoveries", 0)), int)
        ),
        default=0,
    )
    if used >= MAX_SIDE_CALL_INTEGRITY_RECOVERIES:
        return None
    active_plugin = plugin or (getattr(controller, "_plugin", None) if controller is not None else None)
    cancel = getattr(active_plugin, "_cancel_side_calls", None)
    run_async = getattr(controller, "_run_async", None) if controller is not None else None
    if callable(cancel) and callable(run_async):
        try:
            run_async(cancel())
        except Exception:
            return None
    for owner in owners:
        setattr(owner, "_side_call_integrity_recoveries", used + 1)
    if any(getattr(owner, "_side_call_integrity_state_path", None) is not None for owner in owners):
        record_side_call_recovery(used + 1)
    # The wrapper deliberately leaves the #46 latch record unrecovered.  Only
    # this admitted, counted side-call path may promote it, so an ordinary
    # acceptance Reviewer and a cap-rejected side call keep the primary
    # reviewer_mutation/reviewer_restore_failed failure.
    _accept_latch_recovery(exc, {
        "action": "discard_advisory;terminate_side_call;rebuild_protected_view;reverify",
        "attempt": used + 1,
        "max_attempts": MAX_SIDE_CALL_INTEGRITY_RECOVERIES,
    })
    return {
        "schema": "long-horizon/side-call-integrity-recovery/v1",
        "stage": stage,
        "attempt": used + 1,
        "max_attempts": MAX_SIDE_CALL_INTEGRITY_RECOVERIES,
        "action": "discard_advisory;terminate_side_call;rebuild_protected_view;reverify",
        "outcome": "recovered",
        "changed_paths": list(getattr(exc, "changed_paths", ())),
        "changed_path_count": getattr(exc, "changed_path_count", len(getattr(exc, "changed_paths", ()))),
        "changed_paths_truncated": bool(getattr(exc, "changed_paths_truncated", False)),
        "branch": getattr(exc, "branch", ""),
        "checks": dict(getattr(exc, "checks", {}) or {}),
        "processes_terminated": True,
        "recovery": recovery,
    }


class SideCalls:
    def __init__(self) -> None:
        self._side_adapters: list[Any] = []
        self._side_integrity_adapters: list[Any] = []
        self._memory_provider: Any = None
        self._memory_identity = "none"

    def set_memory_provider(self, provider: Any, *, identity: str) -> None:
        if not identity.strip():
            raise RuntimeContractError("a memory provider requires a frozen corpus/configuration identity")
        self._memory_provider, self._memory_identity = provider, identity

    def evidence_configuration(self) -> dict[str, str]:
        from .capacity import capacity_receipt
        from .localization import localization_configuration

        receipt = capacity_receipt()
        return {
            "memory": self._memory_identity,
            "localization": localization_configuration(),
            "capacity_scale": str(receipt["scale"]),
            "capacity_setting": str(receipt["setting"]),
            "capacity_schema": str(receipt["schema"]),
        }

    async def recall(self, context: Any, original_task: str) -> str:
        if self._memory_provider is None:
            return ""
        return await self._memory_provider(context, original_task)

    def _remember_execution(self, binding: Any, args: Any, context: dict[str, Any]) -> None:
        self._side_binding, self._side_args, self._side_context = binding, args, context

    def recover_integrity(self, exc: SideCallIntegrityError, *, stage: str) -> dict[str, Any]:
        recovery = recover_side_call_integrity(exc, stage=stage, plugin=self)
        if recovery is None:
            raise exc
        return recovery

    async def investigate(self, context: Any, prompt: str, *, stage: str) -> str:
        # Fresh native adapters start their own turn counters at one. Give each
        # side call a separate namespace/output root so concurrent proposals
        # cannot overwrite another call's transport or worker artifacts.
        binding, args, execution = self._side_binding, self._side_args, dict(self._side_context)
        call_id = getattr(context, "side_call_id", None) or uuid4().hex
        policy = ReviewerLaunchPolicy.from_args(args)
        call_root = (policy.root if policy is not None else Path(context.session_dir)) / "side-calls" / call_id
        views = tuple(Path(root.view_root).expanduser().resolve() for root in context.snapshot.roots)
        resolved_call = call_root.resolve()
        if any(resolved_call == view or resolved_call.is_relative_to(view) for view in views):
            raise ReviewerProtectionError("side-call output overlaps a protected view")
        if policy is not None and callable(getattr(policy, "protect_views", None)):
            policy.protect_views(*views)
        execution_root = (
            call_root / "review-execution"
            if policy is not None
            else Path(context.session_dir) / "side-review-execution"
        )
        scratch_candidate = reviewer_scratch_path(execution_root, call_id)
        protected_roots = views
        try:
            adapter = RoleBoundary(
                self.make_adapter(
                    "reviewer",
                    binding,
                    replace(args, output=call_root, reviewer_execution="read_only"),
                    **{
                        **execution,
                        "invocation_id": context.session_id + "-" + call_id,
                        "abort": ScopedAbort(execution["abort"]),
                    },
                )
            )
            # Side calls execute in a disposable copy, so they may build and
            # write there.  The wrapper still compares the real assigned view
            # and protected roots; only those changes invalidate the result.
            optional: dict[str, Any] = {}
            try:
                parameters = inspect.signature(IsolatedExecutableReviewer).parameters
            except (TypeError, ValueError):
                parameters = {}
            accepts_kwargs = any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in parameters.values()
            )
            checkpoint = getattr(context, "checkpoint", None)
            if checkpoint is not None and (accepts_kwargs or "checkpoint" in parameters):
                optional["checkpoint"] = checkpoint
            if accepts_kwargs or "scratch_namespace" in parameters:
                optional["scratch_namespace"] = call_id
            adapter = IsolatedExecutableReviewer(
                adapter,
                root=execution_root,
                protected_roots=protected_roots,
                launch_policy=policy,
                **optional,
            )
            # Keep every integrity-bearing wrapper for the plugin invocation.
            # Completed calls leave the active cancellation set but their
            # recorded mutation evidence must still be latchable afterwards.
            self._side_integrity_adapters.append(adapter)
        finally:
            self._remember_execution(binding, args, execution)
        if not adapter.probe().available:
            raise RuntimeContractError("read-only investigation adapter unavailable")
        if execution["abort"].is_set():
            raise self.artifacts.stopped("invocation stopped before investigation admission")
        verify_recovered_views(protected_roots)
        guard_before = await asyncio.to_thread(recovered_view_identity, protected_roots)
        before = await asyncio.to_thread(view_identity, context.snapshot, ignore_git_index=True)
        ticket = context.admission.admit(stage)
        self._side_adapters.append(adapter)
        status, detail = "error", ""
        investigation_prompt = (
            prompt
            + "\n\nRead-only investigation: use only this assigned repository view. "
            "Do not modify files or Git metadata, install dependencies, follow paths into the primary "
            "checkout, or use private grading artifacts. Return findings through the response only."
            + reviewer_execution_guidance(scratch_candidate)
        )
        try:
            turn = asyncio.create_task(asyncio.to_thread(
                adapter.run_turn,
                TurnRequest(
                    role="reviewer",
                    review_mode="feedback",
                    workdir=Path(context.snapshot.roots[0].view_root),
                    timeout_seconds=ticket.timeout_seconds,
                    artifact_dir=call_root / "transport",
                    prompt=investigation_prompt,
                ),
            ))
            response = await asyncio.shield(turn)
            violation = getattr(adapter, "last_violation", None)
            if violation is not None:
                error = self._integrity_error(
                    context,
                    call_root,
                    stage,
                    violation,
                    before=before,
                    guard_before=guard_before,
                    protected_roots=protected_roots,
                    processes_terminated=bool(getattr(adapter, "processes_terminated", True)),
                )
                # The wrapper owns the #46 latch record.  Keep a reference so
                # recover_side_call_integrity can promote exactly this record
                # after admission and counting.
                if adapter.integrity_failures:
                    error.latch_record = adapter.integrity_failures[-1]
                raise error
            after = await asyncio.to_thread(view_identity, context.snapshot, ignore_git_index=True)
            if "reviewer_mutation" in response.stderr_tail or after != before:
                raise SideCallIntegrityError(
                    "read-only side call mutated its assigned repository view",
                    branch="content_hash",
                    checks={"content_hash": after != before, "git_state": False},
                    recovered=False,
                    protected_roots=protected_roots,
                    before_identity=before,
                    after_identity=after,
                    processes_terminated=bool(getattr(adapter, "processes_terminated", True)),
                )
            if self._side_context["abort"].is_set():
                raise self.artifacts.stopped("invocation stopped during investigation")
            if response.status == "timeout":
                status = "timeout"
                raise self.artifacts.exhausted(f"{stage} exhausted its admitted call window")
            if response.status != "completed":
                raise SideCallFailure(f"{stage} ended with {response.status}")
            if not response.final_text.strip() or len(response.final_text) > scaled(150_000):
                raise SideCallFailure("side call must return nonempty bounded evidence")
            status = "completed"
            return response.final_text
        except asyncio.CancelledError:
            detail = "cancelled"
            deadline = getattr(self, "_shutdown_deadline_monotonic", None)

            def remaining() -> float:
                return 12.0 if deadline is None else max(0.0, deadline - time.monotonic())
            try:
                cancel_task = asyncio.create_task(adapter.cancel_current_turn())
                done, pending = await asyncio.wait({cancel_task}, timeout=remaining())
                for unfinished in pending:
                    unfinished.cancel()
                if done:
                    await cancel_task
                else:
                    detail = "cancellation_cleanup_unfinished"
            except asyncio.CancelledError:
                detail = "cancellation_cleanup_unfinished"
            except Exception as exc:
                detail = "cancellation_cleanup_error:" + type(exc).__name__
            if "turn" in locals():
                # Cancelling the await does not stop its writer thread. Keep
                # the side-call owner until the shared shutdown deadline.
                done, _ = await asyncio.wait({turn}, timeout=remaining())
                if done:
                    try:
                        turn.result()
                    except BaseException:
                        pass
                else:
                    detail = "cancellation_writer_unfinished"
            raise
        except BaseException as exc:
            detail = type(exc).__name__
            raise
        finally:
            context.admission.finish(ticket, status=status, detail=detail)
            self._side_adapters.remove(adapter)

    def _integrity_error(
        self,
        context: Any,
        call_root: Path,
        stage: str,
        violation: dict[str, Any],
        *,
        before: str,
        guard_before: str,
        protected_roots: tuple[Path, ...],
        processes_terminated: bool,
    ) -> SideCallIntegrityError:
        rows = [row for row in violation.get("violations", []) if isinstance(row, dict)]
        changed_paths: list[str] = []
        branches: list[str] = []
        checks = {"content_hash": False, "git_state": False}
        for row in rows:
            changed_paths.extend(str(path) for path in row.get("changed_paths", []))
            branch_names = str(row.get("branch") or "").split("+")
            branches.extend(name for name in branch_names if name)
            row_checks = row.get("checks") if isinstance(row.get("checks"), dict) else {}
            for name in checks:
                checks[name] = checks[name] or bool(row_checks.get(name))
        # The wrapper has already stopped the turn and restored the view.
        # Re-check the full content identity now; this is the recovery proof
        # the controller requires before it may degrade.
        try:
            after_identity = view_identity(context.snapshot, ignore_git_index=True)
        except Exception:
            after_identity = None
        verified = bool(violation.get("verified"))
        recoverable = bool(violation.get("recoverable"))
        recovered = bool(
            verified and recoverable and processes_terminated and after_identity == before
        )
        prior_recovery = dict(violation.get("recovery") or {})
        quiet_recheck_changed = bool(prior_recovery.get("quiet_recheck_changed"))
        quiet_seconds = float(prior_recovery.get("quiet_period_seconds") or 0.0)
        guard_after: str | None = None
        if recovered and prior_recovery.get("quiet_recheck_done"):
            # The reviewer wrapper already waited and registered the guard.
            quiet_seconds = float(prior_recovery.get("quiet_period_seconds") or 0.0)
            quiet_recheck_changed = bool(prior_recovery.get("quiet_recheck_changed"))
            recovered = not quiet_recheck_changed
        elif recovered:
            # A process that escaped the turn can still write later. Recovery
            # is not trusted until the restored view survives a quiet period.
            quiet_seconds = recovery_quiet_seconds()
            still_verified, guard_after = revalidate_recovered_view(
                protected_roots, expected=guard_before, quiet_seconds=quiet_seconds
            )
            after_identity = view_identity(context.snapshot, ignore_git_index=True)
            quiet_recheck_changed = not still_verified
            recovered = still_verified and after_identity == before
        recovery = dict(violation.get("recovery") or {})
        recovery.update(
            {
                "verified": verified,
                "recoverable": recoverable,
                "recovered": recovered,
                "changed_paths": changed_paths[:200],
                "quiet_period_seconds": quiet_seconds,
                "quiet_recheck_changed": quiet_recheck_changed,
            }
        )
        if recovered and guard_after is not None:
            register_recovered_view(protected_roots, guard_after)
        error = SideCallIntegrityError(
            "read-only side call mutated its assigned repository view",
            changed_paths=changed_paths[:200],
            branch="+".join(sorted(set(branches))),
            checks=checks,
            recovery=recovery,
            recovered=recovered,
            protected_roots=protected_roots,
            before_identity=before,
            after_identity=after_identity,
            processes_terminated=processes_terminated,
            changed_path_count=max(
                sum(int(row.get("changed_path_count") or 0) for row in rows),
                len(changed_paths),
            ),
            changed_paths_truncated=any(bool(row.get("changed_paths_truncated")) for row in rows),
        )
        evidence_root = Path(context.session_dir) / "side-call-integrity"
        evidence_path: Path | None = None
        try:
            evidence_root.mkdir(parents=True, exist_ok=True)
            record = {
                "schema": PROTECTED_VIEW_SCHEMA,
                "stage": stage,
                "call_id": call_root.name,
                "changed_paths": list(error.changed_paths),
                "changed_path_count": error.changed_path_count,
                "changed_paths_truncated": error.changed_paths_truncated,
                "branch": error.branch,
                "checks": error.checks,
                "recovery": error.recovery,
                # This is a recovery candidate.  recover_side_call_integrity
                # promotes the record only after admission and counting.
                "recovered": False,
                "recovery_candidate": error.recovered,
                "processes_terminated": error.processes_terminated,
                "protected_roots": list(error.protected_roots),
            }
            evidence_path = evidence_root / f"{call_root.name}.json"
            evidence_path.write_text(
                json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
        except OSError:
            evidence_path = None
        error.evidence_path = evidence_path
        return error

    async def _cancel_side_calls(self) -> None:
        adapters = tuple(self._side_adapters)
        if adapters:
            deadline = getattr(self, "_shutdown_deadline_monotonic", None)
            remaining = 12.0 if deadline is None else max(0.0, deadline - time.monotonic())
            tasks = [asyncio.create_task(adapter.cancel_current_turn()) for adapter in adapters]
            done, pending = await asyncio.wait(tasks, timeout=remaining)
            for task in pending:
                task.cancel()
            for task in done:
                task.result()
