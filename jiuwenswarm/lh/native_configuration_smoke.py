"""Validate Beacon-style configuration through real worker admission, without inference.

Run in a fresh process with the installed package. The output directory must be
empty. Seed, isolated configurations, requests and metadata remain available for
inspection. Only ``_native_startup`` is replaced, after real configuration and
OneShotRunInput validation; the native command and model are never started.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path
from typing import Any, Iterator


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


@contextmanager
def _data_directory(state: Path) -> Iterator[None]:
    """Bind native path singletons before import, without changing the user's home."""
    values = {
        "JIUWENSWARM_DATA_DIR": str(state),
        "JIUWENSWARM_CONFIG_DIR": str(state / "config"),
    }
    previous = {name: os.environ.get(name) for name in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _seed_configuration(state: Path) -> Path:
    """Follow Beacon's seed rules, using packaged resources and no credentials."""
    (state / "config").mkdir(parents=True)
    from jiuwenswarm.common.utils import get_config_dir, prepare_workspace

    expected = state / "config" / "config.yaml"
    _require(Path(get_config_dir()).resolve() == expected.parent, "config directory escaped smoke state")
    loaded = sys.modules.get("jiuwenswarm.common.config")
    if loaded is not None:
        _require(Path(loaded.CONFIG_YAML_PATH).resolve() == expected, "run configuration smoke in a fresh process")
    prepare_workspace(overwrite=True, preferred_language="en", workspace_dir=state)
    from jiuwenswarm.common import config

    _require(Path(config.CONFIG_YAML_PATH).resolve() == expected, "config singleton escaped smoke state")
    config.update_permissions_profile_in_config("full_access")

    def mutate(value: dict[str, Any]) -> dict[str, Any]:
        value["preferred_language"] = "en"
        value.setdefault("permissions", {}).setdefault("file_guard", {})["enabled"] = False
        return value

    config.update_config(mutate)
    return expected


async def _run_case(
    root: Path,
    template: Path,
    seeded: dict[str, Any],
    *,
    binary: Path,
    name: str,
    stage: str,
    read_only: bool,
    tool_policy: str,
) -> dict[str, Any]:
    import yaml
    from jiuwenswarm.long_horizon.worker import JiuwenWorker

    class NativeStartupBlocked(RuntimeError):
        """The expected stopping point, before command construction or spawning."""

    class ConfigurationOnlyWorker(JiuwenWorker):
        startup_calls = 0

        def _native_startup(self, *, state, read_only, environment=None):
            self.startup_calls += 1
            raise NativeStartupBlocked("native startup intentionally blocked after request validation")

    output = root / name
    worker = ConfigurationOnlyWorker(binary, template)
    result: dict[str, Any] = {
        "status": "failed",
        "read_only": read_only,
        "tool_policy": tool_policy,
        "output": str(output),
        "native_startup_blocked": False,
        "request_validated": False,
        "native_subprocesses_started": 0,
        "model_calls": 0,
    }
    try:
        try:
            await worker.run(
                stage=stage,
                prompt="Validate configuration only; native execution must remain blocked.",
                workspace=root / "repo",
                output_dir=output,
                read_only=read_only,
                tool_policy=tool_policy,
                timeout_seconds=30,
                environment={},
            )
        except NativeStartupBlocked:
            result["native_startup_blocked"] = True
        _require(worker.startup_calls == 1, "worker did not reach the blocked native startup boundary")
        metadata_path = output / "metadata.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        request_path = output / "request.json"
        request = json.loads(request_path.read_text(encoding="utf-8"))
        snapshot = output / "native-config.before.yaml"
        state = output / "state"
        effective_path = state / "config" / "config.yaml"
        effective = yaml.safe_load(snapshot.read_text(encoding="utf-8"))
        _require(snapshot.read_bytes() == effective_path.read_bytes(), "config snapshot differs from live stage config")
        _require(metadata["status"] == "preparing", "worker unexpectedly left the preparation phase")
        _require(metadata["actual_exit_code"] is None, "native execution unexpectedly produced an exit status")
        _require(not (output / "native-config.after.yaml").exists(), "native execution produced an after snapshot")
        _require(request["type"] == "run" and request["schema_version"] == "0.1", "native request has wrong schema")
        _require(request["mode"] == "agent.code.normal", "native request has wrong mode")
        _require(request["workspace"]["project_dir"] == str(root / "repo"), "request escaped the fixture workspace")
        expected_cwd = state if read_only else root / "repo"
        _require(request["workspace"]["cwd"] == str(expected_cwd), "request cwd violates role isolation")
        selector = effective["browser"]["decision"]["api_key_env"]
        _require(selector == seeded["browser"]["decision"]["api_key_env"], "environment selector was rewritten")
        _require(effective["models"] == seeded["models"], "native model configuration was changed")
        _require(effective["modes"]["code"]["tool_policy"] == tool_policy, "tool policy was not applied")
        _require(effective["preferred_language"] == "en", "seed language was lost")
        if read_only:
            _require(effective["permissions"]["file_guard"]["workspace"]["write"] == "deny", "read-only writes were not denied")
            _require(effective["hooks"]["disable_all_hooks"] is True, "read-only hooks were not disabled")
        else:
            _require(effective["permissions"] == seeded["permissions"], "writable permissions changed")
        if read_only or tool_policy == "none":
            _require(effective["mcp"]["servers"] == [], "restricted role retained external MCP servers")
        if tool_policy == "none":
            recall = effective["react"]["context_engine_config"]["compression_recall_config"]
            _require(recall["enabled"] is False, "tool-free role retained the recall tool")
        result.update(
            status="passed",
            request_validated=True,
            selector_preserved=selector,
            request=str(request_path),
            config_snapshot=str(snapshot),
            metadata=str(metadata_path),
            metadata_status=metadata["status"],
        )
    except Exception as exc:
        result["error"] = {"type": type(exc).__name__, "code": getattr(exc, "code", None), "detail": str(exc)}
        metadata_path = output / "metadata.json"
        if metadata_path.is_file():
            result["metadata"] = str(metadata_path)
            result["metadata_status"] = json.loads(metadata_path.read_text(encoding="utf-8"))["status"]
    result["native_startup_calls"] = worker.startup_calls
    return result


def run_smoke(output: Path, *, binary: Path | None = None) -> dict[str, Any]:
    """Exercise three real worker roles and preserve failure evidence."""
    root = output.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    if any(root.iterdir()):
        raise ValueError(f"configuration smoke output directory must be empty: {root}")
    result_path = root / "native-configuration.json"
    binary = binary or Path(sys.executable).parent / "jiuwenswarm-process"
    result: dict[str, Any] = {
        "schema": "jiuwenswarm/lh-native-configuration-smoke/v1",
        "status": "failed",
        "result": str(result_path),
        "data_dir": str(root / "seed-state"),
        "binary": str(binary),
        "binary_launch_tested": False,
        "cases": {},
        "native_subprocesses_started": 0,
        "model_calls": 0,
        "boundary": "real JiuwenWorker.run through config isolation and OneShotRunInput; stop at _native_startup",
    }
    guard = {"active": True, "network_attempts": 0, "forbidden_process_attempts": 0}

    def no_execution(event: str, args: tuple[Any, ...]) -> None:
        if not guard["active"]:
            return
        if event in {"socket.connect", "socket.getaddrinfo", "socket.sendto"}:
            guard["network_attempts"] += 1
            raise RuntimeError("configuration smoke forbids network access")
        if event == "subprocess.Popen":
            # Worker provenance reads Git HEAD/status; no native child is allowed.
            command = args[1]
            if not isinstance(command, (list, tuple)) or not command or command[0] != "git":
                guard["forbidden_process_attempts"] += 1
                raise RuntimeError("configuration smoke forbids native subprocesses")

    sys.addaudithook(no_execution)
    try:
        import yaml

        _require(binary.is_file() and os.access(binary, os.X_OK), "native worker binary is missing or not executable")
        packaged = files("jiuwenswarm.resources").joinpath("config.yaml")
        packaged_bytes = packaged.read_bytes()
        original = yaml.safe_load(packaged_bytes)
        result["packaged_template"] = str(packaged)
        result["packaged_template_sha256"] = hashlib.sha256(packaged_bytes).hexdigest()
        with _data_directory(root / "seed-state"):
            template = _seed_configuration(root / "seed-state")
            seeded = yaml.safe_load(template.read_text(encoding="utf-8"))
            _require(seeded["browser"]["decision"]["api_key_env"] == original["browser"]["decision"]["api_key_env"], "seed changed environment selector")
            _require(seeded["preferred_language"] == "en", "seed did not select English")
            _require(seeded["permissions"]["enabled"] is False, "seed did not select full access")
            _require(seeded["permissions"]["mode"] == "manual", "seed permission mode differs from Beacon")
            _require(seeded["permissions"]["file_guard"]["enabled"] is False, "seed did not disable file guard")
            result["seeded_config"] = str(template)
            repo = root / "repo"
            repo.mkdir()
            (repo / "source.txt").write_text("Native configuration smoke fixture.\n", encoding="utf-8")
            for name, stage, read_only, tool_policy in (
                ("writable", "benchmark-worker", False, "configured"),
                ("read_only", "benchmark-reviewer", True, "configured"),
                ("tool_free", "benchmark-synthesis", True, "none"),
            ):
                result["cases"][name] = asyncio.run(_run_case(
                    root, template, seeded, binary=binary, name=name, stage=stage,
                    read_only=read_only, tool_policy=tool_policy,
                ))
        _require(packaged.read_bytes() == packaged_bytes, "packaged template was modified")
        _require(all(case["status"] == "passed" for case in result["cases"].values()), "native configuration admission failed")
        _require(guard["network_attempts"] == guard["forbidden_process_attempts"] == 0, "execution was attempted during configuration smoke")
        result["status"] = "passed"
    except Exception as exc:
        result["status"] = "failed"
        result["error"] = {"type": type(exc).__name__, "detail": str(exc)}
    finally:
        guard["active"] = False
        result["network_attempts"] = guard["network_attempts"]
        result["forbidden_process_attempts"] = guard["forbidden_process_attempts"]
        result_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--binary", type=Path, help="explicit identity-only binary for source tests; never executed")
    options = parser.parse_args(argv)

    try:
        result = run_smoke(options.output, binary=options.binary)
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}))
        return 1
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
