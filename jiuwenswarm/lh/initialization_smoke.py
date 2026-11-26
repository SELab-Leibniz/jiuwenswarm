"""Exercise real LH initialization and S0 capture without permitting model calls.

Run with the installed runtime, including a default ``--no-dev --no-editable``
installation::

    python -m jiuwenswarm.lh.initialization_smoke --output /tmp/lh-init-smoke --check-native-imports

The output directory must be empty or absent. It retains the temporary Git
fixture, Core state, native snapshots, selection.json and smoke-result.json.
Only the P0 execution boundary is replaced: Core and native artifact capture
remain real, and the worker cannot start a process or contact a model.
The optional native import check requires the full default installation and
checks stage/logging bootstrap in a separate process without running a task.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

STOP_REASON = "initialization_smoke_complete"
IGNORE_TEXT = "ignored.txt\n"
SOURCE_TEXT = "S0 initialization fixture.\n"
_NATIVE_IMPORT_CHECK = r"""
import importlib
import json
import sys
from pathlib import Path

state, result_path = (Path(value) for value in sys.argv[1:])
result = {"schema": "jiuwenswarm/lh-native-import-smoke/v1", "status": "failed", "modules": {}}

def no_network(event, args):
    if event in {"socket.connect", "socket.getaddrinfo", "socket.sendto"}:
        raise RuntimeError("native import smoke forbids network access")

sys.addaudithook(no_network)
try:
    from jiuwenswarm.long_horizon import native_entry
    native_entry._prepare_stage(state, read_only=False)
    native_entry._configure_logging(state)
    for name in ("jiuwenswarm.channels.process_cli.machine", "jiuwenswarm.channels.process_cli.main"):
        module = importlib.import_module(name)
        result["modules"][name] = module.__file__
    result["status"] = "passed"
except BaseException as exc:
    result["error"] = {"type": type(exc).__name__, "detail": str(exc)}
    raise
finally:
    result_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
"""


def _check_native_imports(root: Path) -> dict[str, Any]:
    state = root / "native-state"
    result_path = root / "native-imports.json"
    stdout_path = root / "native-imports.stdout.log"
    stderr_path = root / "native-imports.stderr.log"
    state.mkdir()
    environment = dict(os.environ)
    environment["JIUWENSWARM_DATA_DIR"] = str(state)
    result: dict[str, Any] = {
        "schema": "jiuwenswarm/lh-native-import-smoke/v1",
        "status": "failed",
        "stdout": str(stdout_path),
        "stderr": str(stderr_path),
    }
    try:
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            completed = subprocess.run(
                [sys.executable, "-I", "-B", "-c", _NATIVE_IMPORT_CHECK, str(state), str(result_path)],
                cwd=state,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                timeout=60,
            )
        result["exit_code"] = completed.returncode
        if result_path.is_file():
            result.update(json.loads(result_path.read_text(encoding="utf-8")))
        if completed.returncode != 0 or result.get("status") != "passed":
            result["status"] = "failed"
            result.setdefault("error", {"type": "NativeImportFailure", "detail": "native import process failed"})
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        result["status"] = "failed"
        result["error"] = {"type": type(exc).__name__, "detail": str(exc)}
    result_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


@dataclass
class _NoModelWorker:
    """Supply native configuration identity, but never an inference backend."""

    binary: Path
    config_template: Path
    source_commit: str = "initialization-smoke-no-model-v1"
    benchmark_source_root: Path | None = None
    calls: int = 0

    async def run(self, *args: Any, **kwargs: Any) -> None:
        self.calls += 1
        raise RuntimeError("initialization smoke forbids native worker execution")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=LH smoke", "-c", "user.email=lh-smoke@example.invalid", *args],
        cwd=repo,
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()


async def _initialize(root: Path, worker: _NoModelWorker) -> dict[str, Any]:
    from jiuwenswarm.lh.controller import BenchmarkArguments, LongHorizonBenchmark
    from jiuwenswarm.lh.plugins.jiuwen import JiuwenPlugin
    from jiuwenswarm.long_horizon.clarification.snapshot import WorkspaceSnapshotter
    from jiuwenswarm.pact_core.runtime import WorkRuntime

    class InitializationOnlyBenchmark(LongHorizonBenchmark):
        def _run_p0(self, runtime, output, turn_root, budget, window, state, *, recover=False):
            # The real bootstrap has already created Work and frozen native S0.
            # Stop before P0 admits a role call; do not replace snapshot capture.
            _require(self._phase(state) == "s0_frozen", "P0 reached before S0 was frozen")
            self._invocation.stop_reason = STOP_REASON
            self._abort.set()
            return state, False

    repo = root / "repo"
    repo.mkdir()
    (repo / ".gitignore").write_text(IGNORE_TEXT, encoding="utf-8")
    (repo / "source.txt").write_text(SOURCE_TEXT, encoding="utf-8")
    (repo / "ignored.txt").write_text("Must not enter the frozen model view.\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", ".gitignore", "source.txt")
    # The Core export must contain the ignored fixture, so the native snapshot
    # actually exercises gitignore filtering rather than seeing an absent file.
    _git(repo, "add", "-f", "ignored.txt")
    _git(repo, "commit", "-q", "--no-verify", "-m", "S0 fixture")
    source_head = _git(repo, "rev-parse", "HEAD")
    task = root / "task.md"
    task.write_text("Inspect this fixture without changing any file.\n", encoding="utf-8")
    worker.config_template.write_text("{}\n", encoding="utf-8")
    args = BenchmarkArguments(
        task=task,
        workdir=repo,
        output=root / "output",
        worktree_root=root / "worktrees",
        task_id="initialization-smoke",
        trial_id="1",
        budget_seconds=120,
        turn_timeout_seconds=30,
        max_turns=1,
        model=None,
        workflow="suite",
    )
    outcome = await InitializationOnlyBenchmark(args, plugin=JiuwenPlugin(worker=worker)).run()
    _require(outcome.selection is not None, "controller did not publish selection.json")
    report = json.loads(outcome.selection.read_text(encoding="utf-8"))
    _require(not outcome.internal_failure, f"controller initialization failed: {report['internal_failure']}")
    _require(report["internal_failure"] is None, "selection recorded an internal failure")
    _require(report["stop_reason"] == STOP_REASON, "controller did not stop at the initialization boundary")
    _require(report["long_horizon"]["phase"] == "s0_frozen", "S0 was not durably frozen")
    _require(report["long_horizon"]["workflow"] == "suite", "smoke did not use the suite workflow")
    _require(not report["long_horizon"]["p0_ran_this_invocation"], "P0 unexpectedly ran")
    _require(report["budget"]["role_calls"] == 0, "a role call was admitted")
    _require(report["budget"]["calls"] == [], "the role-call ledger is not empty")
    _require(worker.calls == 0, "the native worker was invoked")
    _require(not report["internal_accepted"] and not report["delivered"], "initialization claimed task acceptance")
    _require(report["external_grade"]["status"] == "not_run", "initialization claimed external grading")
    _require(report["export_error"] is None, "candidate export failed")

    s0 = report["long_horizon"]["stages"]["s0"]
    snapshot_path = args.output / s0["snapshot"]
    snapshot = WorkspaceSnapshotter.load(
        snapshot_path,
        expected_manifest_hash=s0["snapshot_manifest_sha256"],
    )
    view = Path(snapshot.roots[0].view_root)
    _require(snapshot.roots[0].ignore_text == IGNORE_TEXT, "S0 did not read the nonempty .gitignore")
    _require((view / "source.txt").read_text(encoding="utf-8") == SOURCE_TEXT, "S0 lost source content")
    _require(not (view / "ignored.txt").exists(), "S0 did not apply gitignore filtering")
    _require((view / ".gitignore").read_text(encoding="utf-8") == IGNORE_TEXT, "S0 lost .gitignore")
    _require((args.output / s0["checkpoint"]).is_file(), "S0 checkpoint is missing")
    state = WorkRuntime(repo, worktree_root=args.worktree_root).read_work(outcome.work_id)
    _require(state.advisory[JiuwenPlugin.spec.progress_key]["phase"] == "s0_frozen", "Core state lost S0")
    _require(state.candidate.tree == s0["candidate_tree"], "Core and S0 refer to different candidates")
    _require(_git(repo, "rev-parse", "HEAD") == source_head, "initialization changed the source commit")
    _require(_git(repo, "diff", "--exit-code") == "", "initialization changed tracked source files")
    return {
        "work_id": outcome.work_id,
        "phase": "s0_frozen",
        "role_calls": 0,
        "selection": str(outcome.selection),
        "snapshot": str(snapshot_path),
        "snapshot_manifest_sha256": snapshot.manifest_hash,
        "gitignore_filter_verified": True,
    }


def run_smoke(output: Path, *, check_native_imports: bool = False) -> dict[str, Any]:
    """Retain JSON evidence on success or failure; never run a native worker."""
    root = output.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    if any(root.iterdir()):
        raise ValueError(f"smoke output directory must be empty: {root}")
    worker = _NoModelWorker(Path(sys.executable), root / "config.yaml")
    result: dict[str, Any] = {
        "schema": "jiuwenswarm/lh-initialization-smoke/v1",
        "status": "failed",
        "output": str(root),
        "selection": str(root / "output" / "selection.json"),
        "worker_calls": 0,
        "native_imports": {"status": "not_run" if check_native_imports else "not_requested"},
    }
    try:
        result.update(asyncio.run(_initialize(root, worker)))
        result["status"] = "passed"
        if check_native_imports:
            result["native_imports"] = _check_native_imports(root)
            if result["native_imports"]["status"] != "passed":
                result["status"] = "failed"
    except Exception as exc:
        result["status"] = "failed"
        result["error"] = {"type": type(exc).__name__, "detail": str(exc)}
        selection = Path(result["selection"])
        if selection.is_file():
            report = json.loads(selection.read_text(encoding="utf-8"))
            result["internal_failure"] = report.get("internal_failure")
            result["phase"] = report.get("long_horizon", {}).get("phase")
            result["role_calls"] = (report.get("budget") or {}).get("role_calls")
    finally:
        result["worker_calls"] = worker.calls
        (root / "smoke-result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="empty directory for retained smoke evidence")
    parser.add_argument(
        "--check-native-imports",
        action="store_true",
        help="also bootstrap native logging and import machine/runtime in an isolated, network-blocked process",
    )
    options = parser.parse_args(argv)
    output = options.output or Path(tempfile.mkdtemp(prefix="jiuwenswarm-lh-init-"))
    try:
        result = run_smoke(output, check_native_imports=options.check_native_imports)
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}))
        return 1
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
