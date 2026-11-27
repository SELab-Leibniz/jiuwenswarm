"""Verify the migration notice without starting a Work in the caller's directory."""

import os
from pathlib import Path
import subprocess
import sys

import pytest

import jiuwenswarm


@pytest.mark.parametrize("module", ["jiuwenswarm.long_horizon", "jiuwenswarm.long_horizon.controller"])
@pytest.mark.parametrize(("argv", "status"), [([], 2), (["--help"], 0), (["--workspace", "."], 2)])
def test_retired_entrypoint_prints_modern_invocation(module, argv, status, tmp_path):
    environment = {**os.environ, "PYTHONPATH": str(Path(jiuwenswarm.__file__).resolve().parents[1])}
    result = subprocess.run(
        [sys.executable, "-B", "-m", module, *argv], cwd=tmp_path,
        env=environment, capture_output=True, text=True, timeout=15, check=False,
    )
    assert result.returncode == status
    notice, unused_stream = (result.stdout, result.stderr) if status == 0 else (result.stderr, result.stdout)
    assert "jiuwenswarm-process --lh --workflow suite" in notice
    assert "--workflow benchmark" in notice
    assert unused_stream == ""
    assert list(tmp_path.iterdir()) == []
