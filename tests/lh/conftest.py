from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def git_repo_factory():
    def make(path: Path) -> Path:
        path.mkdir(parents=True)

        def git(*args: str) -> None:
            subprocess.run(["git", *args], cwd=path, check=True, capture_output=True, stdin=subprocess.DEVNULL)

        git("init", "-b", "main")
        git("config", "user.email", "prototype@example.invalid")
        git("config", "user.name", "Prototype test")
        (path / "src").mkdir()
        (path / "src/README.md").write_text("Baseline repository.\n")
        git("add", ".")
        git("commit", "-m", "Baseline")
        return path

    return make


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CHRYS_SESSION_ROOT_DIR", str(tmp_path / "sessions"))


@pytest.fixture(autouse=True)
def isolated_side_call_recovery_state():
    from jiuwenswarm.lh.reviewer_execution import clear_side_call_integrity_state

    clear_side_call_integrity_state()
    yield
    clear_side_call_integrity_state()
