"""Native read-only stage writes stay in the stage state, never the frozen view.

The deterministic cases run a small fake native CLI through the real companion
bootstrap. That child changes cwd to the request workspace before opening a
rotating log and a WAL SQLite database, exactly where openjiuwen's import-time
logging and memory index would. These controls use synthetic runtime modules and make no provider calls.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[2]
WORKER = ROOT / "jiuwenswarm/long_horizon/worker.py"
NATIVE_ENTRY = WORKER.with_name("native_entry.py")


# ---------------------------------------------------------------------------
# Worker under test, without requiring an installed native runtime.
# ---------------------------------------------------------------------------


@pytest.fixture
def worker_module(monkeypatch):
    protocol = types.ModuleType("jiuwenswarm.channels.process_cli.protocol.model")

    class Schema:
        @classmethod
        def from_dict(cls, value):
            return value

    protocol.OneShotEvent = protocol.OneShotRunInput = protocol.OneShotRunResult = Schema
    for name in ("jiuwenswarm", "jiuwenswarm.channels", "jiuwenswarm.channels.process_cli"):
        monkeypatch.setitem(sys.modules, name, sys.modules.get(name) or types.ModuleType(name))
    monkeypatch.setitem(sys.modules, "jiuwenswarm.channels.process_cli.protocol", types.ModuleType("protocol"))
    monkeypatch.setitem(sys.modules, protocol.__name__, protocol)
    spec = importlib.util.spec_from_file_location("jiuwenswarm.long_horizon.stage_write_worker", WORKER)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    # The vendor archive carries only the companion subtree, not the native
    # product's shipped resources/config.yaml. Keep the permission shape
    # explicit without depending on that separate archive.
    module._readonly_permissions = lambda config: {
        "enabled": True, "schema": "tiered_policy", "permission_mode": "strict",
        "package_builtin_rules": True, "mode": "manual", "defaults": {"*": "deny"},
        "tools": {"read_file": "allow", "grep": "allow"}, "rules": [],
        "approval_overrides": [],
        "file_guard": {
            "enabled": True,
            "workspace": {"read": "allow", "write": "deny", "exec": "deny"},
            "defaults": {"read": "deny", "write": "deny", "exec": "deny"}, "paths": [],
        },
    }
    return module


def _config(path: Path) -> Path:
    path.write_text(
        "models: {}\n"
        "permissions:\n  defaults:\n    '*': allow\n"
        "mcp:\n  servers: []\n"
        "hooks: {}\n"
        "trajectory_ui: {}\n"
        "agent_observability: {}\n"
        "react:\n  context_engine_config: {}\n"
        "memory:\n  store: {}\n"
    )
    return path


def _recall_config(path: Path) -> Path:
    """A normal native template whose context engine has recall enabled."""
    path.write_text(
        "models: {}\n"
        "permissions:\n  defaults:\n    '*': allow\n"
        "mcp:\n  servers: []\n"
        "hooks: {}\n"
        "trajectory_ui: {}\n"
        "agent_observability: {}\n"
        "react:\n"
        "  context_engine_config:\n"
        "    compression_recall_config:\n"
        "      enabled: true\n"
        "      history_limit: 12\n"
        "    compression:\n"
        "      enabled: true\n"
        "      tiers: [dialogue, current_round, round_level]\n"
        "    actual_window_recovery:\n"
        "      enabled: true\n"
        "      reserve_tokens: 512\n"
        "memory:\n  store: {}\n"
    )
    return path


def _binary(path: Path) -> Path:
    path.write_text("#!/bin/sh\nexit 1\n")
    path.chmod(0o700)
    return path


def _write(path: Path, text: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _fake_runtime(tmp_path: Path) -> Path:
    """A synthetic native installation containing the real bootstrap file."""
    root = tmp_path / "runtime-root"
    package = root / "jiuwenswarm"
    _write(package / "__init__.py")
    _write(package / "long_horizon/__init__.py")
    (package / "long_horizon/native_entry.py").write_bytes(NATIVE_ENTRY.read_bytes())
    for name in ("channels", "channels/process_cli"):
        _write(package / name / "__init__.py")
    _write(
        package / "channels/process_cli/machine_entry.py",
        "from contextlib import contextmanager\n"
        "import sys\n"
        "\n"
        "@contextmanager\n"
        "def protocol_stdout():\n"
        "    yield sys.stdout\n",
    )
    _write(
        package / "channels/process_cli/main.py",
        (ROOT / "tests" / "lh" / "fixtures" / "stage-writes-native-main.py").read_text(encoding="utf-8"),
    )
    for name in ("openjiuwen", "openjiuwen/core", "openjiuwen/core/common", "openjiuwen/core/common/logging"):
        _write(root / name / "__init__.py")
    _write(
        root / "openjiuwen/core/common/logging/log_config.py",
        "import json\n"
        "from pathlib import Path\n"
        "\n"
        "def configure_log_config(config):\n"
        "    directory = Path(config['log_path'])\n"
        "    directory.mkdir(parents=True, exist_ok=True)\n"
        "    (directory / 'configured.json').write_text(json.dumps(config, sort_keys=True))\n",
    )
    return root


def _environment(runtime: Path, receipt: Path) -> dict[str, str]:
    return {
        "PYTHONPATH": str(runtime),
        "PATH": os.environ.get("PATH", ""),
        "STAGE_WRITE_RECEIPT": str(receipt),
        "HOME": str(receipt.parent / "home"),
    }


def _manifest(root: Path) -> dict[str, dict]:
    entries: dict[str, dict] = {}

    def visit(directory: Path) -> None:
        for child in sorted(directory.iterdir(), key=lambda item: item.name):
            relative = str(child.relative_to(root))
            info = child.lstat()
            row = {
                "mode": stat.S_IMODE(info.st_mode),
                "uid": info.st_uid,
                "gid": info.st_gid,
            }
            if child.is_symlink():
                row.update(kind="symlink", target=os.readlink(child))
            elif child.is_dir():
                row.update(kind="dir")
                visit(child)
            elif child.is_file():
                row.update(kind="file", size=info.st_size,
                           sha256=hashlib.sha256(child.read_bytes()).hexdigest())
            else:
                row.update(kind="other", size=info.st_size)
            entries[relative] = row

    visit(root)
    return entries


def _under(path: str | Path, root: Path) -> bool:
    candidate, root = Path(os.path.realpath(path)), Path(os.path.realpath(root))
    return candidate == root or root in candidate.parents


def _run(worker_module, worker, *, output, view, stage, prompt="go", read_only=True,
         session_id=None, tool_policy="configured", environment):
    return asyncio.run(worker.run(
        stage=stage, prompt=prompt, workspace=view, output_dir=output,
        read_only=read_only, timeout_seconds=60, session_id=session_id,
        tool_policy=tool_policy, environment=environment,
    ))


@pytest.mark.parametrize("stage", ["clarification-proposal", "pact-goal-contract"])
@pytest.mark.parametrize("outcome", ["success", "cancellation"])
def test_fake_native_readonly_stage_preserves_view_digest(worker_module, tmp_path, stage, outcome):
    runtime = _fake_runtime(tmp_path)
    view = tmp_path / "view"
    (view / "nested").mkdir(parents=True)
    _write(view / "SOURCE.txt", "frozen source\n")
    (view / "nested/data.bin").write_bytes(b"\x00frozen\xff")
    (view / "link").symlink_to("SOURCE.txt")
    (view / "readonly.txt").write_text("read only\n")
    (view / "readonly.txt").chmod(0o444)
    before = _manifest(view)

    output = tmp_path / "out"
    receipt = output / "state/child-receipt.json"
    worker = worker_module.JiuwenWorker(
        _binary(tmp_path / "native-worker"), _config(tmp_path / "config.yaml"),
        native_python=sys.executable,
    )
    prompt = "cancel after writes" if outcome == "cancellation" else "complete"
    environment = _environment(runtime, receipt)
    environment.update({
        "LH_NOTICE_CONTEXT": json.dumps({"notice_id": "notice-1", "token": "token-1"}),
        "LH_INVOCATION_ID": "invocation-1",
    })
    if outcome == "cancellation":
        with pytest.raises(worker_module.WorkerExecutionError) as failure:
            _run(worker_module, worker, output=output, view=view, stage=stage,
                 prompt=prompt, environment=environment)
        assert failure.value.code == "INTERRUPTED"
    else:
        result = _run(worker_module, worker, output=output, view=view, stage=stage,
                      prompt=prompt, environment=environment)
        assert result.status == "completed"

    assert _manifest(view) == before, "the frozen view changed"
    state = output / "state"
    observed = json.loads(receipt.read_text(encoding="utf-8"))
    assert observed["view_read"] == "frozen source\n"
    assert observed["uid"] == os.geteuid()
    assert observed["initial_cwd"] == str(state)
    assert (state / "logs/pre-activation.log").is_file()
    assert _under(observed["db_path"], state)
    assert _under(observed["wal_path"], state) and observed["wal_exists"]
    assert _under(observed["shm_path"], state) and observed["shm_exists"]
    assert observed["journal_mode"].lower() == "wal"
    assert (state / "stage-writes/logs/logs/run/child.log.1").is_file()
    assert (state / "openjiuwen-logs/configured.json").is_file()
    configured = json.loads((state / "openjiuwen-logs/configured.json").read_text())
    assert Path(configured["log_path"]).is_absolute()
    assert _under(configured["log_path"], state)

    request = json.loads((output / "request.json").read_text())
    assert request["workspace"]["cwd"] == str(state)
    assert request["workspace"]["project_dir"] == str(view)
    assert request["workspace"]["trusted_dirs"] == [str(view)]

    metadata = json.loads((output / "metadata.json").read_text())
    assert metadata["stage_writes"]["read_only"] is True
    assert metadata["stage_writes"]["state_dir"] == str(state)
    assert metadata["command"][-3:] == [
        "jiuwenswarm.long_horizon.native_entry", "--run-json", "-",
    ]
    assert metadata["stage_state_ownership"]["directory_mode"] == "0700"
    assert metadata["stage_state_ownership"]["file_mode"] == "0600"
    if outcome == "cancellation":
        cancellation = metadata["cancellation"]
        assert cancellation["request_id"] == metadata["request_id"]
        assert cancellation["invocation_id"] == "invocation-1"
        assert cancellation["notice_context"]["notice_id"] == "notice-1"
        assert cancellation["notice_context"]["token"] == "[REDACTED]"
        assert cancellation["notice_context"]["token_sha256"] == hashlib.sha256(b"token-1").hexdigest()
        assert cancellation["terminal_status"] == "cancelled"
        assert cancellation["terminal_result_sha256"]
        observed = cancellation["cancellation_observed_at_unix"]
        exited = cancellation["process_exit_observed_at_unix"]
        assert observed is not None
        assert exited is not None
        # The first terminal-event observation survives cleanup and collection.
        assert observed <= exited <= metadata["finished_at_unix"]
        terminal = json.loads((output / "terminal-result.json").read_text())
        assert cancellation["terminal_result_sha256"] == worker_module._object_sha256(terminal)
        assert cancellation["terminal_timestamp_unix"] == terminal.get("finished_at_unix")
    else:
        assert "cancellation" not in metadata
    assert not any(
        name in {"memory.db", "memory.db-wal", "memory.db-shm", "logs"}
        for name in _manifest(view)
    )


@pytest.mark.parametrize("exit_code", [130, 137, 143, -2, -9, -15])
def test_exit_codes_do_not_imply_cancellation(worker_module, tmp_path, exit_code):
    runtime = _fake_runtime(tmp_path)
    view = tmp_path / "view"
    view.mkdir()
    _write(view / "SOURCE.txt", "frozen source\n")
    output = tmp_path / "out"
    worker = worker_module.JiuwenWorker(
        _binary(tmp_path / "native-worker"), _config(tmp_path / "config.yaml"),
        native_python=sys.executable,
    )
    terminal_time = 1_600_000_000.0 + abs(exit_code)
    environment = _environment(runtime, output / "state/child-receipt.json")
    environment.update({
        "STAGE_WRITE_EXIT_CODE": str(exit_code),
        "STAGE_WRITE_TERMINAL_TIMESTAMP": str(terminal_time),
        "LH_NOTICE_CONTEXT": json.dumps({"notice_id": "notice-1", "token": "token-1"}),
        "LH_INVOCATION_ID": "invocation-1",
    })
    with pytest.raises(worker_module.WorkerExecutionError) as failure:
        _run(worker_module, worker, output=output, view=view,
             stage="clarification-proposal", prompt="complete", environment=environment)
    assert failure.value.code != "INTERRUPTED"
    metadata = json.loads((output / "metadata.json").read_text())
    assert metadata["actual_exit_code"] == exit_code
    assert metadata.get("cancellation") is None
    terminal = json.loads((output / "terminal-result.json").read_text())
    assert terminal["status"] == "failed"
    assert terminal["finished_at_unix"] == terminal_time
    # Exit time and collection time are separate from the fixed terminal stamp.
    assert metadata["finished_at_unix"] != terminal_time
    assert metadata["actual_exit_code"] != 0


def test_cancellation_observation_precedes_cleanup(worker_module, tmp_path):
    runtime = _fake_runtime(tmp_path)
    view = tmp_path / "view"
    view.mkdir()
    _write(view / "SOURCE.txt", "frozen source\n")
    terminal_time = 1_300_000_000.25

    def environment(output: Path, barrier: Path) -> dict[str, str]:
        values = _environment(runtime, output / "state/child-receipt.json")
        values.update({
            "STAGE_WRITE_EXIT_BARRIER": str(barrier),
            "STAGE_WRITE_TERMINAL_TIMESTAMP": str(terminal_time),
            "LH_NOTICE_CONTEXT": json.dumps({"notice_id": "notice-1", "token": "token-1"}),
            "LH_INVOCATION_ID": "invocation-1",
        })
        return values

    # Terminal-event path: the child emits a cancelled terminal event, then
    # waits on an explicit barrier while cleanup and collection continue.
    output = tmp_path / "terminal-out"
    barrier = tmp_path / "release-terminal"
    worker = worker_module.JiuwenWorker(
        _binary(tmp_path / "native-worker"), _config(tmp_path / "config.yaml"),
        native_python=sys.executable,
    )
    captured: dict[str, BaseException] = {}

    def run_terminal() -> None:
        try:
            _run(worker_module, worker, output=output, view=view,
                 stage="clarification-proposal", prompt="cancel after writes",
                 environment=environment(output, barrier))
        except BaseException as error:  # noqa: BLE001 - asserted below
            captured["error"] = error

    thread = threading.Thread(target=run_terminal, daemon=True)
    thread.start()
    ready = Path(str(barrier) + ".ready")
    until = time.monotonic() + 10
    while not ready.exists() and time.monotonic() < until:
        time.sleep(0.01)
    assert ready.exists(), "the child never emitted its terminal event"
    assert thread.is_alive(), "the child exited before the cancellation observation"
    barrier.write_text("go\n", encoding="utf-8")
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert isinstance(captured.get("error"), worker_module.WorkerExecutionError)
    metadata = json.loads((output / "metadata.json").read_text())
    cancellation = metadata["cancellation"]
    observed = cancellation["cancellation_observed_at_unix"]
    exited = cancellation["process_exit_observed_at_unix"]
    assert observed is not None and exited is not None
    assert observed <= exited <= metadata["finished_at_unix"]
    assert cancellation["terminal_timestamp_unix"] == terminal_time
    terminal = json.loads((output / "terminal-result.json").read_text())
    assert cancellation["terminal_sequence"] == terminal["sequence"]
    assert cancellation["terminal_result_sha256"] == worker_module._object_sha256(terminal)

    # Task-cancellation path: no cancelled terminal event exists, so only the
    # explicit task cancellation observation may set the cancellation time.
    task_output = tmp_path / "task-out"
    task_barrier = tmp_path / "release-task"
    task_worker = worker_module.JiuwenWorker(
        _binary(tmp_path / "native-worker"), _config(tmp_path / "config.yaml"),
        native_python=sys.executable,
    )

    async def cancelled_task() -> None:
        task = asyncio.create_task(task_worker.run(
            stage="clarification-proposal", prompt="complete", workspace=view,
            output_dir=task_output, read_only=True, timeout_seconds=60,
            environment=environment(task_output, task_barrier),
        ))
        ready = Path(str(task_barrier) + ".ready")
        until = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < until:
            await asyncio.sleep(0.01)
        assert ready.exists(), "the child never emitted its terminal event"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancelled_task())
    task_metadata = json.loads((task_output / "metadata.json").read_text())
    task_cancellation = task_metadata["cancellation"]
    task_observed = task_cancellation["cancellation_observed_at_unix"]
    task_exited = task_cancellation["process_exit_observed_at_unix"]
    assert task_observed is not None and task_exited is not None
    assert task_observed <= task_exited <= task_metadata["finished_at_unix"]
    assert task_cancellation["terminal_status"] == "completed"


def test_state_environment_disables_bytecode_for_every_stage(worker_module, tmp_path):
    writable = worker_module._state_environment(tmp_path, read_only=False)
    read_only = worker_module._state_environment(tmp_path, read_only=True, views=(tmp_path,))
    assert writable["PYTHONDONTWRITEBYTECODE"] == "1"
    assert read_only["PYTHONDONTWRITEBYTECODE"] == "1"
    assert read_only["PYTHONPYCACHEPREFIX"] == str(tmp_path / "pycache")


def test_stage_write_paths_reject_aliases(worker_module, tmp_path):
    runtime = _fake_runtime(tmp_path)
    view = tmp_path / "view"
    view.mkdir()
    _write(view / "SOURCE.txt", "frozen source\n")
    before = _manifest(view)
    worker = worker_module.JiuwenWorker(
        _binary(tmp_path / "native-worker"), _config(tmp_path / "config.yaml"),
        native_python=sys.executable,
    )
    environment = _environment(runtime, tmp_path / "receipt.json")

    state_alias = tmp_path / "state-alias"
    state_alias.mkdir()
    (state_alias / "state").symlink_to(view)
    with pytest.raises(worker_module.WorkerConfigurationError) as failure:
        _run(worker_module, worker, output=state_alias, view=view,
             stage="clarification-proposal", environment=environment)
    assert failure.value.code == "STAGE_PATH_OVERLAP"

    output_inside = view / "out"
    with pytest.raises(worker_module.WorkerConfigurationError) as failure:
        _run(worker_module, worker, output=output_inside, view=view,
             stage="clarification-proposal", environment=environment)
    assert failure.value.code in {"OUTPUT_VIEW_OVERLAP", "STAGE_PATH_OVERLAP"}

    output_parent = tmp_path / "output-parent"
    nested_view = output_parent / "view"
    nested_view.mkdir(parents=True)
    _write(nested_view / "SOURCE.txt", "frozen source\n")
    with pytest.raises(worker_module.WorkerConfigurationError) as failure:
        _run(worker_module, worker, output=output_parent, view=nested_view,
             stage="clarification-proposal", environment=environment)
    assert failure.value.code == "OUTPUT_VIEW_OVERLAP"

    self_alias = tmp_path / "self-alias"
    self_alias.mkdir()
    (self_alias / "state").symlink_to(self_alias)
    with pytest.raises(worker_module.WorkerConfigurationError) as failure:
        _run(worker_module, worker, output=self_alias, view=view,
             stage="clarification-proposal", environment=environment)
    assert failure.value.code == "OUTPUT_INSIDE_STATE"

    assert _manifest(view) == before
    assert not (state_alias / "state").exists() or (state_alias / "state").is_symlink()
    assert not (output_inside / "metadata.json").exists()
    assert not (self_alias / "metadata.json").exists()


def test_stage_state_survives_continuation(worker_module, tmp_path):
    runtime = _fake_runtime(tmp_path)
    view = tmp_path / "view"
    view.mkdir()
    _write(view / "SOURCE.txt", "frozen source\n")
    before = _manifest(view)
    worker = worker_module.JiuwenWorker(
        _binary(tmp_path / "native-worker"), _config(tmp_path / "config.yaml"),
        native_python=sys.executable,
    )
    environment = _environment(runtime, tmp_path / "first/state/child-receipt.json")
    first = _run(worker_module, worker, output=tmp_path / "first", view=view,
                 stage="clarification-proposal", environment=environment)
    first_metadata = json.loads((tmp_path / "first/metadata.json").read_text())
    second = _run(worker_module, worker, output=tmp_path / "second", view=view,
                  stage="clarification-proposal", session_id=first.session_id,
                  environment=environment)
    second_metadata = json.loads((tmp_path / "second/metadata.json").read_text())

    assert second.session_id == first.session_id
    assert second_metadata["session_continuation"] is True
    assert second_metadata["state_dir"] == first_metadata["state_dir"]
    assert second_metadata["session_deadline_unix"] == first_metadata["session_deadline_unix"]
    assert second_metadata["session_deadline_monotonic"] == first_metadata["session_deadline_monotonic"]
    assert second_metadata["session_budget_seconds"] == first_metadata["session_budget_seconds"]
    assert _manifest(view) == before


def test_mutable_stage_behavior_preserved(worker_module, tmp_path):
    view = tmp_path / "workspace"
    view.mkdir()
    binary = tmp_path / "mutable-native"
    _write(binary, (ROOT / "tests" / "lh" / "fixtures" / "stage-writes-mutable-native.py").read_text())
    binary.chmod(0o700)
    worker = worker_module.JiuwenWorker(binary, _config(tmp_path / "config.yaml"))
    result = _run(worker_module, worker, output=tmp_path / "out", view=view,
                  stage="native-code", read_only=False, environment={
                      "PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path / "home"),
                  })
    assert result.status == "completed"
    assert (view / "memory.db").is_file()
    assert (view / "logs/mutable.log").is_file()
    state = tmp_path / "out/state"
    assert not (state / "stage-writes").exists()
    metadata = json.loads((tmp_path / "out/metadata.json").read_text())
    # Writable stages keep the baseline launch tuple exactly: the launcher is
    # started directly, never replaced by a resolved probe interpreter.
    assert metadata["command"] == [str(binary.resolve()), "--run-json", "-"]
    assert metadata["stage_writes"]["read_only"] is False


def test_tool_free_synthesis_config_disables_compression_recall_only(worker_module, tmp_path):
    """The real per-stage config producer keeps compression but drops recall.

    A tool-free synthesis stage must not let the retained context processor
    register recall_compressed_context before the fail-closed guard. The real
    configured investigation stage keeps the full native context engine.
    """
    runtime = _fake_runtime(tmp_path)
    view = tmp_path / "view"
    view.mkdir()
    _write(view / "SOURCE.txt", "frozen source\n")
    template = _recall_config(tmp_path / "config.yaml")
    original = yaml.safe_load(template.read_text(encoding="utf-8"))
    original_context = original["react"]["context_engine_config"]
    original_recall = copy.deepcopy(original_context["compression_recall_config"])
    original_compression = copy.deepcopy(original_context["compression"])
    original_window = copy.deepcopy(original_context["actual_window_recovery"])
    template_sha = hashlib.sha256(template.read_bytes()).hexdigest()

    # Genuine tool-free synthesis: fresh session, tool_policy=none.
    output = tmp_path / "synthesis"
    receipt = output / "state/child-receipt.json"
    worker = worker_module.JiuwenWorker(
        _binary(tmp_path / "native-worker"), template, native_python=sys.executable,
    )
    environment = _environment(runtime, receipt)
    environment["STAGE_WRITE_TOOL_POLICY_PROOF"] = "none"
    result = _run(worker_module, worker, output=output, view=view,
                  stage="public-verification-ac1-synthesis",
                  tool_policy="none", environment=environment)
    assert result.status == "completed"
    metadata = json.loads((output / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["tool_policy"] == "none"
    assert metadata["tool_policy_proof_sequences"] == [0]
    assert metadata["config_template_sha256"] == template_sha

    for name in ("native-config.before.yaml", "native-config.after.yaml"):
        config = yaml.safe_load((output / name).read_text(encoding="utf-8"))
        assert config["modes"]["code"]["tool_policy"] == "none"
        assert config["models"] == original["models"]
        assert config["react"]["context_engine_config"]["compression"] == original_compression
        assert config["react"]["context_engine_config"]["actual_window_recovery"] == original_window
        assert config["react"]["context_engine_config"]["compression_recall_config"] == {
            **original_recall, "enabled": False,
        }
        assert str(config["react"]["context_engine_config"]["context_debug_dir"]).startswith(
            str(output / "state")
        )
        assert str(config["memory"]["store"]["path"]).startswith(str(output / "state"))

    before_sha = hashlib.sha256((output / "native-config.before.yaml").read_bytes()).hexdigest()
    after_sha = hashlib.sha256((output / "native-config.after.yaml").read_bytes()).hexdigest()
    assert metadata["requested_config_sha256"] == before_sha
    assert metadata["archived_config_sha256"] == after_sha

    # A continuation reuses the previous effective config unchanged.
    continuation = tmp_path / "synthesis-continuation"
    resumed = _run(worker_module, worker, output=continuation, view=view,
                   stage="public-verification-ac1-synthesis", session_id=result.session_id,
                   tool_policy="none", environment=environment)
    continuation_metadata = json.loads(
        (continuation / "metadata.json").read_text(encoding="utf-8")
    )
    assert resumed.session_id == result.session_id
    assert continuation_metadata["session_continuation"] is True
    continuation_config = yaml.safe_load(
        (continuation / "native-config.before.yaml").read_text(encoding="utf-8")
    )
    assert continuation_config["react"]["context_engine_config"][
        "compression_recall_config"
    ] == {**original_recall, "enabled": False}

    # A normal configured investigation is read-only but keeps recall enabled.
    investigation = tmp_path / "investigation"
    investigation_worker = worker_module.JiuwenWorker(
        _binary(tmp_path / "native-worker"), template, native_python=sys.executable,
    )
    investigation_environment = _environment(
        runtime, investigation / "state/child-receipt.json"
    )
    investigated = _run(worker_module, investigation_worker, output=investigation, view=view,
                        stage="public-verification-ac1-investigation",
                        tool_policy="configured", environment=investigation_environment)
    assert investigated.status == "completed"
    investigation_metadata = json.loads(
        (investigation / "metadata.json").read_text(encoding="utf-8")
    )
    assert investigation_metadata["tool_policy"] == "configured"
    investigation_config = yaml.safe_load(
        (investigation / "native-config.before.yaml").read_text(encoding="utf-8")
    )
    assert investigation_config["modes"]["code"]["tool_policy"] == "configured"
    assert investigation_config["react"]["context_engine_config"][
        "compression_recall_config"
    ] == original_recall
    assert investigation_config["react"]["context_engine_config"]["compression"] == original_compression
    assert investigation_config["react"]["context_engine_config"]["actual_window_recovery"] == original_window




def test_native_main_protocol_and_error_behavior(tmp_path):
    runtime = tmp_path / "runtime"
    package = runtime / "jiuwenswarm"
    _write(package / "__init__.py")
    _write(package / "long_horizon/__init__.py")
    (package / "long_horizon/native_entry.py").write_bytes(NATIVE_ENTRY.read_bytes())
    for name in ("channels", "channels/process_cli"):
        _write(package / name / "__init__.py")
    _write(
        package / "channels/process_cli/machine_entry.py",
        "from contextlib import contextmanager\n"
        "import os\n"
        "\n"
        "@contextmanager\n"
        "def protocol_stdout():\n"
        "    with open(os.environ['DELTA5_EVENTS'], 'a') as stream:\n"
        "        stream.write('context-enter\\n')\n"
        "    try:\n"
        "        yield None\n"
        "    finally:\n"
        "        with open(os.environ['DELTA5_EVENTS'], 'a') as stream:\n"
        "            stream.write('context-exit\\n')\n",
    )
    _write(
        package / "channels/process_cli/machine.py",
        "import os\n\n"
        "class _MachineRun:\n"
        "    def execute(self):\n"
        "        with open(os.environ['DELTA5_MISSING_FILE'], 'rb') as stream:\n"
        "            return stream.read()\n",
    )
    for name in ("openjiuwen", "openjiuwen/core", "openjiuwen/core/common",
                 "openjiuwen/core/common/logging"):
        _write(runtime / name / "__init__.py")
    _write(
        runtime / "openjiuwen/core/common/logging/log_config.py",
        "def configure_log_config(config):\n    pass\n",
    )
    missing = tmp_path / "missing" / "file.txt"
    counter = 0

    def run(main_source: str, *, bootstrap_failure: bool = False):
        nonlocal counter
        counter += 1
        state = tmp_path / f"state-{counter}"
        state.mkdir()
        if bootstrap_failure:
            (state / "tmp").write_text("not a directory\n")
        events = tmp_path / f"events-{counter}.txt"
        _write(package / "channels/process_cli/main.py", main_source)
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(runtime),
            "JIUWENSWARM_DATA_DIR": str(state),
            "JIUWENSWARM_STAGE_READ_ONLY": "1",
            "JIUWENSWARM_STAGE_VIEW_DIRS": str(tmp_path / "view"),
            "HOME": str(tmp_path / "home"),
            "TMPDIR": str(tmp_path / "tmp"),
            "DELTA5_MISSING_FILE": str(missing),
            "DELTA5_EVENTS": str(events),
        }
        (tmp_path / "home").mkdir(exist_ok=True)
        (tmp_path / "view").mkdir(exist_ok=True)
        completed = subprocess.run(
            [sys.executable, str(package / "long_horizon/native_entry.py")],
            env=environment, capture_output=True, text=True, timeout=120,
        )
        observed = events.read_text().splitlines() if events.exists() else []
        return completed, observed

    native_marker = (
        "def main():\n"
        "    with open(os.environ['DELTA5_EVENTS'], 'a') as stream:\n"
        "        stream.write('native\\n')\n"
    )

    # A bootstrap OSError is reported once and never reaches native execution.
    bootstrap, events = run(native_marker + "    raise SystemExit(0)\n", bootstrap_failure=True)
    assert bootstrap.returncode == 1
    assert bootstrap.stderr.count("NATIVE_OSERROR_DIAGNOSTIC") == 1
    assert "context=native_entry_prepare_stage" in bootstrap.stderr
    assert "Native stage write boundary is invalid." in bootstrap.stderr
    assert "Native stage logging bootstrap failed." not in bootstrap.stderr
    assert events == []

    # A direct native OSError escapes unchanged and is reported exactly once by
    # the dedicated native_main wrapper, with no bootstrap label.
    direct, events = run(
        "import os\n" + native_marker
        + "    raise OSError(2, 'direct failure', '/direct/missing')\n")
    assert direct.returncode != 0
    assert direct.stderr.count("NATIVE_OSERROR_DIAGNOSTIC") == 1
    assert "context=native_main" in direct.stderr
    assert "Native stage logging bootstrap failed." not in direct.stderr
    assert "direct failure" in direct.stderr
    assert "'/direct/missing'" in direct.stderr
    assert events == ["context-enter", "context-exit", "native"]

    # A swallowed execute OSError is reported once before the native handler
    # converts it to its own message.
    swallowed, events = run(
        "import os\nfrom .machine import _MachineRun\n" + native_marker
        + "    try:\n"
        "        _MachineRun().execute()\n"
        "    except OSError:\n"
        "        print('Command I/O failed')\n")
    assert swallowed.returncode == 0
    assert swallowed.stdout.strip() == "Command I/O failed"
    assert swallowed.stderr.count("NATIVE_OSERROR_DIAGNOSTIC") == 1
    assert "context=machine_run_execute" in swallowed.stderr
    assert "context=native_main" not in swallowed.stderr
    assert "Native stage logging bootstrap failed." not in swallowed.stderr
    assert events == ["context-enter", "context-exit", "native"]

    # An escaping execute OSError is not reported a second time by the outer
    # native_main wrapper.
    escaping, events = run(
        "import os\nfrom .machine import _MachineRun\n" + native_marker
        + "    _MachineRun().execute()\n")
    assert escaping.returncode != 0
    assert escaping.stderr.count("NATIVE_OSERROR_DIAGNOSTIC") == 1
    assert "context=machine_run_execute" in escaping.stderr
    assert "context=native_main" not in escaping.stderr
    assert "Native stage logging bootstrap failed." not in escaping.stderr
    assert events == ["context-enter", "context-exit", "native"]

    # Ordinary exceptions and SystemExit keep their native behavior and are
    # not mislabelled as logging-bootstrap failures.
    ordinary, events = run(
        "import os\n" + native_marker + "    raise ValueError('ordinary failure')\n")
    assert ordinary.returncode != 0
    assert "NATIVE_OSERROR_DIAGNOSTIC" not in ordinary.stderr
    assert "Native stage logging bootstrap failed." not in ordinary.stderr
    assert "ValueError: ordinary failure" in ordinary.stderr
    assert events == ["context-enter", "context-exit", "native"]

    exit_code, events = run("import os\n" + native_marker + "    raise SystemExit(7)\n")
    assert exit_code.returncode == 7
    assert exit_code.stdout == ""
    assert "NATIVE_OSERROR_DIAGNOSTIC" not in exit_code.stderr
    assert "Native stage logging bootstrap failed." not in exit_code.stderr
    assert events == ["context-enter", "context-exit", "native"]


