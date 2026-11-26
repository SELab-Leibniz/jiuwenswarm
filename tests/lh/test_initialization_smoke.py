"""The installed-runtime smoke exercises native S0 capture without inference."""

import json
import subprocess
import sys
from pathlib import Path

from jiuwenswarm.lh.initialization_smoke import run_smoke


def test_initialization_smoke_freezes_real_s0_without_model_calls(tmp_path: Path) -> None:
    result = run_smoke(tmp_path / "smoke")
    assert result["status"] == "passed", result
    assert result["phase"] == "s0_frozen"
    assert result["worker_calls"] == result["role_calls"] == 0
    assert result["gitignore_filter_verified"] is True
    assert len(result["snapshot_manifest_sha256"]) == 64
    assert json.loads((tmp_path / "smoke/smoke-result.json").read_text()) == result


def test_initialization_smoke_reports_missing_snapshot_dependency(tmp_path: Path) -> None:
    # A fresh process keeps pathspec absent even if another test imported it.
    # Only dependency resolution is blocked; Core, capture, and publication run.
    program = """
import importlib.abc
import sys

class MissingPathspec(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'pathspec' or fullname.startswith('pathspec.'):
            raise ModuleNotFoundError("No module named 'pathspec'", name='pathspec')
        return None

sys.meta_path.insert(0, MissingPathspec())
from jiuwenswarm.lh.initialization_smoke import main
raise SystemExit(main(['--output', sys.argv[1]]))
"""
    root = tmp_path / "missing-pathspec"
    completed = subprocess.run(
        [sys.executable, "-c", program, str(root)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 1, completed.stdout + completed.stderr
    result = json.loads((root / "smoke-result.json").read_text())
    assert result["status"] == "failed"
    assert result["worker_calls"] == result["role_calls"] == 0
    assert result["internal_failure"]["type"] == "ModuleNotFoundError"
    assert "pathspec" in result["internal_failure"]["detail"]
    report = json.loads(Path(result["selection"]).read_text())
    assert report["long_horizon"]["phase"] == "created"
    assert "s0" not in report["long_horizon"]["stages"]


def test_native_import_check_exception_fails_after_successful_s0(tmp_path: Path, monkeypatch) -> None:
    from jiuwenswarm.lh import initialization_smoke

    def fail_native_check(root: Path) -> None:
        raise OSError("native check output could not be written")

    monkeypatch.setattr(initialization_smoke, "_check_native_imports", fail_native_check)
    root = tmp_path / "native-check-error"
    assert initialization_smoke.main(["--output", str(root), "--check-native-imports"]) == 1
    result = json.loads((root / "smoke-result.json").read_text())
    assert result["status"] == "failed"
    assert result["error"]["type"] == "OSError"
    assert result["phase"] == "s0_frozen"
    assert result["worker_calls"] == result["role_calls"] == 0
    assert result["internal_failure"] is None
