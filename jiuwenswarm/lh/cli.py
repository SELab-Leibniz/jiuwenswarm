"""``--lh`` entry: run the shared long-horizon controller on the native Jiuwen worker.

``jiuwenswarm --lh ...`` and ``jiuwenswarm-process --lh ...`` both dispatch here
before their normal argument parsing, so every invocation without ``--lh`` keeps
the original JiuwenSwarm behavior.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import shlex
import shutil
import signal
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

LH_FLAG = "--lh"


def split_lh_flag(argv: list[str]) -> tuple[bool, list[str]]:
    """Return whether ``--lh`` was given and the remaining arguments."""
    if LH_FLAG not in argv:
        return False, list(argv)
    return True, [value for value in argv if value != LH_FLAG]


def build_parser(prog: str = "jiuwenswarm --lh") -> argparse.ArgumentParser:
    from .snapshot_resources import DEFAULT_SNAPSHOT_MAX_TOTAL_BYTES

    parser = argparse.ArgumentParser(
        prog=prog,
        description="Run one long-horizon task with the native JiuwenSwarm worker and publish selection.json",
    )
    parser.add_argument("prompt", nargs="?", help="Task text; alternatively use --task FILE")
    parser.add_argument("--task", type=Path, help="Task description file")
    parser.add_argument("--workdir", "--cwd", dest="workdir", type=Path, help="Git repository to work on; default: .")
    parser.add_argument("--output", type=Path, help="Fresh output directory outside the workdir")
    parser.add_argument("--task-id", help="Task identity recorded in the output; default derived from the time")
    parser.add_argument("--trial-id", default="1")
    parser.add_argument(
        "--workflow",
        choices=("suite", "benchmark"),
        default="suite",
        help="suite restores early long-horizon capabilities; benchmark retains the prior protocol",
    )
    parser.add_argument(
        "--memgraph-command",
        help="MemGraph (default off): trusted read-only command, JSON query on stdin, {text: str} on stdout",
    )
    parser.add_argument("--memgraph-receipt", type=Path, help="Frozen corpus receipt required with --memgraph-command")
    parser.add_argument("--budget-seconds", "--timeout", dest="budget_seconds", type=float)
    parser.add_argument("--budget-started-at-unix", type=float)
    parser.add_argument("--finish-reserve-seconds", type=float)
    parser.add_argument("--cutoff-at-unix", type=float)
    parser.add_argument("--transport-recovery-attempts", type=int, default=3)
    parser.add_argument("--reviewer-uid", type=int)
    parser.add_argument("--reviewer-gid", type=int)
    parser.add_argument("--review-diagnosis-limit", type=int)
    parser.add_argument("--review-acceptance-limit", type=int)
    parser.add_argument("--early-audit-max-rounds", type=int, default=2)
    parser.add_argument("--early-audit-min-remaining-ratio", type=float, default=0.40)
    parser.add_argument("--early-audit-min-remaining-seconds", type=float, default=2700.0)
    parser.add_argument("--early-audit-max-seconds", type=float)
    parser.add_argument("--allow-user-interaction", action="store_true")
    parser.add_argument("--clarification-max-rounds", type=int, default=3)
    parser.add_argument("--clarification-budget-ratio", type=float, default=0.20)
    parser.add_argument(
        "--control-profile",
        choices=("legacy", "bounded"),
        default="legacy",
        help="bounded reserves suite phases and exports only a supplied-public-check-passed candidate",
    )
    parser.add_argument(
        "--native-transport-recovery",
        action="store_true",
        help="Allow one evidence-bound fresh Jiuwen Worker call within the original trial budget; default off",
    )
    parser.add_argument("--snapshot-max-total-bytes", type=int, default=DEFAULT_SNAPSHOT_MAX_TOTAL_BYTES)
    parser.add_argument("--max-role-calls", type=int)
    turns = parser.add_mutually_exclusive_group()
    turns.add_argument("--max-turns", type=int)
    turns.add_argument(
        "--deadline-only-turns",
        action="store_true",
        help="Use the explicit original suite task budget without an implementation-turn count cap",
    )
    parser.add_argument("--turn-timeout", type=float)
    parser.add_argument("--review-interval", type=int, default=2)
    parser.add_argument("--review-each-round", action="store_true")
    parser.add_argument("--window-seconds", type=float)
    parser.add_argument("--verify", help="Public check command run by PACT Core")
    parser.add_argument("--agent", default="Code", help="Native role profile")
    parser.add_argument("--model", default="bench")
    parser.add_argument("--worktree-root", type=Path)
    parser.add_argument("--work-id")
    parser.add_argument("--expected-revision", type=int)
    parser.add_argument("--worker-bin", type=Path, help="Native machine interface; default: jiuwenswarm-process")
    parser.add_argument("--config-template", type=Path, help="Credential-free config.yaml; default: active config")
    return parser


def _default_worker_bin() -> Path:
    sibling = Path(sys.executable).with_name("jiuwenswarm-process")
    if sibling.is_file():
        return sibling
    found = shutil.which("jiuwenswarm-process")
    if found is None:
        raise ValueError("jiuwenswarm-process was not found; pass --worker-bin")
    return Path(found)


def _default_config_template() -> Path:
    from jiuwenswarm.common.utils import get_config_file

    return Path(get_config_file())


def _complete_options(options: argparse.Namespace) -> argparse.Namespace:
    if (options.prompt is None) == (options.task is None):
        raise ValueError("--lh requires exactly one of a task prompt or --task FILE")
    if bool(options.memgraph_command) != bool(options.memgraph_receipt):
        raise ValueError("MemGraph command requires a frozen corpus receipt, and vice versa")
    options.workdir = (options.workdir or Path.cwd()).expanduser().resolve(strict=True)
    stamp = time.strftime("%Y%m%dT%H%M%S")
    options.task_id = options.task_id or f"lh-{stamp}"
    if options.output is None:
        options.output = Path(tempfile.gettempdir()) / "jiuwenswarm-lh" / f"{options.task_id}-{options.trial_id}"
    if options.task is None:
        options.output.parent.mkdir(parents=True, exist_ok=True)
        options.task = options.output.parent / f"{options.output.name}.task.md"
        options.task.write_text(options.prompt, encoding="utf-8")
    options.worker_bin = options.worker_bin or _default_worker_bin()
    options.config_template = options.config_template or _default_config_template()
    return options


def arguments_from_namespace(options: argparse.Namespace) -> Any:
    from .controller import BenchmarkArguments

    suite = options.workflow == "suite"
    deadline_only = options.deadline_only_turns
    if deadline_only:
        if not suite or options.budget_seconds is None or options.budget_started_at_unix is None:
            raise ValueError("deadline-only turns require suite and explicit original task budget and origin")
        if options.turn_timeout is not None and options.turn_timeout != options.budget_seconds:
            raise ValueError("deadline-only role timeout must equal the original full task budget")
    return BenchmarkArguments(
        task=options.task,
        workdir=options.workdir,
        output=options.output,
        task_id=options.task_id,
        trial_id=options.trial_id,
        workflow=options.workflow,
        control_profile=options.control_profile,
        snapshot_max_total_bytes=options.snapshot_max_total_bytes,
        budget_seconds=options.budget_seconds if options.budget_seconds is not None else 172800.0 if suite else 3600.0,
        budget_started_at_unix=options.budget_started_at_unix,
        finish_reserve_override_seconds=options.finish_reserve_seconds,
        cutoff_at_unix=options.cutoff_at_unix,
        transport_recovery_attempts=options.transport_recovery_attempts,
        reviewer_uid=options.reviewer_uid,
        reviewer_gid=options.reviewer_gid,
        review_diagnosis_limit=options.review_diagnosis_limit,
        review_acceptance_limit=options.review_acceptance_limit,
        early_audit_max_rounds=options.early_audit_max_rounds,
        early_audit_min_remaining_ratio=options.early_audit_min_remaining_ratio,
        early_audit_min_remaining_seconds=options.early_audit_min_remaining_seconds,
        early_audit_max_seconds=options.early_audit_max_seconds,
        allow_user_interaction=options.allow_user_interaction,
        clarification_max_rounds=options.clarification_max_rounds,
        clarification_budget_ratio=options.clarification_budget_ratio,
        max_role_calls=options.max_role_calls if options.max_role_calls is not None else None if suite else 32,
        max_turns=None if deadline_only else options.max_turns if options.max_turns is not None else 64 if suite else 3,
        deadline_only_budget_seconds=options.budget_seconds if deadline_only else None,
        turn_timeout_seconds=(
            options.budget_seconds
            if deadline_only
            else options.turn_timeout
            if options.turn_timeout is not None
            else 21600.0
            if suite
            else 3600.0
        ),
        review_interval=options.review_interval,
        review_each_round=options.review_each_round,
        window_seconds=options.window_seconds,
        verify_command=options.verify.strip() if options.verify is not None else None,
        profile_name=options.agent,
        model=options.model or None,
        worktree_root=options.worktree_root,
        work_id=options.work_id,
        expected_revision=options.expected_revision,
    )


def load_plugin(options: argparse.Namespace) -> Any:
    from jiuwenswarm.long_horizon.worker import JiuwenWorker

    from .plugins.jiuwen import JiuwenPlugin

    args = arguments_from_namespace(options)
    worker = JiuwenWorker(
        options.worker_bin,
        options.config_template,
        source_commit=os.environ.get("JIUWENSWARM_SUITE_COMMIT", ""),
        default_timeout_seconds=args.turn_timeout_seconds,
    )
    return JiuwenPlugin(worker=worker, transport_recovery=options.native_transport_recovery)


async def run_command(options: argparse.Namespace) -> int:
    outcome = await run_controller(options)
    sys.stdout.write(json.dumps(asdict(outcome), default=str) + "\n")
    return outcome_exit_code(outcome)


def outcome_exit_code(outcome: Any) -> int:
    return (
        1
        if outcome.selection is None
        else 3
        if outcome.internal_failure
        else 4
        if not outcome.candidate_available
        else 0
    )


async def run_controller(options: argparse.Namespace) -> Any:
    """Run one controller invocation and return its ``BenchmarkOutcome``."""
    from .controller import LongHorizonBenchmark

    if bool(options.memgraph_command) != bool(options.memgraph_receipt):
        raise ValueError("MemGraph command requires a frozen corpus receipt, and vice versa")
    args = arguments_from_namespace(options)
    plugin = load_plugin(options)
    loop = asyncio.get_running_loop()
    installed = []
    try:
        if options.memgraph_command:
            from .memory import CommandMemory

            memory = CommandMemory(shlex.split(options.memgraph_command), options.memgraph_receipt)
            plugin.set_memory_provider(memory, identity=memory.identity)
        controller = LongHorizonBenchmark(args, plugin=plugin)
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.add_signal_handler(sig, lambda: loop.create_task(controller.cancel()))
                installed.append(sig)
        try:
            outcome = await controller.run()
        except asyncio.CancelledError:
            await controller.cancel()
            raise
    finally:
        for sig in installed:
            loop.remove_signal_handler(sig)
        close = getattr(plugin, "aclose", None)
        if close is not None:
            await close()
    return outcome


def main(argv: list[str] | None = None, *, prog: str = "jiuwenswarm --lh") -> int:
    parser = build_parser(prog)
    options = parser.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        return asyncio.run(run_command(_complete_options(options)))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1
