from __future__ import annotations

import asyncio
import io
import json
import shlex
import subprocess
import time
import sys
from pathlib import Path

import pytest

from jiuwenswarm.channels.process_cli.machine_io import OneShotWriter
from jiuwenswarm.lh import cli, run_json
from jiuwenswarm.lh.controller import BenchmarkOutcome


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "app.py").write_text("VALUE = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def _run_doc(tmp_path: Path, repo: Path, **extra) -> Path:
    doc = {
        "schema_version": "0.1", "type": "run", "input": "set VALUE to 2",
        "mode": "agent.code.normal", "max_turns": 500, "timeout_seconds": 600,
        "workspace": {"cwd": str(repo), "project_dir": str(repo), "trusted_dirs": [str(repo)]},
        **extra,
    }
    path = tmp_path / "run.json"
    path.write_text(json.dumps(doc))
    return path


def _records(stream: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


def _support(tmp_path: Path) -> list[str]:
    worker = tmp_path / "jiuwenswarm-process"
    worker.write_text("#!/bin/sh\n")
    config = tmp_path / "config.yaml"
    config.write_text("{}\n")
    return ["--worker-bin", str(worker), "--config-template", str(config), "--output", str(tmp_path / "lh-out")]


def test_find_run_json_accepts_both_spellings_in_any_position():
    assert run_json.find_run_json(["--run-json", "/tmp/r.json"]) == ("/tmp/r.json", [])
    assert run_json.find_run_json(["--verify", "pytest", "--run-json=/tmp/r.json"]) == (
        "/tmp/r.json", ["--verify", "pytest"],
    )
    assert run_json.find_run_json(["fix it", "--cwd", "/repo"]) == (None, ["fix it", "--cwd", "/repo"])


def test_unknown_or_reserved_lh_arguments_are_reported_not_ignored():
    with pytest.raises(run_json.LHArgumentError, match="unrecognized"):
        run_json.parse_lh_arguments(["--checkpoint-every", "20"])
    with pytest.raises(run_json.LHArgumentError, match="--workdir"):
        run_json.parse_lh_arguments(["--cwd", "/repo"])
    options = run_json.parse_lh_arguments(["--verify", "pytest -q", "--control-profile=bounded"])
    assert options.verify == "pytest -q" and options.control_profile == "bounded"


def test_lh_run_applies_only_the_exported_patch_and_reports_evidence(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    source = _run_doc(tmp_path, repo)
    seen = {}

    async def fake_controller(options):
        seen["workdir"] = options.workdir
        seen["budget"] = options.budget_seconds
        seen["task"] = options.task.read_text()
        from jiuwenswarm.pact_core._runtime_serialization import encode
        from jiuwenswarm.pact_core.runtime import WorkRuntime

        runtime = WorkRuntime(options.workdir, worktree_root=tmp_path / "worktrees")
        work = runtime.create_work(request_id="run-fixture", contract={
            "schema": "pact-runtime/goal-contract/v1", "goal": "Update VALUE",
            "acceptance_criteria": [{"id": "value", "text": "VALUE is 2"}], "non_goals": [],
        })
        with runtime.begin_execution(work.work_id, expected_revision=work.revision, draft=True) as handle:
            (handle.workdir / "app.py").write_text("VALUE = 2\n")
            work = runtime.save_candidate(handle, summary="Change VALUE")
        receipt = runtime.export_candidate(work.candidate, options.output / "exports")
        for index, tokens in enumerate((10, 32)):
            call = options.output / "calls" / str(index)
            call.mkdir(parents=True)
            (call / "usage.json").write_text(json.dumps({"input_tokens": tokens, "model_calls": 1}))
        selection = options.output / "selection.json"
        selection.write_text(json.dumps({
            "work_id": work.work_id, "candidate": encode(work.candidate),
            "export": encode(receipt), "internal_accepted": False, "delivered": False,
        }))
        return BenchmarkOutcome(selection, False, "budget_exhausted", work.work_id, candidate_available=True)

    monkeypatch.setattr(cli, "run_controller", fake_controller)
    stream = io.StringIO()
    writer = OneShotWriter(stream, request_id="req-1")
    result = asyncio.run(run_json.run_lh_document(str(source), _support(tmp_path), writer))
    writer.write_result(result)
    records = _records(stream)

    assert seen["workdir"] != repo and not seen["workdir"].is_relative_to(repo)
    assert seen["budget"] == 600.0 and seen["task"] == "set VALUE to 2"
    assert (repo / "app.py").read_text() == "VALUE = 2\n"
    assert not (repo / ".pact").exists()
    assert [r["event_type"] for r in records if r["type"] == "event"] == ["lh.enabled", "lh.finished"]
    enabled = records[0]["payload"]
    assert enabled["long_horizon"] is True and enabled["workflow"] == "suite"
    assert records[1]["payload"]["applied_files"] == ["app.py"]
    assert records[1]["payload"]["verified_workspace_tree"] is None  # partial export
    final = records[-1]
    assert final["type"] == "result" and final["status"] == "completed" and final["exit_code"] == 0
    assert final["usage"] == {"input_tokens": 42, "model_calls": 2}


def test_lh_run_without_candidate_fails_with_structured_error(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    source = _run_doc(tmp_path, repo)

    async def fake_controller(options):
        selection = options.output / "selection.json"
        options.output.mkdir(parents=True)
        selection.write_text(json.dumps({"export": None}))
        return BenchmarkOutcome(selection, False, "budget_exhausted", "work-1", candidate_available=False)

    monkeypatch.setattr(cli, "run_controller", fake_controller)
    stream = io.StringIO()
    writer = OneShotWriter(stream, request_id="req-2")
    result = asyncio.run(run_json.run_lh_document(str(source), _support(tmp_path), writer))
    assert result.status == "failed" and result.exit_code == 4
    assert result.error.code == "LH_NO_CANDIDATE"
    assert (repo / "app.py").read_text() == "VALUE = 1\n"


def _process(args: list[str]) -> subprocess.CompletedProcess[str]:
    code = "import sys; from jiuwenswarm.channels.process_cli.main import main; sys.argv[0]='jiuwenswarm-process'; main()"
    return subprocess.run([sys.executable, "-c", code, *args], capture_output=True, text=True, timeout=120)


@pytest.mark.parametrize(
    ("extra_doc", "extra_args", "code"),
    [
        ({"mode": "agent.code.plan"}, [], "UNSUPPORTED_COMBINATION"),
        ({}, ["--checkpoint-every", "20"], "INVALID_ARGUMENTS"),
    ],
)
def test_process_entry_routes_run_json_with_lh_and_rejects_bad_combinations(tmp_path, extra_doc, extra_args, code):
    repo = _repo(tmp_path)
    source = _run_doc(tmp_path, repo, **extra_doc)
    completed = _process(["--run-json", str(source), "--lh", *extra_args])
    assert completed.returncode == 2, completed.stderr
    final = json.loads(completed.stdout.strip().splitlines()[-1])
    assert final["type"] == "result" and final["status"] == "failed"
    assert final["error"]["code"] == code


@pytest.fixture(scope="module")
def accepted_export(tmp_path_factory):
    """Run the real Core on a private copy once; all publication cases share its sealed bytes."""
    from test_portable_flow import plugin

    from jiuwenswarm.lh.controller import BenchmarkArguments, LongHorizonBenchmark

    root = tmp_path_factory.mktemp("sealed")
    live = _repo(root)
    (live / "src").mkdir()
    (live / "src/README.md").write_text("baseline\n")
    (live / ".gitignore").write_text("*.cache\n")
    core = run_json.prepare_arena(live, root / "arena")
    base = _git(core, "rev-parse", "HEAD^{tree}").strip()
    task = root / "task.md"
    task.write_text("Create greeting.txt containing hello. Preserve existing behavior.")
    args = BenchmarkArguments(
        workflow="benchmark", workdir=core, task=task, task_id="publication", trial_id="1",
        budget_seconds=300, output=root / "out", worktree_root=root / "worktrees",
        verify_command=shlex.join([sys.executable, "-c", "from pathlib import Path; assert Path('greeting.txt').read_text() == 'hello\\n'"]),
    )
    outcome = asyncio.run(LongHorizonBenchmark(args, plugin=plugin([])).run())
    report = json.loads(outcome.selection.read_bytes())
    assert report["internal_accepted"] and report["delivered"], outcome.stop_reason
    return live, core, base, outcome.selection, outcome.work_id


def _publication_target(tmp_path, fixture):
    import shutil

    live, core, base, selection, work = fixture
    target = tmp_path / "subdirectory"
    shutil.copytree(live, target, ignore=shutil.ignore_patterns(".git"))
    return target, core, base, selection, work


def test_accepted_publication_is_idempotent_in_a_subdirectory(tmp_path, accepted_export, monkeypatch):
    target, core, base, selection, work = _publication_target(tmp_path, accepted_export)
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "outer")
    head = _git(tmp_path, "rev-parse", "HEAD")
    index = _git(tmp_path, "write-tree")
    (target / "build.cache").write_text("ignored build output")
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "missing"))
    tree = json.loads(selection.read_bytes())["candidate"]["tree"]
    assert run_json.apply_candidate(selection, target, core, base, work_id=work, deadline=None) == (["greeting.txt"], tree)
    assert run_json.apply_candidate(selection, target, core, base, work_id=work, deadline=None) == ([], tree)
    monkeypatch.delenv("GIT_DIR")
    assert (target / "greeting.txt").read_text() == "hello\n"
    assert (target / "build.cache").read_text() == "ignored build output"
    assert _git(tmp_path, "rev-parse", "HEAD") == head and _git(tmp_path, "write-tree") == index


@pytest.mark.parametrize("drift", ["bytes", "mode", "symlink", "extra", "checkpoint", "receipt", "expired", "after-apply"])
def test_accepted_publication_refuses_drift(tmp_path, accepted_export, monkeypatch, drift):
    from jiuwenswarm.lh import publication

    target, core, base, selection, work = _publication_target(tmp_path, accepted_export)
    report = json.loads(selection.read_bytes())
    deadline = time.time() + 60
    if drift == "bytes":
        (target / "app.py").write_text("caller edit\n")
    elif drift == "mode":
        (target / "app.py").chmod(0o755)
    elif drift == "symlink":
        (target / "app.py").unlink()
        (target / "app.py").symlink_to(core / "app.py")
    elif drift == "extra":
        (target / "untracked.txt").touch()
    elif drift in ("checkpoint", "receipt"):
        if drift == "checkpoint":
            report["delivery"]["checkpoint"] = base
        else:
            report["export"]["artifact_sha256"] = "0" * 64
        selection = tmp_path / "selection.json"
        selection.write_text(json.dumps(report))
    elif drift == "expired":
        deadline = time.time() - 1
    else:
        real_git = publication._git

        def edit_after_apply(*args, **kwargs):
            result = real_git(*args, **kwargs)
            if args[2] == "apply":
                (target / "greeting.txt").write_text("concurrent edit\n")
            return result

        monkeypatch.setattr(publication, "_git", edit_after_apply)
    with pytest.raises((ValueError, RuntimeError, TimeoutError)):
        publication.apply_candidate(selection, target, core, base, work_id=work, deadline=deadline)
    if drift != "after-apply":
        assert not (target / "greeting.txt").exists()


def test_publication_error_preserves_controller_failure(tmp_path, monkeypatch):
    source = _run_doc(tmp_path, _repo(tmp_path))

    async def incomplete_failure(options):
        selection = tmp_path / "broken-selection.json"
        selection.write_text("{}")
        return BenchmarkOutcome(selection, True, "runtime_error", "failed-work", candidate_available=True)

    monkeypatch.setattr(cli, "run_controller", incomplete_failure)
    stream = io.StringIO()
    result = asyncio.run(run_json.run_lh_document(str(source), _support(tmp_path), OneShotWriter(stream, request_id="r")))
    assert result.exit_code == 3 and result.error.code == "LH_INTERNAL_FAILURE"
    assert _records(stream)[-1]["payload"]["apply_error"]


def test_private_baseline_and_publication_include_ignored_tracked_files(tmp_path):
    from jiuwenswarm.lh import publication

    source = _repo(tmp_path)
    (source / ".gitignore").write_text("*.bin\n")
    tracked = source / "tracked.bin"
    tracked.write_bytes(b"public fixture\0")
    _git(source, "add", "-f", "tracked.bin")
    _git(source, "update-index", "--assume-unchanged", "tracked.bin")
    core = run_json.prepare_arena(source, tmp_path / "arena")
    tree = _git(core, "rev-parse", "HEAD^{tree}").strip()
    assert "tracked.bin" in _git(core, "ls-tree", "--name-only", tree).splitlines()
    assert publication._matches(source, core, tree, lambda: None)
    tracked.write_bytes(b"changed fixture\0")
    assert not publication._matches(source, core, tree, lambda: None)


@pytest.mark.asyncio
async def test_cancelling_publication_joins_before_the_arena_is_removed(tmp_path, monkeypatch):
    source = _run_doc(tmp_path, _repo(tmp_path))
    loop = asyncio.get_running_loop()
    publishing = asyncio.Event()
    observed = []

    async def done(options):
        return BenchmarkOutcome(tmp_path / "selection.json", False, "delivered", "w", candidate_available=True)

    def wait_for_cancel(_selection, _target, core, _baseline, *, cancelled, **kwargs):
        loop.call_soon_threadsafe(publishing.set)
        assert cancelled.wait(5)
        observed.append(core.is_dir())
        raise asyncio.CancelledError

    monkeypatch.setattr(cli, "run_controller", done)
    monkeypatch.setattr(run_json, "apply_candidate", wait_for_cancel)
    writer = OneShotWriter(io.StringIO(), request_id="cancel")
    task = asyncio.create_task(run_json.run_lh_document(str(source), _support(tmp_path), writer))
    await asyncio.wait_for(publishing.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert observed == [True]
