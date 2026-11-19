from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from jiuwenswarm.lh import cli


def test_split_lh_flag_only_removes_the_switch():
    assert cli.split_lh_flag(["fix it", "--cwd", "/repo"]) == (False, ["fix it", "--cwd", "/repo"])
    assert cli.split_lh_flag(["--lh", "fix it", "--cwd", "/repo"]) == (True, ["fix it", "--cwd", "/repo"])


def _options(*argv: str):
    return cli.build_parser().parse_args(list(argv))


def test_prompt_becomes_task_file_outside_workdir(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    worker = tmp_path / "jiuwenswarm-process"
    worker.write_text("#!/bin/sh\n")
    config = tmp_path / "config.yaml"
    config.write_text("{}\n")
    output = tmp_path / "out" / "run"
    options = cli._complete_options(_options(
        "repair the parser", "--cwd", str(repo), "--output", str(output),
        "--worker-bin", str(worker), "--config-template", str(config), "--task-id", "t1",
    ))
    assert options.workdir == repo.resolve()
    assert options.task.read_text(encoding="utf-8") == "repair the parser"
    assert not options.task.is_relative_to(repo)
    assert not output.exists()
    args = cli.arguments_from_namespace(options)
    assert args.workflow == "suite" and args.task_id == "t1" and args.budget_seconds == 172800.0


def test_prompt_and_task_file_are_mutually_exclusive(tmp_path: Path):
    task = tmp_path / "task.md"
    task.write_text("x")
    with pytest.raises(ValueError, match="exactly one"):
        cli._complete_options(_options("x", "--task", str(task)))
    with pytest.raises(ValueError, match="exactly one"):
        cli._complete_options(_options())


def test_memgraph_is_off_unless_command_and_receipt_are_both_given(tmp_path: Path):
    options = _options("x")
    assert options.memgraph_command is None and options.memgraph_receipt is None
    with pytest.raises(ValueError, match="MemGraph"):
        cli._complete_options(_options("x", "--memgraph-command", "query"))


def test_main_reports_errors_without_traceback(capsys):
    assert cli.main(["--task", "/nonexistent/task.md", "extra prompt"]) == 1
    assert "Error:" in capsys.readouterr().err
