# Copyright (c) 2026 Chrys. All rights reserved.

"""Own-Work completed planning stages, independent of native session continuation."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import logging
import os
import re
import stat
import time
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from pact_core.artifacts import write_json_atomic
from pydantic import TypeAdapter

from ..capacity import scaled
from ..planning_checkpoint import NativeArtifactInvalid, PlanningCheckpointError
from ..public_review import PUBLIC_VERIFICATION_DEADLINE_MARGIN_SECONDS, PublicModelOutputStop, PublicTransportStop
from ..reviewer_execution import NonDegradableIntegrityError
from ..side_calls import SideCallIntegrityError, view_identity


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()


def owned_file(root: Path, path: Path, *, limit: int | None = None) -> bytes:
    limit = scaled(16 * 1024 * 1024) if limit is None else limit
    """Read bounded public native receipts, never provider config/state/streams."""
    root, path = root.absolute(), path.absolute()
    if ".." in path.parts or ".." in root.parts or not path.is_relative_to(root) or path == root:
        raise PlanningCheckpointError("native receipt escapes its invocation")
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise PlanningCheckpointError("native receipt path is a symlink")
        if parent == root:
            break
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
        raise PlanningCheckpointError("unsafe or oversized native receipt")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        opened = os.fstat(source.fileno())

        def identity(item):
            return item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns

        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or identity(opened) != identity(info):
            raise PlanningCheckpointError("native receipt changed while opening")
        raw = source.read(limit + 1)
        if len(raw) > limit or identity(os.fstat(source.fileno())) != identity(opened):
            raise PlanningCheckpointError("native receipt changed while reading")
        return raw


_RETRYABLE_STREAM = re.compile(
    r"^\[181001\] model call failed, reason: (?:"
    r"LLM stream timeout: stream frame timeout: stage=(?:idle_chunk|first_chunk),"
    r"|openAI API async stream error: (?:ReadError|APIConnectionError|APITimeoutError|ReadTimeout)\b"
    r"|openAI API async invoke error: (?:ReadError|APIConnectionError|APITimeoutError|ReadTimeout)\b)"
)
_REPEATED_OUTPUT = re.compile(
    r"\[181001\] model call failed, reason: LLM repeated stream output detected: "
    r"field=(?P<field>reasoning_content|content), unit="
    r"""(?:'(?:[^'\\\r\n]|\\.){1,320}'|"(?:[^"\\\r\n]|\\.){1,320}"), """
    r"repeat_count=(?P<count>[1-9][0-9]{0,3})"
)
_REPEATED_OUTPUT_REASON = "native_repeated_stream_output"
_LOG = logging.getLogger(__name__)
_USAGE_KEYS = ("input_tokens", "output_tokens", "total_tokens")


def _leaf_exceptions(exc: BaseException):
    """Yield independent failures without leaking an ExceptionGroup to callers."""
    if isinstance(exc, BaseExceptionGroup):
        for item in exc.exceptions:
            yield from _leaf_exceptions(item)
    else:
        yield exc


def _public_stream_failure(failure: Exception, root: Path) -> dict | None:
    """Classify only a native failure with matching machine receipts and error event."""
    from jiuwenswarm.long_horizon.worker import WorkerExecutionError

    if not isinstance(failure, WorkerExecutionError) or failure.code != "NATIVE_FAILED" or failure.result is None:
        return None
    result = failure.result
    if result.status != "failed" or type(result.exit_code) is not int or result.exit_code != 1:
        return None
    try:
        directory = Path(failure.output_dir).absolute()
        if directory == root.absolute() or not directory.is_relative_to(root.absolute()):
            return None
        raws = {
            name: owned_file(root, directory / name, limit=65536) for name in ("metadata.json", "terminal-result.json")
        }
        meta, terminal = (json.loads(raws[name]) for name in ("metadata.json", "terminal-result.json"))
        if not isinstance(meta, dict) or not isinstance(terminal, dict):
            return None
        error = terminal.get("error")
        if not isinstance(error, dict):
            return None
        message = error.get("message")
        session, request = terminal.get("session_id"), terminal.get("request_id")
        if not (
            terminal.get("schema_version") == "0.1"
            and terminal.get("type") == "result"
            and terminal.get("status") == "failed"
            and type(terminal.get("exit_code")) is int
            and terminal["exit_code"] == 1
            and error.get("code") == "RUNTIME_ERROR"
            and isinstance(message, str)
            and len(message) <= 4000
            and isinstance(session, str)
            and session
            and isinstance(request, str)
            and request
            and session == meta.get("session_id") == result.session_id
            and request == meta.get("request_id")
            and meta.get("status") == "failed"
            and meta.get("failure_code") == "NATIVE_FAILED"
            and re.fullmatch(r"public-verification-(?:review-)?ac1-(?:investigation|synthesis)", meta.get("stage", ""))
            and meta.get("read_only") is True
            and type(meta.get("actual_exit_code")) is int
            and meta["actual_exit_code"] == 1
            and meta.get("timed_out") is False
            and "signal" in meta
            and meta["signal"] is None
            and meta.get("protocol_error", "missing") is None
            and meta.get("budget_stop_observed") is False
        ):
            return None
        if not any(
            row.get("type") == "event"
            and row.get("event_type") == "chat.error"
            and row.get("request_id") == request
            and row.get("session_id") == session
            and isinstance(row.get("payload"), dict)
            and row["payload"].get("error") == message
            for row in result.events
            if isinstance(row, dict)
        ):
            return None
        anomaly = _REPEATED_OUTPUT.fullmatch(message)
        if anomaly is not None:
            if (
                error.get("retryable") is not False
                or int(anomaly["count"]) < 2
                or not re.fullmatch(r"[0-9]+-" + re.escape(meta["stage"]), directory.name)
            ):
                return None
            # The native diagnostic quotes a fragment of model output. Keep only
            # its field and repetition count in the outer review receipt.
            classification = {
                "reason": _REPEATED_OUTPUT_REASON, "field": anomaly["field"], "repeat_count": int(anomaly["count"])
            }
        elif _RETRYABLE_STREAM.match(message):
            classification = {
                "reason": "transport_idle_stream" if "LLM stream timeout:" in message else "transport_connection_stream"
            }
        else:
            return None
        return {
            **classification,
            "failed_session_id": session,
            "failed_request_id": request,
            "stage": meta["stage"],
            "machine_receipts": [
                {"path": str(directory / name), "sha256": hashlib.sha256(raw).hexdigest()} for name, raw in raws.items()
            ],
        }
    except (OSError, ValueError, TypeError, KeyError, AttributeError, PlanningCheckpointError):
        return None


def public_transport_failure(failure: Exception, root: Path) -> dict | None:
    """Return only failures that authorize the existing one-shot transport retry."""
    proof = _public_stream_failure(failure, root)
    return proof if proof is not None and proof["reason"] != _REPEATED_OUTPUT_REASON else None


def known_retry_usage(parent, current):
    units = [*parent, current]
    known = {
        key: sum(unit[key] for unit in units)
        for key in _USAGE_KEYS
        if all(isinstance(unit, dict) and type(unit.get(key)) is int and unit[key] >= 0 for unit in units)
    }
    unknown = [key for key in _USAGE_KEYS if key not in known]
    return known, unknown


def receipt_json(root: Path, path: Path) -> dict:
    try:
        value = json.loads(owned_file(root, path))
        if not isinstance(value, dict):
            raise ValueError("object required")
        return value
    except (OSError, ValueError, TypeError) as exc:
        raise PlanningCheckpointError("invalid native receipt") from exc


def reviewed_origin(root: Path, criterion: Any, value: Any, store_type: Any, expected_identity: dict) -> dict:
    try:
        return _reviewed_origin(root, criterion, value, store_type, expected_identity)
    except PlanningCheckpointError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise PlanningCheckpointError("invalid independent native criterion provenance") from exc


def _reviewed_origin(root: Path, criterion: Any, value: Any, store_type: Any, expected_identity: dict) -> dict:
    """Use public artifacts and the public native store; no private model state."""
    criterion_value = criterion.model_dump(mode="json")
    matches = []
    folder = root / "public-verification-checkpoints"
    if not folder.exists() or folder.is_symlink():
        raise PlanningCheckpointError("native reviewed criterion receipt missing")
    for path in folder.glob("*/*.json"):
        record = receipt_json(root, path)
        if record.get("criterion") != criterion_value:
            continue
        if record.get("identity") != expected_identity:
            raise PlanningCheckpointError("native reviewed criterion does not bind the current public inputs/runtime")
        # The native store rechecks identity, schema, criterion/value hashes,
        # normalized fields, coverage, commands and its own secure file contract.
        loaded = store_type(path.parent.parent, record["identity"]).load_with_receipt(criterion)
        normalized = (
            []
            if loaded is None
            else [
                check.model_copy(update={"id": f"ac1-check{n}"}).model_dump(mode="json")
                for n, check in enumerate(loaded[0].checks, 1)
            ]
        )
        if loaded is None or normalized != [check.model_dump(mode="json") for check in value.checks]:
            raise PlanningCheckpointError("native reviewed criterion differs from returned value")
        origin = record["provenance"]
        refs = [{"path": str(path), "sha256": loaded[1]["sha256"]}]
        sessions: set[str] = set()
        for role, stage in (("generation", "public-verification-ac1"), ("review", "public-verification-review-ac1")):
            proof = origin[role]
            source = Path(proof["path"])
            raw = owned_file(root, source)
            if hashlib.sha256(raw).hexdigest() != proof["sha256"]:
                raise PlanningCheckpointError("native public stage receipt changed")
            receipt = json.loads(raw)
            if receipt["stage"] != stage or receipt["status"] != "completed":
                raise PlanningCheckpointError("native public stage did not complete")
            if receipt["synthesis_tool_policy"] != "none" or receipt["native_turns"] != proof["native_turns"]:
                raise PlanningCheckpointError("native public stage policy/provenance mismatch")
            pair = {receipt["investigation_session_id"], receipt["synthesis_session_id"]}
            if len(pair) != 2 or any(not isinstance(x, str) or not x for x in pair) or sessions & pair:
                raise PlanningCheckpointError("public generation and review sessions are not independent")
            sessions.update(pair)
            if any(proof[k] != receipt[k] for k in ("investigation_session_id", "synthesis_session_id")):
                raise PlanningCheckpointError("native public stage session binding changed")
            refs.append({"path": str(source), "sha256": proof["sha256"]})
            accepted = []
            for artifact in root.glob(f"accepted-{stage}-*.json"):
                candidate = receipt_json(root, artifact)
                if candidate.get("public_stage_receipt", {}).get("path") == str(source):
                    if (
                        candidate.get("stage") != stage
                        or candidate.get("session_id") != receipt["synthesis_session_id"]
                    ):
                        raise PlanningCheckpointError("native accepted artifact session/stage mismatch")
                    if role == "generation":
                        # Bind the generation value to this exact criterion. The
                        # receipt alone proves a run, not which criterion it served.
                        generated = candidate.get("value")
                        checks = generated.get("checks") if isinstance(generated, dict) else None
                        if not isinstance(checks, list) or not checks:
                            raise PlanningCheckpointError("native generation did not contain criterion checks")
                        for check in checks:
                            targets = check.get("target_ac_ids") if isinstance(check, dict) else None
                            if (
                                not isinstance(targets, list)
                                or not targets
                                or any(not isinstance(target, str) or target != criterion.id for target in targets)
                            ):
                                raise PlanningCheckpointError("native generation belongs to another criterion")
                    if role == "review" and candidate.get("value") != record["value"]:
                        raise PlanningCheckpointError("cached value differs from the independently reviewed artifact")
                    accepted.append(artifact)
            if len(accepted) != 1:
                raise PlanningCheckpointError("native accepted stage artifact missing or ambiguous")
            refs.append({"path": str(accepted[0]), "sha256": hashlib.sha256(owned_file(root, accepted[0])).hexdigest()})
            observed = set()
            for turn in receipt["native_turns"]:
                if turn["status"] != "completed" or turn["stage"] not in {
                    stage + "-investigation",
                    stage + "-synthesis",
                }:
                    raise PlanningCheckpointError("invalid completed native public turn")
                call = Path(turn["output_dir"])
                metadata = receipt_json(root, call / "metadata.json")
                terminal = receipt_json(root, call / "terminal-result.json")
                if (
                    metadata["status"] != "completed"
                    or metadata["stage"] != turn["stage"]
                    or metadata["session_id"] != turn["session_id"]
                    or metadata["read_only"] is not True
                    or terminal["status"] != "completed"
                    or terminal["session_id"] != turn["session_id"]
                ):
                    raise PlanningCheckpointError("native public call record identity/status changed")
                phase = "synthesis" if turn["stage"].endswith("-synthesis") else "investigation"
                if turn["session_id"] != receipt[phase + "_session_id"]:
                    raise PlanningCheckpointError("native public call has another stage's session")
                if phase == "synthesis" and metadata["tool_policy"] != "none":
                    raise PlanningCheckpointError("native public synthesis was not tool-free")
                observed.add(phase)
                # Small completed records only. Never hash/read events, state,
                # native config snapshots or provider files to create this link.
                for name in ("metadata.json", "terminal-result.json"):
                    target = call / name
                    refs.append({"path": str(target), "sha256": hashlib.sha256(owned_file(root, target)).hexdigest()})
            if observed != {"investigation", "synthesis"}:
                raise PlanningCheckpointError("native public stage lacks both phases")
        matches.append({"native_root": str(root), "references": refs, "sessions": sorted(sessions)})
    if len(matches) != 1:
        raise PlanningCheckpointError("native reviewed criterion origin is absent or ambiguous")
    return matches[0]


class NativeStageRunner:
    """Delegate real calls to native; reuse only exact completed typed stages."""

    def __init__(
        self,
        *,
        runner,
        checkpoint,
        context,
        types,
        verification,
        store_type,
        validate_pair,
        stopped,
        exhausted,
        abort,
        snapshot_identity,
        expected_worker_identity,
        public_runner_factory=None,
        public_concurrency: int = 1,
    ):
        self.runner, self.checkpoint, self.context = runner, checkpoint, context
        self.types, self.verification, self.store_type = types, verification, store_type
        self.integrity_error = importlib.import_module(types.__package__ + ".budget").SideCallIntegrityError
        self.validate_pair, self.stopped, self.exhausted = validate_pair, stopped, exhausted
        self.abort, self.snapshot_identity = abort, snapshot_identity
        self.expected_worker_identity = expected_worker_identity
        self.public_runner_factory = public_runner_factory
        self.public_concurrency = max(1, int(public_concurrency))
        self.public_review_started = False
        self.public_review_completed: set[str] = set()
        self.goal = None
        from ..planning_calls import PlanningCalls

        self.calls = PlanningCalls(context.session_dir, checkpoint.binding, context.deadline)

    def public_identity(self, prompt, criterion):
        # Exact public augmentation in the pinned native model. Binding also
        # includes that model's bytes, so upstream changes require a new key.
        if self.runner.runtime_evidence:
            prompt += (
                "\n\nVerified public runtime capabilities supplied by the launcher (executable files and SHA-256 "
                "checked in this runtime):\n"
                + self.runner.runtime_evidence
                + "\nThese commands exist outside the frozen Git source. Do not search the repository to decide "
                "whether they exist. Use them only for an explicitly requested capability; they do not add "
                "acceptance criteria or reveal private grading tests. Generate the actual observable assertions."
            )
        heads = [root.git_head for root in self.context.snapshot.roots if root.is_primary and root.git_head]
        if heads:
            prompt += "\n\nFrozen primary Git baseline commit (identity evidence): " + ", ".join(heads)
        # Older external SDKs omit this field from their native prompt.
        prompt += getattr(self.runner, "public_check_repository_context", "")
        identity = {
            "snapshot_manifest_hash": self.context.snapshot.manifest_hash,
            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            "worker_identity": self.expected_worker_identity,
            "source_workflow_revision": "0561cf0fa584fd56fdcd98c90cca6a5bc1890d95",
            "acceptance_criteria": [criterion.model_dump(mode="json")],
        }
        instructions_digest = getattr(self.runner, "public_check_instructions_sha256", None)
        if instructions_digest is not None:
            identity["public_check_instructions_sha256"] = instructions_digest
        return identity

    def guard(self) -> None:
        # A concurrent budget stop/cancel must never hide a frozen-view mutation.
        if view_identity(self.context.snapshot) != self.snapshot_identity:
            raise PlanningCheckpointError("planning checkpoint frozen source changed")
        if self.abort.is_set():
            raise self.stopped("planning checkpoint invocation stopped")
        if self.context.deadline is None or time.time() >= self.context.deadline:
            raise self.exhausted("planning checkpoint has no remaining admitted time")

    async def frozen_base_evidence(self, requirement: str) -> str:
        """Reuse exact public stage input under the existing Work/S0/config binding."""
        self.guard()
        key = "input:base-evidence:" + hashlib.sha256(requirement.encode()).hexdigest()
        row = self.checkpoint.read(key)
        if row is None:
            module = importlib.import_module(self.types.__package__ + ".evidence")
            packet = await asyncio.to_thread(module.collect_base_evidence, self.context.snapshot, requirement)
            self.guard()
            row = {
                "schema": "long-horizon/frozen-base-evidence/v1",
                "packet": packet,
                "packet_sha256": hashlib.sha256(packet.encode()).hexdigest(),
                "requirement_sha256": hashlib.sha256(requirement.encode()).hexdigest(),
                "snapshot_manifest_hash": self.context.snapshot.manifest_hash,
            }
            self._validate_base_evidence(row, requirement)
            self.checkpoint.save(key, row)
        self._validate_base_evidence(row, requirement)
        self.guard()
        return row["packet"]

    def _validate_base_evidence(self, row, requirement):
        if (
            not isinstance(row, dict)
            or row.get("schema") != "long-horizon/frozen-base-evidence/v1"
            or not isinstance(row.get("packet"), str)
            or not 0 < len(row["packet"]) <= scaled(8000)
            or row.get("packet_sha256") != hashlib.sha256(row["packet"].encode()).hexdigest()
            or row.get("requirement_sha256") != hashlib.sha256(requirement.encode()).hexdigest()
            or row.get("snapshot_manifest_hash") != self.context.snapshot.manifest_hash
        ):
            raise PlanningCheckpointError("frozen base-evidence input mismatch")

    async def cached(
        self, stage, inputs, kind, invoke, validate, *, origin=None, origin_root=None, invalid_is_fatal=False
    ):
        self.guard()
        key = stage + ":" + hashlib.sha256(canonical(inputs)).hexdigest()
        row = self.checkpoint.read(key)
        adapter = TypeAdapter(kind)
        if row is not None:
            try:
                value = adapter.validate_json(canonical(row["value"]), strict=True)
                validate(value)
                if origin is not None:
                    proof = row["origin"]
                    if not Path(proof["native_root"]).is_relative_to(self.context.session_dir):
                        raise PlanningCheckpointError("native criterion origin belongs to another Work")
                    for ref in proof["references"]:
                        raw = owned_file(Path(proof["native_root"]), Path(ref["path"]))
                        if hashlib.sha256(raw).hexdigest() != ref["sha256"]:
                            raise PlanningCheckpointError("reviewed native origin changed after checkpoint")
                    current = origin(value, Path(proof["native_root"]))
                    if current != proof:
                        raise PlanningCheckpointError("reviewed native origin no longer matches")
                self.guard()
                return value, {}
            except PlanningCheckpointError:
                raise
            except (ValueError, TypeError, KeyError, OSError) as exc:
                raise PlanningCheckpointError("invalid completed native planning checkpoint") from exc
        call_stage = f"proposal-{inputs['sample']}" if stage == "proposal" else stage
        completed_call = await self.calls.run(call_stage, key, invoke, guard=self.guard)
        value, usage = completed_call.value
        self.guard()
        try:
            value = adapter.validate_python(value, strict=True)
            validate(value)
        except (ValueError, TypeError) as exc:
            if invalid_is_fatal:
                raise NativeArtifactInvalid("native public verification did not pass its own contract") from exc
            # The service may perform additional sanitation/correction. An
            # invalid raw result must never become a completed checkpoint.
            return value, usage
        proof = (
            origin(value, origin_root() if origin_root is not None else Path(self.runner.output_dir))
            if origin is not None
            else None
        )
        self.checkpoint.save(
            key,
            {"value": adapter.dump_python(value, mode="json", by_alias=True), "original_usage": usage, "origin": proof},
        )
        self.calls.mark_validated(completed_call.call_id)
        return value, usage

    async def propose(self, prompt: str, *, sample_index: int):
        def valid(result):
            if (
                result.proposal is None
                or result.investigation.status != "completed"
                or result.investigation.coverage_status != "sufficient"
                or result.investigation.validation_errors
                or result.investigation.coverage_errors
            ):
                raise ValueError("proposal is not validated")

        async def invoke():
            value = await self.runner.propose(prompt, sample_index=sample_index)
            return value, {"native_usage": list(value.usage_details)}

        try:
            value, usage = await self.cached(
                "proposal", {"prompt": prompt, "sample": sample_index}, self.types.ProposalModelResult, invoke, valid
            )
        except PlanningCheckpointError as exc:
            # Native proposal fan-out tolerates ordinary model failures. Its
            # integrity exception must cross that boundary without degradation.
            raise self.integrity_error(str(exc)) from exc
        if not usage:
            return replace(value, usage_details=())
        return value

    async def select(self, prompt: str):
        # Service-level selector audits, including correction attempts, remain
        # authoritative. Never cache a merely schema-valid selector response.
        return await self.runner.select(prompt)

    def record_selector_pass(self, **kwargs):
        self.runner.record_selector_pass(**kwargs)

    async def generate_pact_goal_contract(self, prompt: str):
        value, usage = await self.cached(
            "goal",
            prompt,
            self.types.PactGoalContract,
            lambda: self.runner.generate_pact_goal_contract(prompt),
            lambda value: None,
        )
        self.goal = value
        return value, usage

    async def generate_pact_initial_plan(self, prompt: str):
        def valid(value):
            if self.goal is None:
                raise ValueError("goal must precede plan")
            self.validate_pair(self.goal, value)

        try:
            return await self.cached(
                "plan",
                prompt,
                self.types.PactInitialPlan,
                lambda: self.runner.generate_pact_initial_plan(prompt),
                valid,
            )
        except PlanningCheckpointError as exc:
            # The native plan correction loop catches ValueError, including the
            # host contract-error superclass. Corrupt evidence cannot be repaired
            # by generating a different plan under a correction prompt.
            raise self.integrity_error(str(exc)) from exc

    async def public_invoke(self, prompt, criterion, inputs, runner):
        from jiuwenswarm.long_horizon.worker import WorkerExecutionError

        try:
            return await runner.generate_public_verification(prompt, acceptance_criteria=(criterion,))
        except WorkerExecutionError as failure:
            self.guard()
            proof = _public_stream_failure(failure, Path(runner.output_dir))
            self.guard()
            if proof is None:
                raise
            if proof["reason"] == _REPEATED_OUTPUT_REASON:
                failure.public_model_output_stop = PublicModelOutputStop(criterion.id, proof)
                raise
            review_deadline = self.context.deadline - PUBLIC_VERIFICATION_DEADLINE_MARGIN_SECONDS
            if review_deadline - time.time() <= 5:
                failure.public_transport_stop = PublicTransportStop(
                    "public_transport_retry_no_time", criterion.id, proof
                )
                raise
            # Exactly one extra call, still admitted by the public native API.
            # Its local investigation/synthesis session variables start fresh;
            # the existing runner sequence and all previous output dirs remain.
            parent_usage = getattr(failure, "usage_details", ())  # External native SDK exception boundary.
            if not parent_usage:
                parent_usage = (failure.result.usage,)
            parent_usage = [
                {
                    key: unit[key]
                    for key in _USAGE_KEYS
                    if isinstance(unit, dict) and type(unit.get(key)) is int and unit[key] >= 0
                }
                for unit in parent_usage
            ]
            receipt = {
                "schema": "long-horizon/public-planning-transport-retry/v1",
                "retry_number": 1,
                "status": "intent",
                "original_context_deadline": self.context.deadline,
                "checkpoint_binding": self.checkpoint.binding,
                "inputs_sha256": hashlib.sha256(canonical(inputs)).hexdigest(),
                "criterion_id": criterion.id,
                "parent": proof,
                "parent_usage": parent_usage,
                "native_session_continuation": False,
                "independent_grade_claimed": False,
            }
            path = Path(runner.output_dir) / ("public-transport-retry-" + uuid4().hex + ".json")
            try:
                with path.open("x") as handle:
                    os.chmod(path, 0o600)
                    json.dump(receipt, handle)
                    handle.flush()
                    os.fsync(handle.fileno())
            except (OSError, ValueError):
                _LOG.warning("public transport retry not started: intent receipt unavailable")
                raise failure
            started = False
            try:
                self.guard()
                await asyncio.sleep(min(5.0, max(0.0, self.context.deadline - time.time())))
                self.guard()
                started = True
                value, usage = await runner.generate_public_verification(prompt, acceptance_criteria=(criterion,))
                known, unknown = known_retry_usage(parent_usage, usage)
                receipt.update(
                    status="completed",
                    retry_usage={k: usage[k] for k in _USAGE_KEYS if type(usage.get(k)) is int},
                    usage_totals=known,
                    usage_unknown_fields=unknown,
                )
                return value, {**known, **({"usage_unknown_fields": unknown} if unknown else {})}
            except BaseException as retry_error:
                receipt.update(status="failed" if started else "not_started")
                if started and isinstance(retry_error, WorkerExecutionError):
                    retry_proof = _public_stream_failure(retry_error, Path(runner.output_dir))
                    if retry_proof is not None and retry_proof["failed_request_id"] != proof["failed_request_id"]:
                        receipt["retry_failure"] = retry_proof
                        if retry_proof["reason"] == _REPEATED_OUTPUT_REASON:
                            retry_error.public_model_output_stop = PublicModelOutputStop(criterion.id, retry_proof)
                        else:
                            retry_error.public_transport_stop = PublicTransportStop(
                                "public_transport_retry_exhausted", criterion.id, proof, retry_proof
                            )
                raise
            finally:
                try:
                    write_json_atomic(path, receipt)
                except (OSError, ValueError):
                    _LOG.warning("public transport retry receipt update failed; inspect retained intent/native records")

    def _public_failure_priority(self, exc: BaseException) -> int | None:
        """Select a stable original failure; never expose a TaskGroup wrapper."""
        if isinstance(exc, asyncio.CancelledError):
            return None
        if not isinstance(exc, Exception):
            return 0
        if not isinstance(exc, NativeArtifactInvalid) and isinstance(
            exc,
            (
                PlanningCheckpointError,
                SideCallIntegrityError,
                NonDegradableIntegrityError,
                self.integrity_error,
            ),
        ):
            return 1
        if isinstance(exc, self.stopped):
            return 2
        if isinstance(exc, NativeArtifactInvalid):
            return 3
        return 4

    def _selected_public_failure(self, failures: dict[int, BaseException]) -> BaseException | None:
        candidates = []
        for index in sorted(failures):
            for exc in _leaf_exceptions(failures[index]):
                priority = self._public_failure_priority(exc)
                if priority is not None:
                    candidates.append((priority, index, exc))
        if not candidates:
            return None
        selected = min(candidates, key=lambda row: (row[0], row[1]))[2]
        for _, index, exc in candidates:
            if exc is selected:
                continue
            selected.add_note(f"public criterion chain {index} also failed: {type(exc).__name__}: {str(exc)[:1000]}")
        return selected

    def _validate_public_separation(self, proofs: list[dict | None]) -> None:
        """Reject a shared native root/store or any cross-criterion receipt path."""
        roots: dict[str, int] = {}
        sessions: dict[str, int] = {}
        references: dict[str, int] = {}
        for index, proof in enumerate(proofs, 1):
            if not isinstance(proof, dict):
                raise PlanningCheckpointError("public criterion provenance is missing")
            root = proof.get("native_root")
            if not isinstance(root, str) or not root:
                raise PlanningCheckpointError("public criterion native root is missing")
            if root in roots:
                raise PlanningCheckpointError("public criteria share a native criterion store")
            roots[root] = index
            for session in proof.get("sessions", ()):
                if not isinstance(session, str) or not session:
                    raise PlanningCheckpointError("public criterion session is invalid")
                if session in sessions and sessions[session] != index:
                    raise PlanningCheckpointError("public criteria share a native session")
                sessions[session] = index
            for reference in proof.get("references", ()):
                path = reference.get("path") if isinstance(reference, dict) else None
                if not isinstance(path, str) or not path:
                    raise PlanningCheckpointError("public criterion receipt path is invalid")
                if path in references and references[path] != index:
                    raise PlanningCheckpointError("public criteria share a native receipt path")
                references[path] = index

    @staticmethod
    def _public_inputs(prompt: str, full: list[dict], criterion: dict) -> dict:
        return {"prompt": prompt, "ordered_criteria": full, "criterion": criterion}

    @classmethod
    def _public_key(cls, inputs: dict) -> str:
        return "public-criterion:" + hashlib.sha256(canonical(inputs)).hexdigest()

    def public_review_progress(self, prompt: str, acceptance_criteria: tuple) -> tuple[str, ...]:
        """Durable completed-criterion ids, independent of a previous task run."""
        full = [criterion.model_dump(mode="json") for criterion in acceptance_criteria]
        completed = []
        for index, criterion in enumerate(acceptance_criteria):
            key = self._public_key(self._public_inputs(prompt, full, full[index]))
            if criterion.id in self.public_review_completed:
                completed.append(criterion.id)
                continue
            try:
                present = self.checkpoint.has(key)
            except PlanningCheckpointError:
                # Presence is only a classification hint. The normal reader
                # still fails closed if this record is actually consumed.
                present = False
            if present:
                completed.append(criterion.id)
        return tuple(completed)

    def _public_group_failure(
        self, exc: BaseException, failures: dict[int, BaseException], *, cancelling: bool
    ) -> BaseException:
        """Choose one stable failure; external cancellation outranks ordinary siblings."""
        selected = self._selected_public_failure(failures)
        if selected is not None:
            if cancelling and self._public_failure_priority(selected) != 1:
                # A genuine integrity failure still wins and fails closed.
                return asyncio.CancelledError()
            return selected
        if cancelling or isinstance(exc, asyncio.CancelledError):
            return asyncio.CancelledError()
        leaves = [item for item in _leaf_exceptions(exc) if not isinstance(item, asyncio.CancelledError)]
        return leaves[0] if leaves else asyncio.CancelledError()

    async def generate_public_verification(self, prompt: str, *, acceptance_criteria: tuple):
        criteria = tuple(acceptance_criteria)
        if not criteria or len({c.id for c in criteria}) != len(criteria):
            raise ValueError("nonempty unique full criteria required")
        self.public_review_started = True
        full = [c.model_dump(mode="json") for c in criteria]
        count = len(criteria)
        results: list[Any] = [None] * count
        proofs: list[dict | None] = [None] * count
        usages: list[dict] = [{}] * count
        failures: dict[int, BaseException] = {}
        roots: dict[str, int] = {}
        order = iter(range(1, count + 1))

        async def chain(index: int, criterion: Any) -> None:
            inputs = self._public_inputs(prompt, full, full[index - 1])
            child = None

            def use_child():
                nonlocal child
                if child is None:
                    if self.public_runner_factory is None:
                        child = self.runner
                    else:
                        child = self.public_runner_factory()
                        if getattr(child, "output_dir", None) is None:
                            raise PlanningCheckpointError("public criterion runner has no output directory")
                        if child is self.runner or Path(child.output_dir) == Path(self.runner.output_dir):
                            raise PlanningCheckpointError("public criterion runner must not share the parent runner")
                        if getattr(child, "runtime_evidence", None) != getattr(self.runner, "runtime_evidence", None):
                            raise PlanningCheckpointError("public criterion runner runtime evidence mismatch")
                        root = str(Path(child.output_dir).absolute())
                        if root in roots:
                            raise PlanningCheckpointError("public criterion runners must have disjoint native roots")
                        roots[root] = index
                return child

            def valid(value):
                value.validate_coverage({criterion.id})
                value.validate_generated_commands()

            def origin(value, root):
                proof = reviewed_origin(
                    root, criterion, value, self.store_type, self.public_identity(prompt, criterion)
                )
                proofs[index - 1] = proof
                return proof

            value, usage = await self.cached(
                "public-criterion",
                inputs,
                self.verification,
                lambda: self.public_invoke(prompt, criterion, inputs, use_child()),
                valid,
                origin=origin,
                origin_root=lambda: Path(use_child().output_dir),
                invalid_is_fatal=True,
            )
            results[index - 1] = value
            self.public_review_completed.add(criterion.id)
            usages[index - 1] = usage

        async def lane() -> None:
            # A lane takes the next original criterion only after it has joined
            # the prior chain, so #5 starts as soon as any first lane is free.
            for index in order:
                try:
                    await chain(index, criteria[index - 1])
                except BaseException as exc:
                    failures[index] = exc
                    raise

        # A factory is what makes a new native store per criterion. Without
        # one the historical shared-runner path remains strictly serial.
        lanes = min(self.public_concurrency, count) if self.public_runner_factory is not None else 1

        async def run_lanes() -> None:
            async with asyncio.TaskGroup() as group:
                for _ in range(lanes):
                    group.create_task(lane())

        # On Python 3.11/3.12 a failing child can leave TaskGroup's own
        # bookkeeping cancellation on its parent task. Keep that parent
        # disposable so the calling task only ever sees genuine cancellation.
        # A normal await joins it: cancelling the caller cancels the awaited
        # task, which finishes only after its TaskGroup has joined every lane.
        # Baseline the caller: an already delivered request is not a new one.
        owner = asyncio.current_task()
        before = owner.cancelling() if owner is not None else 0
        worker = asyncio.create_task(run_lanes(), name="public-criterion-lanes")
        try:
            await worker
        except asyncio.CancelledError as cancelled:
            raise self._public_group_failure(cancelled, failures, cancelling=True) from None
        except BaseException as exc:
            raise self._public_group_failure(
                exc,
                failures,
                cancelling=owner is not None and owner.cancelling() > before,
            ) from None

        # Only concurrent independent runners need this extra separation proof.
        # The historical serial path intentionally shares one native directory.
        if self.public_runner_factory is not None:
            self._validate_public_separation(proofs)
        checks = []
        for index, value in enumerate(results, 1):
            if value is None:
                raise PlanningCheckpointError("public criterion result is missing")
            checks.extend(
                check.model_copy(update={"id": f"ac{index}-check{n}"}) for n, check in enumerate(value.checks, 1)
            )
        result = self.verification(
            schema="jiuwenswarm/pact-public-verification/v1",
            status="ready",
            reason="Every original criterion retains native generation and independent review provenance.",
            checks=checks,
        )
        result.validate_coverage({c.id for c in criteria})
        result.validate_generated_commands()
        totals = {
            name: sum(u[name] if u else 0 for u in usages)
            for name in _USAGE_KEYS
            if all(not u or type(u.get(name)) is int for u in usages)
        }
        unknown = [name for name in _USAGE_KEYS if name not in totals]
        return result, {**totals, **({"usage_unknown_fields": unknown} if unknown else {})} if any(
            totals.values()
        ) or unknown else {}

    async def propose_legacy_v1(self, prompt, *, sample_index):
        return await self.runner.propose_legacy_v1(prompt, sample_index=sample_index)

    async def select_legacy_v1(self, prompt):
        return await self.runner.select_legacy_v1(prompt)
