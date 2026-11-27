"""Default workflow CLI; Campaign is a compatibility name for durable work."""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from uuid import uuid4
from pathlib import Path

from .adapters import SUPPORTED_ADAPTERS, create_adapter
from .artifacts import read_json
from .runtime.control_plane import WorkRuntime
from .runtime.configuration import DriverConfiguration
from .runtime.driver import DefaultDriver
from .runtime.dashboard import serve_dashboard
from .runtime.planning import initial_plan_payload, validate_initial_plan
from .runtime.schemas import RuntimeContractError, goal_contract_criteria, validate_goal_contract
from .runtime.text_plan import read_text_plan
from .runtime.types import AcceptancePolicy, WorkSnapshot



def add_campaign_commands(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    campaign = subparsers.add_parser("campaign", help="create, advance and inspect work-centric Campaigns")
    commands = campaign.add_subparsers(dest="campaign_command")
    for name in ("create", "run", "advance", "recover", "status", "pause", "resume", "revise", "reset-operation", "request-review", "adopt"):
        command = commands.add_parser(name)
        _add_campaign_arguments(command, name)
    dashboard = commands.add_parser("dashboard", help="serve the read-only Dashboard on loopback")
    dashboard.add_argument("--workspace", type=Path, default=Path.cwd())
    dashboard.add_argument("--campaign", default="latest")
    dashboard.add_argument("--port", type=int, default=8765)


def add_run_arguments(command: argparse.ArgumentParser) -> None:
    """Top-level run and the compatibility alias have exactly the same options."""
    _add_campaign_arguments(command, "run")


def _add_campaign_arguments(command: argparse.ArgumentParser, name: str) -> None:
    command.set_defaults(campaign_command=name)
    if name == "run":
        command.description = "Run or continue accepted work with continuous execution by default."
    command.add_argument("--workspace", type=Path, default=Path.cwd())
    if name in {"run", "create"}:
        command.add_argument("--profile", choices=("continuous", "benchmark"),
                             help="optional built-in workflow profile; default is continuous")
        command.add_argument("--benchmark-config", type=Path, help="fixed-task benchmark JSON configuration")
        command.add_argument("--campaign-id", help="identifier for newly created work")
        command.add_argument("--request-id", help="stable caller request identity; duplicate starts reuse existing work")
        command.add_argument("--loop-id", help=argparse.SUPPRESS)
        command.add_argument("--contract", type=Path, help="Goal Contract JSON for a structured Initial Plan")
        command.add_argument("--plan", type=Path, help="accepted text plan, or Initial Plan JSON with --contract")
        command.add_argument("--verify", metavar="CMD", help="agent-visible verification command (exit 0 = passed)")
        command.add_argument("--closeout-verify", metavar="CMD", help="global public verification; defaults to --verify")
        command.add_argument("--final-verify", metavar="CMD", help="opaque validation during final full-work acceptance")
        command.add_argument("--max-rounds", type=int, help="Worker turns per invocation before yielding (default: 3)")
        command.add_argument("--review-interval", type=int, help="optional independent review milestone every N Worker turns")
        command.add_argument("--max-unchanged-rounds", type=int, help="yield after N unchanged draft observations (default: 2)")
        command.add_argument("--review-each-round", action="store_true", default=None,
                             help="opt into independent review after every Worker round")
        command.add_argument("--execution-policy", choices=("continuous", "review_each_round"),
                             help=argparse.SUPPRESS)
        command.add_argument("--allow-unverified", action="store_true", default=None)
        for flag in ("verify-timeout", "final-verify-timeout", "turn-timeout"):
            command.add_argument(f"--{flag}", type=float)
    if name != "create":
        command.add_argument("--campaign", required=name != "run", help="identifier of existing work")
    if name in {"run", "advance", "recover"}:
        command.add_argument("--worker-adapter", required=True,
                             help=f"worker adapter ({', '.join(SUPPORTED_ADAPTERS)})")
        command.add_argument("--reviewer-adapter", required=True,
                             help=f"reviewer adapter ({', '.join(SUPPORTED_ADAPTERS)})")
        command.add_argument("--planner-adapter", choices=SUPPORTED_ADAPTERS)
        command.add_argument("--manager-adapter", choices=SUPPORTED_ADAPTERS)
        command.add_argument("--worktree-root", type=Path)
    elif name == "create":
        # Accepted for symmetry with run, but create does not invoke adapters.
        command.add_argument("--worker-adapter", choices=SUPPORTED_ADAPTERS, default="fake")
        command.add_argument("--reviewer-adapter", choices=SUPPORTED_ADAPTERS, default="fake")
        command.add_argument("--worktree-root", type=Path)
    if name not in {"create", "status"}:
        command.add_argument("--expected-revision", type=int,
                             required=name in {"pause", "resume", "revise", "reset-operation", "adopt", "recover", "request-review"})
    if name in {"revise", "adopt", "reset-operation"}:
        command.add_argument("--reason", required=True)
    if name == "reset-operation":
        command.add_argument("--executor-stopped", action="store_true", required=True)
    if name == "adopt":
        command.add_argument("--executor-stopped", action="store_true")
    if name == "request-review":
        command.add_argument("--reason", default="user_requested_review")
    if name == "revise":
        command.add_argument("--contract", type=Path)
        command.add_argument("--configuration", type=Path, help="JSON with complete policy and driver configuration objects")
        command.add_argument("--constraints", type=Path, help="JSON array of User-owned Plan constraints")


_CONFIG_FLAGS = {
    "verify": "verify_command", "closeout_verify": "closeout_verify_command",
    "final_verify": "final_verify_command", "max_rounds": "max_rounds",
    "allow_unverified": "allow_unverified", "verify_timeout": "verify_timeout_seconds",
    "final_verify_timeout": "final_verify_timeout_seconds", "turn_timeout": "turn_timeout_seconds",
    "execution_policy": "execution_policy",
    "review_interval": "review_interval", "max_unchanged_rounds": "max_unchanged_rounds",
}


def run_campaign_command(args: argparse.Namespace) -> int:
    action = args.campaign_command
    if action == "dashboard":
        serve_dashboard(args.workspace, campaign=args.campaign, port=args.port)
        return 0
    if getattr(args, "planner_adapter", None) or getattr(args, "manager_adapter", None):
        raise RuntimeContractError("Planner/Manager adapters are no longer part of the default workflow")
    benchmark = getattr(args, 'profile', None) == 'benchmark'
    if action in {'run', 'advance', 'recover'} and getattr(args, 'campaign', None):
        existing = WorkRuntime(args.workspace).read_work(args.campaign)
        if benchmark and existing.advisory.get('profile') != 'benchmark':
            raise RuntimeContractError('existing work has no benchmark trial configuration; use the Python BenchDriver for explicit handoff')
        benchmark = existing.advisory.get('profile') == 'benchmark'
        if benchmark and getattr(args, 'profile', None) == 'continuous':
            raise RuntimeContractError('benchmark continuation uses its persisted profile')
    if benchmark:
        from .benchmark.cli import run_benchmark_command
        return run_benchmark_command(args)
    if getattr(args, 'benchmark_config', None):
        raise RuntimeContractError('--benchmark-config requires --profile benchmark')
    runtime = WorkRuntime(args.workspace, worktree_root=getattr(args, "worktree_root", None))
    if action == "status":
        print(json.dumps(asdict(runtime.read_work(args.campaign)), ensure_ascii=False, indent=2, default=str))
        return 0
    if action in {"pause", "resume", "reset-operation", "request-review", "adopt", "revise"}:
        _print_result(_lifecycle(runtime, args))
        return 0
    if action in {"run", "create"}:
        if args.review_each_round:
            if args.execution_policy not in {None, "review_each_round"}:
                raise RuntimeContractError("conflicting review policy options")
            args.execution_policy = "review_each_round"
        if args.loop_id is not None:
            if args.campaign_id not in {None, args.loop_id}:
                raise RuntimeContractError("conflicting --campaign-id and --loop-id")
            args.campaign_id = args.loop_id
    continuing = action in {"advance", "recover"} or (action == "run" and args.campaign is not None)
    if continuing:
        if action == "run" and (args.contract or args.plan or args.campaign_id or args.request_id or
                any(getattr(args, flag) is not None for flag in _CONFIG_FLAGS)):
            raise RuntimeContractError("continuation uses persisted configuration; use campaign revise to change it")
        state = runtime.read_work(args.campaign)
        driver = DriverConfiguration.parse(state.advisory.get("driver_configuration", DriverConfiguration().payload()))
    else:
        if args.plan is None:
            raise RuntimeContractError("work creation requires --plan (or --campaign to resume)")
        workspace = args.workspace.expanduser().resolve()
        for flag in ("plan", "contract"):
            path = getattr(args, flag)
            if path is not None:
                path = path.expanduser()
                setattr(args, flag, path if path.is_absolute() else workspace / path)
        if action == "run" and (args.verify is None or not args.verify.strip()) and not args.allow_unverified:
            raise RuntimeContractError("no verification command; provide --verify or explicit --allow-unverified")
        for flag in ("verify", "closeout_verify", "final_verify"):
            value = getattr(args, flag)
            if value is not None and not value.strip():
                setattr(args, flag, None)
        policy_payload = AcceptancePolicy().payload()
        driver_payload = DriverConfiguration().payload()
        for flag, name in _CONFIG_FLAGS.items():
            value = getattr(args, flag)
            if value is not None:
                (policy_payload if name in policy_payload else driver_payload)[name] = value
        policy = AcceptancePolicy.parse(policy_payload)
        driver = DriverConfiguration.parse(driver_payload)
        if args.contract is None:
            contract, plan = read_text_plan(args.plan, workspace=workspace)
        else:
            contract = validate_goal_contract(read_json(args.contract))
            try:
                plan_payload = read_json(args.plan)
            except (OSError, ValueError) as exc:
                raise RuntimeContractError("Initial Plan is not valid JSON") from exc
            plan = validate_initial_plan(plan_payload,
                acceptance_criterion_ids=tuple(item["id"] for item in goal_contract_criteria(contract)))
        advisory: dict[str, object] = {"plan": initial_plan_payload(plan), "driver_configuration": driver.payload()}
    # Registry validation precedes creation; create never invokes a role.
    worker = create_adapter("fake" if action == "create" else args.worker_adapter,
                            role="worker", max_rounds=driver.max_rounds + 1)
    reviewer = create_adapter("fake" if action == "create" else args.reviewer_adapter,
                              role="reviewer", max_rounds=driver.max_rounds + 2)
    if not continuing:
        state = runtime.create_work(request_id=args.request_id or args.campaign_id or uuid4().hex,
            work_id=args.campaign_id, contract=contract, policy=policy, advisory=advisory,
            constraints=plan.constraints)
    if action == "recover":
        state = runtime.recover(state.work_id, expected_revision=args.expected_revision)
    elif action != "create":
        workflow = DefaultDriver(runtime, worker, reviewer)
        operation = workflow.advance if action == "advance" else workflow.run
        state = operation(state.work_id, expected_revision=args.expected_revision)
    _print_result(state)
    return 0 if action in {"create", "advance", "recover"} or state.status == "completed" else 1


def _lifecycle(runtime: WorkRuntime, args: argparse.Namespace) -> WorkSnapshot:
    action = args.campaign_command
    if action == "revise":
        constraints = None
        if args.constraints is not None:
            raw = json.loads(args.constraints.read_text(encoding="utf-8"))
            if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
                raise RuntimeContractError("constraints file must contain an array of strings")
            constraints = tuple(raw)
        policy = None
        driver = None
        if args.configuration:
            payload = read_json(args.configuration)
            if set(payload) != {"policy", "driver"}:
                raise RuntimeContractError("configuration requires policy and driver objects")
            policy = AcceptancePolicy.parse(payload["policy"])
            driver = DriverConfiguration.parse(payload["driver"])
        state = runtime.read_work(args.campaign)
        contract = read_json(args.contract) if args.contract else None
        authority_changed = ((contract is not None and contract != state.contract)
            or (policy is not None and policy != state.policy)
            or (constraints is not None and constraints != state.constraints))
        if authority_changed:
            state = runtime.revise_work(args.campaign, expected_revision=args.expected_revision, reason=args.reason,
                contract=contract, policy=policy, constraints=constraints)
        elif driver is None or driver.payload() == state.advisory.get("driver_configuration"):
            raise RuntimeContractError("revision must change requirements, acceptance policy or driver configuration")
        if driver is not None:
            state = runtime.update_advisory(state.work_id,
                expected_revision=state.revision if authority_changed else args.expected_revision,
                advisory={**state.advisory, "driver_configuration": driver.payload()})
        return state
    if action == "adopt":
        return runtime.adopt(args.campaign, expected_revision=args.expected_revision, reason=args.reason,
                             executor_stopped=args.executor_stopped)
    if action == "reset-operation":
        return runtime.reset_operation(args.campaign, expected_revision=args.expected_revision,
            executor_stopped=args.executor_stopped, reason=args.reason)
    if action == "request-review":
        return runtime.request_review(args.campaign, expected_revision=args.expected_revision, reason=args.reason)
    operation = runtime.pause if action == "pause" else runtime.resume
    return operation(args.campaign, expected_revision=args.expected_revision)


def _print_result(result: WorkSnapshot) -> None:
    progress = result.advisory.get("default_driver", {})
    reason = progress.get("yield_reason") if isinstance(progress, dict) else None
    next_action = "none" if result.status == "completed" else ("continue_work" if result.status == "active" else result.status)
    print(f"campaign:  {result.work_id}")
    print(f"status:    {result.status}")
    print(f"revision:  {result.revision}")
    print(f"next:      {next_action}")
    if reason:
        print(f"yield:     {reason}")
    print(f"candidate: {result.candidate.checkpoint}")
    print(f"promotion: {result.delivery.status if result.delivery else 'not_attempted'}")
    print(f"artifacts: {result.artifacts}")
