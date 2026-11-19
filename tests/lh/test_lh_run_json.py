from __future__ import annotations

import asyncio
import io
import json
import subprocess
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
        (options.workdir / ".pact").mkdir()
        (options.workdir / ".pact" / "ledger").write_text("x")
        (options.workdir / "app.py").write_text("VALUE = 2\n")
        patch = options.output / "exports" / "candidate.patch"
        patch.parent.mkdir(parents=True)
        patch.write_text(_git(options.workdir, "diff", "--binary", "--", "app.py"))
        for index, tokens in enumerate((10, 32)):
            call = options.output / "calls" / str(index)
            call.mkdir(parents=True)
            (call / "usage.json").write_text(json.dumps({"input_tokens": tokens, "model_calls": 1}))
        selection = options.output / "selection.json"
        selection.write_text(json.dumps({"export": {"patch": str(patch)}}))
        return BenchmarkOutcome(selection, False, "delivered", "work-1", candidate_available=True)

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
