"""Exercise real packaged configuration and worker admission in fresh processes."""

import json
import os
import subprocess
import sys
from pathlib import Path


def _run_smoke(root: Path, prelude: str = "") -> tuple[subprocess.CompletedProcess[str], dict]:
    program = """
import sys
from jiuwenswarm.lh import native_configuration_smoke as smoke
""" + prelude + """
raise SystemExit(smoke.main(['--output', sys.argv[1], '--binary', sys.executable]))
"""
    # A hostile inherited data path must never receive seed/config writes.
    external = root.parent / "unrelated-data"
    environment = dict(os.environ)
    environment["JIUWENSWARM_DATA_DIR"] = str(external)
    environment["JIUWENSWARM_CONFIG_DIR"] = str(external / "config")
    completed = subprocess.run(
        [sys.executable, "-B", "-c", program, str(root)],
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert not external.exists(), completed.stdout + completed.stderr
    result = json.loads((root / "native-configuration.json").read_text())
    return completed, result


def test_native_configuration_smoke_prepares_three_real_roles(tmp_path: Path) -> None:
    import yaml

    completed, result = _run_smoke(tmp_path / "positive")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert result["status"] == "passed"
    assert set(result["cases"]) == {"writable", "read_only", "tool_free"}
    assert result["native_subprocesses_started"] == result["model_calls"] == 0
    assert result["network_attempts"] == result["forbidden_process_attempts"] == 0
    assert result["binary_launch_tested"] is False
    seeded = yaml.safe_load(Path(result["seeded_config"]).read_text())
    assert seeded["preferred_language"] == "en"
    assert seeded["permissions"]["enabled"] is False
    assert seeded["permissions"]["file_guard"]["enabled"] is False
    assert seeded["browser"]["decision"]["api_key_env"] == "OPENROUTER_API_KEY"
    for name, case in result["cases"].items():
        assert case["status"] == "passed", case
        assert case["native_startup_blocked"] is True
        assert case["native_startup_calls"] == 1
        assert case["request_validated"] is True
        assert case["selector_preserved"] == "OPENROUTER_API_KEY"
        metadata = json.loads(Path(case["metadata"]).read_text())
        assert metadata["status"] == "preparing"
        assert metadata["actual_exit_code"] is None
        assert metadata["read_only"] is (name != "writable")
        assert Path(case["request"]).is_file()
        assert Path(case["config_snapshot"]).is_file()


def test_native_configuration_smoke_catches_original_selector_rejection(tmp_path: Path) -> None:
    # Removing precisely the new exception restores the original predicate.
    completed, result = _run_smoke(tmp_path / "old-behavior", """
from jiuwenswarm.long_horizon import worker
worker._BROWSER_KEY_NAMES = frozenset()
""")
    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert result["status"] == "failed"
    assert set(result["cases"]) == {"writable", "read_only", "tool_free"}, result
    for case in result["cases"].values():
        assert case["status"] == "failed"
        assert case["error"]["type"] == "WorkerConfigurationError"
        assert case["error"]["code"] == "INVALID_CONFIG"
        assert case["metadata_status"] == "configuration_failed"
        assert case["native_startup_calls"] == 0
        assert case["native_startup_blocked"] is False
        assert case["request_validated"] is False
        assert not (Path(case["output"]) / "request.json").exists()


def test_native_configuration_smoke_rejects_inline_model_key(tmp_path: Path) -> None:
    completed, result = _run_smoke(tmp_path / "inline-key", """
import yaml
original_seed = smoke._seed_configuration

def seed_with_inline_key(state):
    path = original_seed(state)
    config = yaml.safe_load(path.read_text())
    config['models']['defaults'][0]['model_client_config']['api_key'] = 'inline-test-credential'
    path.write_text(yaml.safe_dump(config, sort_keys=False))
    return path

smoke._seed_configuration = seed_with_inline_key
""")
    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert result["status"] == "failed"
    assert set(result["cases"]) == {"writable", "read_only", "tool_free"}, result
    for case in result["cases"].values():
        assert case["error"]["code"] == "INVALID_CONFIG"
        assert case["metadata_status"] == "configuration_failed"
        assert case["native_startup_calls"] == 0
        assert case["request_validated"] is False
        assert not (Path(case["output"]) / "native-config.before.yaml").exists()


def test_native_configuration_smoke_cannot_pass_rejected_request(tmp_path: Path) -> None:
    completed, result = _run_smoke(tmp_path / "request-rejected", """
from jiuwenswarm.long_horizon import worker

def reject_request(value):
    raise ValueError('request rejected by schema')

worker.OneShotRunInput.from_dict = reject_request
""")
    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert result["status"] == "failed"
    assert set(result["cases"]) == {"writable", "read_only", "tool_free"}, result
    for case in result["cases"].values():
        assert case["error"]["code"] == "INVALID_REQUEST"
        assert case["native_startup_calls"] == 0
        assert case["request_validated"] is False
        assert (Path(case["output"]) / "native-config.before.yaml").is_file()
        assert not (Path(case["output"]) / "request.json").exists()


def test_native_configuration_smoke_detects_swallowed_network_attempt(tmp_path: Path) -> None:
    completed, result = _run_smoke(tmp_path / "network-attempt", """
import socket
original_seed = smoke._seed_configuration

def seed_with_network_attempt(state):
    try:
        socket.getaddrinfo('example.invalid', 443)
    except RuntimeError:
        pass
    return original_seed(state)

smoke._seed_configuration = seed_with_network_attempt
""")
    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert result["status"] == "failed"
    assert result["network_attempts"] == 1
    assert result["forbidden_process_attempts"] == 0
    assert set(result["cases"]) == {"writable", "read_only", "tool_free"}, result
    assert all(case["status"] == "passed" for case in result["cases"].values())
