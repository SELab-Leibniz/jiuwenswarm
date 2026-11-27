"""Check the PACT source and offline dependency shipped with JiuwenSwarm."""

import hashlib
import json
import tomllib
from importlib.resources import files
from importlib.util import find_spec
from pathlib import Path

from jiuwenswarm import pact_core


def test_pact_core_sources_and_resources() -> None:
    manifest = json.loads(files("jiuwenswarm.lh").joinpath("SOURCE.json").read_text(encoding="utf-8"))
    source = manifest["pact_core"]
    assert pact_core.__version__ == source["version"]
    assert pact_core.__name__.replace(".", "/") == source["source_directory"]
    root = files(pact_core)
    for name, digest in source["source_files"].items():
        assert hashlib.sha256(root.joinpath(*name.split("/")).read_bytes()).hexdigest() == digest, name


def test_suite_and_original_task_benchmark_share_work_runtime() -> None:
    from jiuwenswarm.lh.controller import WorkRuntime as SuiteRuntime
    from jiuwenswarm.long_horizon.pact.long_horizon_benchmark import WorkRuntime as BenchmarkRuntime
    from jiuwenswarm.pact_core.runtime import WorkRuntime

    assert SuiteRuntime is BenchmarkRuntime is WorkRuntime
    assert find_spec("jiuwenswarm.long_horizon.legacy_pact_core") is None


def test_offline_requirements_install_only_pathspec() -> None:
    package = files("jiuwenswarm.lh")
    source = json.loads(package.joinpath("SOURCE.json").read_text(encoding="utf-8"))
    pathspec = source["companion_wheels"]["pathspec"]
    requirements = package.joinpath("requirements.txt").read_text(encoding="utf-8").splitlines()
    assert set(requirements) == {
        "--no-index",
        "--find-links ./wheels",
        "--require-hashes",
        f"pathspec==1.1.1 --hash=sha256:{pathspec['sha256']}",
    }
    wheel_name = pathspec["bundled_wheel"].rsplit("/", 1)[-1]
    wheel_dir = package.joinpath("wheels")
    assert {item.name for item in wheel_dir.iterdir() if item.name.endswith(".whl")} == {wheel_name}
    wheel = wheel_dir.joinpath(wheel_name)
    assert hashlib.sha256(wheel.read_bytes()).hexdigest() == pathspec["sha256"]


def test_default_install_includes_snapshot_filter() -> None:
    root = Path(__file__).resolve().parents[2]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    packages = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))["package"]
    workswarm = next(package for package in packages if package["name"] == project["name"])

    assert "pathspec==1.1.1" in project["dependencies"]
    assert {"name": "pathspec"} in workswarm["dependencies"]
