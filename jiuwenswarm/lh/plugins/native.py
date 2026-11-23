"""Native clarification/artifact boundary shared by the two existing plugins."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import inspect
import re
import time
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Callable

from ..capacity import scaled
from ..confirmed_behavior import CONFIRMED_BEHAVIOR_CHECK
from ..planning_checkpoint import NativeArtifactInvalid, PlanningCheckpoint, PlanningCheckpointError
from ..ports import Capabilities, ClarificationContext
from ..public_review import (
    PUBLIC_VERIFICATION_DEADLINE_MARGIN_SECONDS,
    PublicModelOutputStop,
    PublicReviewProgress,
    PublicTransportStop,
    capacity_os_error,
    clear_public_review,
    recoverable_public_review_error,
    set_public_review,
)
from ..reviewer_execution import NonDegradableIntegrityError
from ..side_calls import SideCallIntegrityError, SideCalls
from ..snapshot_resources import SnapshotResources, native_stage_record

# A public-verification artifact that keeps failing its own validation is
# repaired at most this many attempts per criterion stage before the stage is
# treated as unavailable. Native agents may allow more; the controller refuses
# further admissions instead of spending the clarification budget on them.
PUBLIC_VERIFICATION_ATTEMPTS = 3
_PUBLIC_ATTEMPT = re.compile(r"public-verification[^:]*:.*:attempt-(\d+)(?::continuation-\d+)?$")


class PublicRepairCap:
    """Admission wrapper bounding consecutive public-verification repairs per stage."""

    def __init__(self, inner: Any, exhausted: type[BaseException], *, attempts: int) -> None:
        self._inner = inner
        self._exhausted = exhausted
        self._attempts = attempts
        self.capped: list[str] = []

    def admit(self, stage: str) -> Any:
        match = _PUBLIC_ATTEMPT.search(stage)
        if match is not None and int(match.group(1)) > self._attempts:
            self.capped.append(stage)
            raise self._exhausted(
                f"public verification repair cap ({self._attempts} attempts) reached before {stage}; "
                "the stage is degraded to unavailable"
            )
        return self._inner.admit(stage)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class NativeArtifacts:
    def __init__(self, pact_package: str, clarification_package: str) -> None:
        self._evidence = importlib.import_module(f"{pact_package}.long_horizon_evidence")
        self._store = importlib.import_module(f"{clarification_package}.artifacts").ClarificationArtifactStore
        self.exhausted = importlib.import_module(f"{clarification_package}.budget").SideCallBudgetExhausted
        self.stopped = self._evidence.ClarificationStopped
        self._snapshot_resources = SnapshotResources()

    def configure_snapshot_resources(self, *, max_total_bytes: int) -> None:
        self._snapshot_resources = SnapshotResources(max_total_bytes)

    def create_store(self, session_dir: Path) -> Any:
        return self._store(session_dir, 1)

    def stage_record(self, **kwargs: Any) -> Any:
        return native_stage_record(self._evidence, self._snapshot_resources, **kwargs)

    def evidence_checkpoint(self, **kwargs: Any) -> Any:
        return self._evidence.evidence_checkpoint(**kwargs)

    def load_stage_snapshot(self, output: Path, record: dict[str, object]) -> Any:
        return self._evidence.load_stage_snapshot(output, record)

    def read_clarification_bundle(self, turn_root: Path, *, original_task: str) -> Any:
        return self._evidence.read_clarification_bundle(turn_root, original_task=original_task)

    def make_admission(self, budget: Any, **kwargs: Any) -> Any:
        return self._evidence.BenchSideCallAdmission(budget, **kwargs)


class NativeClarification(SideCalls):
    capabilities = Capabilities(True, True, True, True)
    verification_strategy = "frozen-public-checks"
    resume_completed_planning = False
    # A native plugin opts in only when its worker and native model support
    # independent per-criterion output directories. The generic plugins keep
    # the original serial path.
    public_criterion_concurrency = 1
    _public_evidence_unavailable = ()

    @property
    def requires_complete_public_review(self) -> bool:
        """True only for the real native suite path with generated public checks.

        An explicit ``--verify`` command and every non-frozen verification
        profile keep their pre-change optional public-verification behavior.
        """
        args = getattr(self, "_side_args", None)
        return (
            self.public_criterion_concurrency > 1
            and self.resume_completed_planning
            and getattr(self, "_runner_factory", None) is None
            and getattr(args, "workflow", None) == "suite"
            and getattr(args, "verify_command", None) is None
            and self.verification_strategy == "frozen-public-checks"
        )

    def __init__(self, pact_package: str, clarification_package: str, runner_factory: Callable | None) -> None:
        super().__init__()
        self.artifacts = NativeArtifacts(pact_package, clarification_package)
        self._service_module = importlib.import_module(f"{clarification_package}.service")
        self._service = self._service_module.ClarificationService
        self._types = importlib.import_module(f"{clarification_package}.types")
        native_model = importlib.import_module(f"{clarification_package}.model")
        self._public_evidence_unavailable = getattr(native_model, "PublicEvidenceUnavailable", ())
        self._revision = importlib.import_module(f"{clarification_package}.types").RequirementRevision
        self._runner_factory = runner_factory
        self._planning_call_managers = {}
        self._active_clarification: asyncio.Task | None = None
        self.public_verification_degradation: dict[str, Any] | None = None

    async def planning_runner(self, context: ClarificationContext, original_task: str):
        use_cache = (
            self.resume_completed_planning and self._runner_factory is None and self._side_args.workflow == "suite"
        )
        if use_cache:
            # Validate the captured native object before default_runner can
            # report an expired window. Mutation must retain integrity priority.
            validator = importlib.import_module(self._types.__package__ + ".event_evidence").validate_model_view
            integrity_error = importlib.import_module(self._types.__package__ + ".budget").SideCallIntegrityError
            try:
                await asyncio.to_thread(validator, context.snapshot)
            except integrity_error as exc:
                raise PlanningCheckpointError("planning checkpoint frozen source changed") from exc
        runner = self._runner_factory(context) if self._runner_factory else self.default_runner(context)
        if not use_cache:
            return runner
        from ..side_calls import view_identity
        from .native_checkpoint import NativeStageRunner

        snapshot_identity = await asyncio.to_thread(view_identity, context.snapshot)
        native_modules = [
            importlib.import_module(self._types.__package__ + "." + name)
            for name in (
                "types",
                "service",
                "model",
                "verification",
                "public_checkpoint",
                "validation",
                "prompts",
                "event_evidence",
                "evidence",
            )
        ]
        from ..planning_calls import PlanningCalls

        code_paths = (
            Path(inspect.getfile(PlanningCalls)),
            Path(__file__),
            Path(inspect.getfile(type(runner))),
            Path(inspect.getfile(PlanningCheckpoint)),
            Path(inspect.getfile(view_identity)),
            *(Path(module.__file__) for module in native_modules),
        )
        checkpoint = PlanningCheckpoint(
            context.session_dir,
            {
                "protocol": "native-completed-planning/v1",
                "plugin": self.spec.id,
                "execution": self._side_binding.fingerprint,
                "configuration": self.configuration(self._side_args.profile_name),
                "evidence_configuration": self.evidence_configuration(),
                "original_task_sha256": hashlib.sha256(original_task.encode()).hexdigest(),
                "s0_identity": snapshot_identity,
                "snapshot_manifest_hash": context.snapshot.manifest_hash,
                "native_runtime_evidence": runner.runtime_evidence,
                "implementation": {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in code_paths},
                "wrapper_sha256": hashlib.sha256(Path(inspect.getfile(NativeStageRunner)).read_bytes()).hexdigest(),
            },
            max_bytes=scaled(16 * 1024 * 1024),
        )
        verification = importlib.import_module(self._types.__package__ + ".verification").PublicVerification
        store = importlib.import_module(self._types.__package__ + ".public_checkpoint").PublicCriterionStore
        # Match the pinned native producer's supported actual identity: binary
        # and config bytes always bind, while source_commit is optional.
        actual_worker_identity = {
            "worker_type": type(self._worker).__module__ + "." + type(self._worker).__qualname__,
            "binary_sha256": self._side_binding.summary["binary_sha256"],
            "config_template_sha256": self._side_binding.summary["config_template_sha256"],
        }
        source_commit = self._side_binding.summary.get("source_commit")
        if source_commit:
            actual_worker_identity["source_commit"] = source_commit
        wrapped = NativeStageRunner(
            runner=runner,
            checkpoint=checkpoint,
            context=context,
            types=self._types,
            verification=verification,
            store_type=store,
            validate_pair=self._service_module.validate_pact_runtime_input,
            stopped=self.artifacts.stopped,
            exhausted=self.artifacts.exhausted,
            abort=self._side_context["abort"],
            snapshot_identity=snapshot_identity,
            expected_worker_identity={
                "declared": {"scope": "own-work-stages/v1", "work_scope": str(context.session_dir.absolute())},
                "actual": actual_worker_identity,
                "read_only": True,
                "investigation_tool_policy": "configured",
                "synthesis_tool_policy": "none",
            },
            # Only the real Jiuwen suite cache path with generated public
            # checks opts in. Explicit --verify and non-frozen profiles keep
            # the historical serial runner.
            public_runner_factory=(
                (lambda: self.default_runner(context))
                if use_cache and self.public_criterion_concurrency > 1 and self.requires_complete_public_review
                else None
            ),
            public_concurrency=(
                self.public_criterion_concurrency
                if use_cache and self.requires_complete_public_review
                else 1
            ),
        )
        key = (str(context.session_dir.absolute()), checkpoint.binding)
        if key in self._planning_call_managers:
            wrapped.calls = self._planning_call_managers[key]
        else:
            self._planning_call_managers[key] = wrapped.calls
        return wrapped

    async def control_planning_call(self, command):
        matches = [
            manager
            for manager in self._planning_call_managers.values()
            if manager.binding == command["binding"] and command["call_id"] in manager.active
        ]
        if len(matches) != 1:
            raise PlanningCheckpointError("planning call identity is absent or ambiguous")
        return await matches[0].control(command)

    async def frozen_input_options(self, runner, original_task):
        from .native_checkpoint import NativeStageRunner

        if (
            isinstance(runner, NativeStageRunner)
            and self._types.__package__ == "jiuwenswarm.long_horizon.clarification"
        ):
            return {"base_evidence": await runner.frozen_base_evidence(original_task)}
        return {}

    def degraded_clarification(self, reason: str):
        return self._types.ClarificationResult(
            strategy_version=self._service_module.STRATEGY_VERSION,
            revision=1,
            delta="",
            selection=self._types.ClarificationSelection(),
            status="degraded",
            empty_reason="clarification_failed",
            warnings=("bounded planning: " + reason,),
        )

    async def generate_pact_input(
        self, context: ClarificationContext, result: Any, original_task: str, *, hints: str
    ) -> dict[str, Any]:
        # Call-local progress only; a previous attempt or trial cannot leak a
        # review-started/partial marker into this preparation.
        clear_public_review()
        wants_verification = (
            self._side_args.verify_command is None and self.verification_strategy == "frozen-public-checks"
        )
        cap = None
        if wants_verification:
            cap = PublicRepairCap(context.admission, self.artifacts.exhausted, attempts=PUBLIC_VERIFICATION_ATTEMPTS)
            context = replace(context, admission=cap)
        runner = await self.planning_runner(context, original_task)
        self._active_clarification = asyncio.current_task()
        try:
            input_options = await self.frozen_input_options(runner, original_task)
            revision = self._revision(number=1, messages=(original_task,))
            # The Goal Contract and Initial Plan are generated and validated
            # first; public verification is a separate optional stage below.
            pair, _usage = await self._service(runner).generate_pact_input(
                result=result,
                revision=revision,
                background="",
                snapshot=context.snapshot,
                localization_hints=hints,
                **input_options,
                generate_public_verification=False,
            )
            payload = {
                "goal_contract": pair.goal_contract.model_dump(mode="json", by_alias=True),
                "initial_plan": pair.initial_plan.model_dump(mode="json", by_alias=True),
            }
            if wants_verification:
                payload["public_verification"] = await self._bounded_public_verification(
                    runner, context, cap, revision, result, pair
                )
            elif self.verification_strategy == "candidate-review":
                payload["public_verification"] = {
                    "status": "unavailable",
                    "reason": "Deferred until P1 exists: independent Reviewer must propose public behavioral "
                    "checks against the actual candidate. Process obligations need trajectory evidence; "
                    "unobservable obligations stay explicitly unverified. Only Harbor supplies an independent score.",
                    "checks": [],
                }
            return payload
        finally:
            self._active_clarification = None

    def _integrity_errors(self) -> tuple[type[BaseException], ...]:
        errors: list[type[BaseException]] = [
            SideCallIntegrityError,
            NonDegradableIntegrityError,
            self.artifacts.stopped,
        ]
        native = getattr(  # Native package boundary: not every agent defines it.
            importlib.import_module(self._types.__package__ + ".budget"), "SideCallIntegrityError", None
        )
        if isinstance(native, type):
            errors.append(native)
        return tuple(errors)

    def _review_progress(self, runner, prompt, criteria, base, *, entered: bool) -> PublicReviewProgress:
        completed: tuple[str, ...] = ()
        probe = getattr(runner, "public_review_progress", None)
        if callable(probe):
            try:
                completed = tuple(probe(prompt, criteria))
            except PlanningCheckpointError:
                raise
            except (OSError, ValueError, TypeError):
                completed = ()
        if not completed:
            completed = tuple(getattr(runner, "public_review_completed", ()) or ())
        return replace(base, started=entered or bool(completed), completed_criteria=completed)

    def _recoverable_public_review(
        self,
        runner,
        context: ClarificationContext,
        prompt: str,
        criteria: tuple,
        base: PublicReviewProgress,
        *,
        entered: bool,
        reason: str,
        detail: str,
        degradation: dict[str, Any],
    ) -> None:
        progress = self._review_progress(runner, prompt, criteria, base, entered=entered)
        progress = replace(
            progress,
            reason=reason,
            detail=detail,
            refused=bool(getattr(context.admission, "refused", None)),
            degradation=degradation,
        )
        set_public_review(progress)
        self.public_verification_degradation = degradation
        raise recoverable_public_review_error(self.artifacts.exhausted, progress)

    async def _bounded_public_verification(
        self, runner: Any, context: ClarificationContext, cap: PublicRepairCap, revision: Any, result: Any, pair: Any
    ) -> dict[str, Any]:
        """Generate public checks, or record why they are unavailable; never invent one."""
        criteria = tuple(pair.goal_contract.acceptance_criteria)
        # Both native generation and independent review receive this context.
        prompt = (
            "Original requirement:\n"
            + revision.rendered
            + "\n\nValidated clarification:\n"
            + result.delta
            + "\n\nGoal Contract:\n"
            + pair.goal_contract.model_dump_json(by_alias=True)
            + "\n\nInitial Plan:\n"
            + pair.initial_plan.model_dump_json(by_alias=True)
            + "\n\nFor process criteria, verify the actions and timing the public requirement specifies. "
            "A PACT checkpoint can commit several successive edits together. A shared commit leaves their "
            "relative order unknown. Require separate commits only when the public requirement explicitly "
            "requires them. For a required sequence of actions, use ordered public evidence available to the "
            "check at execution, such as a public activity log recording both actions. Document layout alone "
            "does not establish when the actions occurred. If the requirement only asks for a record, check "
            "its contents. If a required sequence cannot be established, report unavailable and identify "
            "the missing evidence."
            + CONFIRMED_BEHAVIOR_CHECK
        )
        remaining = None
        if context.deadline is not None:
            remaining = context.deadline - time.time() - PUBLIC_VERIFICATION_DEADLINE_MARGIN_SECONDS
        # A parallel native full review is all-or-nothing. It may degrade to an
        # explicit unavailable result only for a serial/optional verifier.
        complete_review_required = (
            self.requires_complete_public_review
            and getattr(runner, "public_concurrency", 1) > 1
            and getattr(runner, "public_runner_factory", None) is not None
        )
        base = PublicReviewProgress(
            started=False,
            deadline_unix=context.deadline,
            total_seconds=getattr(self._side_args, "budget_seconds", None),
            required_criteria=tuple(criterion.id for criterion in criteria),
        )
        if complete_review_required:
            set_public_review(base)
        reason, detail = "", ""
        transport_stop = None
        model_output_stop = None
        if remaining is not None and remaining <= 0:
            reason = "no_time_before_planning_deadline"
        else:
            try:
                if complete_review_required:
                    set_public_review(replace(base, started=True))
                async with asyncio.timeout(remaining):
                    verification, _usage = await runner.generate_public_verification(
                        prompt, acceptance_criteria=criteria
                    )
                verification.validate_coverage({criterion.id for criterion in criteria})
                payload = verification.model_dump(mode="json", by_alias=True)
                if complete_review_required:
                    set_public_review(
                        replace(
                            base,
                            started=True,
                            completed_criteria=tuple(criterion.id for criterion in criteria),
                        )
                    )
                    payload["requires_complete_review"] = True
                return payload
            except asyncio.CancelledError:
                if complete_review_required:
                    # An outer planning timeout arrives as cancellation. Keep
                    # the call-local progress so the suite can tell this apart
                    # from a timeout before the review stage was entered.
                    set_public_review(self._review_progress(runner, prompt, criteria, base, entered=True))
                raise
            except NativeArtifactInvalid as exc:
                reason, detail = "invalid_public_verification_artifact", str(exc)
            except PlanningCheckpointError:
                raise
            except Exception as exc:
                if isinstance(exc, self._integrity_errors()):
                    raise
                detail = f"{type(exc).__name__}: {exc}"
                if capacity_os_error(exc) is not None:
                    reason = "storage_capacity"
                elif isinstance(exc, self._public_evidence_unavailable):
                    reason = "public_evidence_unavailable"
                elif cap.capped:
                    reason = "repair_cap_reached"
                elif isinstance(exc, TimeoutError):
                    reason = "timeout_before_planning_deadline"
                elif isinstance(exc, OSError):
                    # Permission, path and other filesystem integrity failures
                    # are not volume capacity and must stay fail-closed.
                    raise PlanningCheckpointError("planning checkpoint storage failed") from exc
                elif isinstance(exc, self.artifacts.exhausted):
                    reason = (
                        "trial_budget_refused"
                        if getattr(context.admission, "refused", None)
                        else "native_repair_exhausted"
                    )
                elif isinstance(stop := getattr(exc, "public_model_output_stop", None), PublicModelOutputStop):
                    model_output_stop = stop
                    reason = stop.failure["reason"]
                    detail = f"{stop.failure['stage']}: {stop.failure['field']} repeated {stop.failure['repeat_count']} times"
                elif isinstance(stop := getattr(exc, "public_transport_stop", None), PublicTransportStop):
                    transport_stop = stop
                    reason = stop.reason
                    terminal = stop.retry_failure or stop.parent
                    detail = f"{terminal['stage']}: {terminal['reason']}"
                else:
                    reason = "invalid_public_verification_artifact"
        degradation = {
            "reason": reason,
            "detail": detail[:1000],
            "capped_stages": list(cap.capped),
            "attempts_per_stage": PUBLIC_VERIFICATION_ATTEMPTS,
        }
        if model_output_stop is not None:
            degradation["model_output"] = asdict(model_output_stop)
        if transport_stop is not None:
            degradation["transport"] = asdict(transport_stop)
        if complete_review_required:
            self._recoverable_public_review(
                runner,
                context,
                prompt,
                criteria,
                base,
                entered=reason != "no_time_before_planning_deadline",
                reason=reason,
                detail=detail,
                degradation=degradation,
            )
        self.public_verification_degradation = degradation
        # Same contract as a profile without a frozen public-check command:
        # no generated checks are registered; Goal Contract and Plan proceed.
        payload = {
            "status": "unavailable",
            "reason": "Generated public verification unavailable (" + reason + "); continuing with the validated "
            "Goal Contract and Initial Plan. No public check was generated or claimed.",
            "checks": [],
            "degradation": self.public_verification_degradation,
        }
        if complete_review_required:
            payload["requires_complete_review"] = True
        return payload

    async def clarify(self, context: ClarificationContext, original_task: str) -> Any:
        runner = await self.planning_runner(context, original_task)
        self._active_clarification = asyncio.current_task()
        try:

            async def invoke():
                input_options = await self.frozen_input_options(runner, original_task)
                value = await self._service(runner).clarify(
                    revision=self._revision(number=1, messages=(original_task,)),
                    background="",
                    snapshot=context.snapshot,
                    **input_options,
                )
                return value, {"native_usage": list(value.usage_details)}

            from .native_checkpoint import NativeStageRunner

            if not isinstance(runner, NativeStageRunner):
                return (await invoke())[0]

            def valid(value):
                service = self._service_module
                if value.status != "completed" or len(value.proposals) < service.MIN_VALID_PROPOSALS:
                    raise ValueError("clarification did not pass the native service")
                indices = value.proposal_sample_indices
                if len(indices) != len(value.proposals) or len(set(indices)) != len(indices):
                    raise ValueError("proposal provenance indices invalid")
                investigations = {row.sample_index: row for row in value.investigations}
                if len(investigations) != len(value.investigations):
                    raise ValueError("duplicate proposal investigation")
                for index in indices:
                    row = investigations.get(index)
                    if (
                        row is None
                        or row.status != "completed"
                        or row.coverage_status != "sufficient"
                        or row.coverage_errors
                        or row.validation_errors
                    ):
                        raise ValueError("clarification contains an unvalidated proposal")

                candidates = service._candidate_map(value.proposals)
                if candidates:
                    decision = value.raw_selection
                    if not isinstance(decision, self._types.ClarificationSelectorDecision) or service._selector_errors(
                        decision, candidates
                    ):
                        raise ValueError("clarification lacks a validated selector decision")
                    selected = service._materialize_selection(decision, candidates)
                    expected = service.sanitize_selection(original_task, selected)
                else:
                    if value.raw_selection is not None:
                        raise ValueError("unexpected selector decision for an empty candidate set")
                    expected = self._types.ClarificationSelection()
                if value.selection != expected or value.delta != service.render_delta(expected):
                    raise ValueError("clarification result does not match the validated selection")

            value, usage = await runner.cached(
                "completed-clarification", original_task, self._types.ClarificationResult, invoke, valid
            )
            return value if usage else replace(value, usage_details=(), elapsed_seconds=0.0)
        finally:
            self._active_clarification = None

    async def cancel(self) -> None:
        task = self._active_clarification
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        await self._cancel_side_calls()

    def default_runner(self, context: ClarificationContext) -> Any:
        raise NotImplementedError
