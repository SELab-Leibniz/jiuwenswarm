"""``jiuwenswarm-process --run-json FILE --lh [LH options]``.

The run JSON keeps its usual role (task text, workspace, timeout); the extra
CLI arguments after ``--lh`` configure the long-horizon controller. Output
follows the machine protocol: JSONL ``event`` records, then exactly one
``result`` record with the summed usage of every native worker call.

The controller works in a private copy of the workspace, so its ``.pact``
ledger and role worktrees never appear in the caller's repository. Only the
exported candidate patch is applied back to the original workspace.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from threading import Event
from typing import Any

from jiuwenswarm.channels.process_cli.machine_io import (
    MachineInputError,
    OneShotWriter,
    read_run_input,
)
from jiuwenswarm.channels.process_cli.protocol import (
    OneShotRunResult,
    RunStatus,
    RuntimeErrorInfo,
)

from .publication import apply_candidate, caller_tracked, git_env

LH_EVENT_ENABLED = "lh.enabled"
LH_EVENT_FINISHED = "lh.finished"
SUPPORTED_MODES = (None, "agent.code.normal")
# The run JSON already names the task and its workspace.
_RESERVED = ("prompt", "task", "workdir")
_USAGE_KEYS = ("input_tokens", "output_tokens", "total_tokens", "cache_tokens", "model_calls")


class LHArgumentError(MachineInputError):
    code = "INVALID_ARGUMENTS"


class LHCombinationError(MachineInputError):
    code = "UNSUPPORTED_COMBINATION"


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        raise LHArgumentError(f"--lh: {message}")


def find_run_json(argv: list[str]) -> tuple[str | None, list[str]]:
    """Split ``--run-json VALUE`` / ``--run-json=VALUE`` out of ``argv``."""
    rest: list[str] = []
    source: str | None = None
    index = 0
    while index < len(argv):
        value = argv[index]
        if value == "--run-json" and index + 1 < len(argv):
            source = argv[index + 1]
            index += 2
            continue
        if value.startswith("--run-json="):
            source = value.split("=", 1)[1]
        else:
            rest.append(value)
        index += 1
    return source, rest


def parse_lh_arguments(argv: list[str]) -> argparse.Namespace:
    from .cli import build_parser

    template = build_parser("jiuwenswarm-process --run-json FILE --lh")
    parser = _Parser(prog=template.prog, add_help=False)
    for action in template._actions:  # noqa: SLF001 - reuse the one LH option table
        if action.dest in ("help",):
            continue
        parser._add_action(action)  # noqa: SLF001
    options = parser.parse_args(argv)
    given = [name for name in _RESERVED if getattr(options, name) is not None]
    if given:
        raise LHArgumentError(
            "--run-json supplies the task and workspace; remove " + ", ".join(f"--{n}" for n in given)
        )
    return options


def _validate_run_input(run_input: Any) -> None:
    unsupported = {
        "mode": run_input.mode not in SUPPORTED_MODES,
        "session_id": run_input.session_id is not None,
        "model": run_input.model is not None,
        "output_schema": run_input.output_schema is not None,
        "host_tools": bool(run_input.host_tools),
    }
    names = [name for name, bad in unsupported.items() if bad]
    if names:
        raise LHCombinationError(
            "--lh supports only a new agent.code.normal run; unsupported fields: " + ", ".join(names),
            request_id=run_input.request_id,
        )


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "user.name=jiuwenswarm-lh", "-c", "user.email=lh@localhost", *args],
        cwd=cwd,
        check=check,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        env=git_env(),
        text=True,
    )


def prepare_arena(source: Path, arena: Path) -> Path:
    """Copy ``source`` without its ``.git`` into a fresh single-commit repository."""
    work = arena / "source"
    shutil.copytree(source, work, symlinks=True, ignore=shutil.ignore_patterns(".git", ".pact"))
    _git(work, "init", "-q")
    _git(work, "add", "-A")
    # The fresh index cannot infer files force-added despite .gitignore.
    ignored = []
    for name in caller_tracked(source, ignored_only=True):
        relative = Path(os.fsdecode(name))
        path = work / relative
        if ".pact" not in relative.parts and (path.is_symlink() or path.is_file()):
            ignored.append(name)
    if ignored:
        subprocess.run(
            ["git", "--literal-pathspecs", "add", "-f", "--pathspec-from-file=-", "--pathspec-file-nul"],
            cwd=work, env=git_env(), input=b"\0".join(ignored) + b"\0", capture_output=True, check=True,
        )
    _git(work, "commit", "-q", "--allow-empty", "--no-verify", "-m", "S0")
    return work


def collect_usage(output: Path) -> dict[str, int | float]:
    """Sum the per-call ``usage.json`` written by every native worker call."""
    total: dict[str, int | float] = {}
    for path in sorted(output.rglob("usage.json")):
        try:
            usage = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(usage, dict):
            continue
        for key, value in usage.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                total[key] = total.get(key, 0) + value
    return total


def _event(writer: OneShotWriter, event_type: str, payload: dict[str, Any]) -> None:
    from jiuwenswarm.runtime.events import RuntimeEvent

    writer.write_event(
        RuntimeEvent(
            request_id=writer.request_id,
            channel_id="process_cli",
            session_id=writer.session_id,
            payload={"event_type": event_type, **payload},
        )
    )


async def run_lh_document(source: str, lh_argv: list[str], writer: OneShotWriter) -> OneShotRunResult:
    from .cli import _complete_options, arguments_from_namespace, outcome_exit_code, run_controller

    options = parse_lh_arguments(lh_argv)
    run_input = read_run_input(source)
    writer.request_id = run_input.request_id or writer.request_id
    _validate_run_input(run_input)
    workspace = run_input.workspace
    target = Path(workspace.cwd if workspace and workspace.cwd else os.getcwd()).expanduser().resolve(strict=True)
    started = time.time()
    if options.budget_seconds is None and run_input.timeout_seconds is not None:
        options.budget_seconds = float(run_input.timeout_seconds)
    if options.budget_started_at_unix is None:
        options.budget_started_at_unix = started
    options.trial_id = options.trial_id or "1"
    options.task_id = options.task_id or f"run-{writer.request_id}"
    writer.session_id = f"lh-{writer.request_id}"
    with tempfile.TemporaryDirectory(prefix="jiuwenswarm-lh-arena-") as scratch:
        scratch_path = Path(scratch)
        options.workdir = prepare_arena(target, scratch_path)
        baseline = _git(options.workdir, "rev-parse", "HEAD^{tree}").stdout.strip()
        options.prompt = run_input.input
        if options.output is None:
            options.output = Path(tempfile.gettempdir()) / "jiuwenswarm-lh" / f"{options.task_id}-{options.trial_id}"
        options = _complete_options(options)
        args = arguments_from_namespace(options)
        _event(
            writer,
            LH_EVENT_ENABLED,
            {
                "long_horizon": True,
                "controller": "jiuwenswarm.lh",
                "workflow": args.workflow,
                "control_profile": args.control_profile,
                "budget_seconds": args.budget_seconds,
                "verify_command": args.verify_command,
                "memgraph": bool(options.memgraph_command),
                "workspace": str(target),
                "output": str(options.output),
                "argv": list(lh_argv),
            },
        )
        outcome = await run_controller(options)
        changed: list[str] = []
        verified_tree: str | None = None
        apply_error: str | None = None
        if outcome.selection is not None and outcome.candidate_available:
            try:
                cancelled = Event()
                publication = asyncio.create_task(asyncio.to_thread(
                    apply_candidate, outcome.selection, target, options.workdir, baseline,
                    work_id=outcome.work_id, deadline=args.original_deadline_unix, cancelled=cancelled,
                ))
                try:
                    changed, verified_tree = await asyncio.shield(publication)
                except asyncio.CancelledError:
                    cancelled.set()
                    # Keep the arena alive until its publication reader has stopped.
                    await asyncio.gather(publication, return_exceptions=True)
                    raise
            except (OSError, KeyError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                detail = exc.stderr.decode(errors="replace") if isinstance(exc, subprocess.CalledProcessError) and isinstance(exc.stderr, bytes) else str(exc)
                apply_error = f"{type(exc).__name__}: {detail}"[:2000]
        usage = collect_usage(options.output)
        _event(
            writer,
            LH_EVENT_FINISHED,
            {
                "long_horizon": True,
                "outcome": asdict(outcome) | {"selection": str(outcome.selection) if outcome.selection else None},
                "applied_files": changed,
                "verified_workspace_tree": verified_tree,
                "apply_error": apply_error,
                "elapsed_seconds": round(time.time() - started, 3),
            },
        )
    code = outcome_exit_code(outcome)
    if code == 0 and apply_error is not None:
        code = 5
    if code == 0:
        return OneShotRunResult(
            sequence=writer.sequence,
            request_id=writer.request_id,
            session_id=writer.session_id,
            status=RunStatus.COMPLETED,
            exit_code=0,
            output=(
                f"long-horizon run finished ({outcome.stop_reason}); "
                f"applied {len(changed)} changed file(s) to {target}"
            ),
            usage=usage,
        )
    reasons = {
        1: ("LH_NO_SELECTION", "The long-horizon controller did not publish a selection."),
        3: ("LH_INTERNAL_FAILURE", "The long-horizon controller recorded an internal failure."),
        4: ("LH_NO_CANDIDATE", "The long-horizon controller produced no exportable candidate."),
        5: ("LH_APPLY_FAILED", "The selected candidate could not be applied to the workspace."),
    }
    error_code, message = reasons[code]
    return OneShotRunResult(
        sequence=writer.sequence,
        request_id=writer.request_id,
        session_id=writer.session_id,
        status=RunStatus.FAILED,
        exit_code=code,
        error=RuntimeErrorInfo(
            code=error_code,
            message=message,
            details={"stop_reason": outcome.stop_reason, "output": str(options.output)},
        ),
        usage=usage,
    )


def execute_lh_run_json(source: str, lh_argv: list[str]) -> int:
    """Run one ``--run-json`` document through the long-horizon controller."""
    from jiuwenswarm.channels.process_cli.machine_entry import protocol_stdout

    with protocol_stdout() as output:
        writer = OneShotWriter(output, request_id=str(uuid.uuid4()))
        try:
            result = asyncio.run(run_lh_document(source, lh_argv, writer))
        except MachineInputError as error:
            writer.request_id = error.request_id or writer.request_id
            result = OneShotRunResult(
                sequence=writer.sequence,
                request_id=writer.request_id,
                session_id=writer.session_id,
                status=RunStatus.FAILED,
                exit_code=2,
                error=RuntimeErrorInfo(code=error.code, message=str(error), details=error.details),
            )
        except (KeyboardInterrupt, asyncio.CancelledError):
            result = OneShotRunResult(
                sequence=writer.sequence,
                request_id=writer.request_id,
                session_id=writer.session_id,
                status=RunStatus.CANCELLED,
                exit_code=130,
                error=RuntimeErrorInfo(code="CANCELLED", message="Command interrupted."),
            )
        except Exception as error:  # noqa: BLE001 - one terminal record per process
            result = OneShotRunResult(
                sequence=writer.sequence,
                request_id=writer.request_id,
                session_id=writer.session_id,
                status=RunStatus.FAILED,
                exit_code=1,
                error=RuntimeErrorInfo(
                    code="LH_STARTUP_FAILED", message=f"{type(error).__name__}: {error}"[:2000]
                ),
            )
        if writer.broken:
            return result.exit_code or 1
        try:
            writer.write_result(result)
        except (OSError, ValueError):
            return 1
        return result.exit_code
