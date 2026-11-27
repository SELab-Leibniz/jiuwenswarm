"""Public policy inventory and fail-closed boundary tests, without inference."""

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from jiuwenswarm.long_horizon.benchmark_write_guard import (
    BenchmarkWriteGuard, WriteGuardError, public_test_directories, public_test_paths, reserved_names,
)
from jiuwenswarm.long_horizon.worker import JiuwenWorker, _state_environment
from jiuwenswarm.long_horizon import benchmark_write_guard as guard_module


class PublicWritePolicyTests(unittest.TestCase):
    def test_untracked_public_tree_content_is_audited_without_rollback(self):
        # Exercise real file snapshots/cleanup on hosts without Linux DAC.
        # Privilege setup is stubbed here; LinuxWriteGuardTests covers setpriv.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            workspace, state = root / "workspace", root / "state"
            workspace.mkdir()
            state.mkdir()
            (workspace / "tests/nested").mkdir(parents=True)
            (workspace / "tests/original.py").write_text("public baseline\n")
            for args in (["init", "-q"], ["add", "tests/original.py"]):
                subprocess.run(["git", "-C", str(workspace), *args], stdin=subprocess.DEVNULL,
                               capture_output=True, check=True)
            extra = workspace / "tests/nested/untracked.txt"
            extra.write_text("public fixture\n")
            reserved = workspace / "tests/.pact"
            reserved.mkdir()
            reserved_file = reserved / ".git"
            reserved_file.write_text("public fixture with reserved names\n")
            source = workspace / "source.py"
            source.write_text("before\n")
            inventory = public_test_paths(workspace)
            self.assertNotIn("tests/nested/untracked.txt", inventory)
            guard = BenchmarkWriteGuard(workspace, state, inventory)
            before = extra.stat().st_mode
            def control_path(value):
                if str(value) == "/usr/bin/setpriv":
                    # This control stubs privilege setup; LinuxWriteGuardTests
                    # exercises the real executable and UID isolation.
                    return Path(sys.executable)
                return root / "guard.lock" if value == "/opt/jiuwenswarm-bench/.native-write-guard.lock" else Path(value)
            # These audit controls use one sentinel per namespace on case-insensitive
            # filesystems; the full Linux case-variant contract is tested separately.
            namespaces = tuple(sorted({name.casefold() for name in reserved_names()}))
            with patch.object(guard_module, "reserved_names", return_value=namespaces), \
                 patch.object(guard_module.sys, "platform", "linux"), \
                 patch.object(guard_module.os, "geteuid", return_value=0), \
                 patch.object(guard_module.platform, "machine", return_value="x86_64"), \
                 patch.object(guard_module, "_pidfd_open", side_effect=lambda _: os.open(os.devnull, os.O_RDONLY)), \
                 patch.object(guard_module, "Path", side_effect=control_path), \
                 patch.object(guard_module.os, "chown") as chown, \
                 patch.object(guard, "_traversable_parents"), \
                 patch.object(guard, "_processes", return_value=[]):
                try:
                    guard.prepare()
                    protected_mode = extra.stat().st_mode
                    reserved_file.chmod(0o600)
                    reserved_file.write_text("retained reserved-name violation\n")
                    source.write_text("candidate source\n")
                    (workspace / "checks").mkdir()
                    (workspace / "checks/new.py").write_text("candidate check\n")
                    extra.chmod(before | 0o200)  # Trusted-controller tamper on non-root hosts.
                    extra.write_text("retained content violation\n")
                finally:
                    receipt = guard.finish()
            self.assertEqual(receipt["status"], "integrity_failed", receipt)
            self.assertIn("tests/nested/untracked.txt", receipt["violations"])
            self.assertIn("tests/.pact/.git", receipt["violations"])
            self.assertFalse(receipt["protected_content_unchanged"])
            self.assertFalse(receipt["candidate_filtering"])
            self.assertEqual(protected_mode & 0o222, 0)
            chown.assert_any_call(extra, 0, 0, follow_symlinks=False)
            self.assertEqual(extra.stat().st_mode, before)
            self.assertEqual(extra.read_text(), "retained content violation\n")
            self.assertEqual(source.read_text(), "candidate source\n")
            self.assertEqual((workspace / "checks/new.py").read_text(), "candidate check\n")

    def test_inventory_uses_only_public_tracked_baseline_files(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            names = ("source.py", "src/feature.test-d.ts", "testdata/examples.go",
                     "tests/original.py", "sample_test.go", "checks/regression.py")
            for name in names:
                path = source / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("public fixture\n")
            for args in (["init", "-q"], ["add", "."]):
                subprocess.run(["git", "-C", str(source), *args], stdin=subprocess.DEVNULL,
                               capture_output=True, check=True)
            (source / "tests/untracked.py").write_text("not frozen baseline\n")
            self.assertEqual(public_test_paths(source),
                             ("sample_test.go", "src/feature.test-d.ts", "testdata/examples.go", "tests/original.py"))

    def test_all_core_case_variants_are_reserved(self):
        names = reserved_names()
        self.assertEqual(len(names), len(set(names)))
        self.assertIn(".PaCt", names)
        self.assertIn(".PaCt-Io", names)
        self.assertEqual(sum(name.casefold() == ".pact" for name in names), 16)
        self.assertEqual(sum(name.casefold() == ".pact-io" for name in names), 64)

    def test_workspace_policy_and_guard_receipt_share_inventory_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "tests").mkdir()
            for name in ("one.py", "中文.py"):
                (source / "tests" / name).write_text("public fixture\n")
            for args in (["init", "-q"], ["add", "."]):
                subprocess.run(["git", "-C", str(source), *args], stdin=subprocess.DEVNULL,
                               capture_output=True, check=True)
            worker = JiuwenWorker(source / "native", source / "config", benchmark_write_guard=True)
            worker.benchmark_source_root = source
            policy = worker.benchmark_workspace_policy
            receipt = BenchmarkWriteGuard(source, source.parent / "state", public_test_paths(source)).receipt
            self.assertEqual(receipt["public_inventory_sha256"], policy["public_inventory_sha256"])
            self.assertEqual(receipt["policy"], policy["native_write_guard"])
            self.assertEqual(receipt["protected_public_directories"], policy["protected_public_directories"])
            self.assertFalse(receipt["public_directory_additions_allowed"])

    def test_public_directory_roots_do_not_reserve_new_checks_or_source(self):
        self.assertEqual(public_test_directories((
            "src/feature.test-d.ts", "sample_test.go", "checkers/testdata/existing/positive.go",
            "tests/fixtures/original.py", "other/TESTS/unit.py", "checks/regression.py",
        )), ("checkers/testdata", "other/TESTS", "tests"))

    def test_unsupported_host_does_not_modify_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            workspace.mkdir(mode=0o700)
            source = workspace / "source.py"
            source.write_text("unchanged\n")
            before = (source.stat().st_uid, source.stat().st_gid, source.stat().st_mode, source.read_bytes())
            guard = BenchmarkWriteGuard(workspace, Path(directory) / "state", ())
            with patch("jiuwenswarm.long_horizon.benchmark_write_guard.sys.platform", "unsupported"):
                with self.assertRaises(WriteGuardError):
                    guard.prepare()
            receipt = guard.finish()
            self.assertEqual(receipt["status"], "not_prepared_cleaned")
            self.assertEqual(before, (source.stat().st_uid, source.stat().st_gid,
                                      source.stat().st_mode, source.read_bytes()))
            self.assertEqual(list(workspace.iterdir()), [source])


@unittest.skipUnless(sys.platform == "linux" and os.geteuid() == 0
                     and Path("/opt/jiuwenswarm-bench").is_dir(), "dedicated Linux runtime control")
class LinuxWriteGuardTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "tests").mkdir()
        self.test = self.workspace / "tests/original.py"
        self.test.write_text("original\n")
        (self.workspace / "source.py").write_text("before\n")
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        self.guard = BenchmarkWriteGuard(self.workspace, self.state, ("tests/original.py",))
        self.addCleanup(self.guard.finish)

    def test_real_shell_and_interpreter_preserve_tests_but_allow_source(self):
        mode = self.test.stat().st_mode
        self.guard.prepare()
        script = '''
import json, os
from pathlib import Path
root = Path(os.environ["CONTROL_WORKSPACE"])
target = root / "tests/original.py"
blocked = []
replacement = root / "replacement.py"
replacement.write_text("bad")
for action in (lambda: target.write_text("bad"), lambda: target.unlink(),
               lambda: replacement.replace(target), lambda: target.chmod(0o666),
               lambda: (root / ".PaCt/checks").mkdir(parents=True), lambda: os.setuid(0)):
    try:
        action()
    except (PermissionError, FileExistsError, NotADirectoryError):
        blocked.append(True)
    else:
        blocked.append(False)
replacement.unlink()
(root / "source.py").write_text("after\\n")
(root / "checks").mkdir()
(root / "checks/new.py").write_text("new check\\n")
print(json.dumps(blocked))
'''
        environment = self.guard.environment(dict(os.environ, CONTROL_WORKSPACE=str(self.workspace)))
        result = subprocess.run([*self.guard.command_prefix, sys.executable, "-c", script],
                                env=environment, stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, timeout=15, check=True)
        self.assertEqual(json.loads(result.stdout), [True] * 6)
        shell = subprocess.run([*self.guard.command_prefix, "/bin/sh", "-c",
                                "printf bad > tests/original.py"], cwd=self.workspace,
                               stdin=subprocess.DEVNULL, capture_output=True, check=False)
        self.assertNotEqual(shell.returncode, 0)
        receipt = self.guard.finish()
        self.assertEqual(receipt["status"], "complete")
        self.assertEqual(self.test.read_text(), "original\n")
        self.assertEqual(self.test.stat().st_mode, mode)
        self.assertEqual((self.workspace / "source.py").read_text(), "after\n")
        self.assertEqual((self.workspace / "checks/new.py").stat().st_uid, 0)
        self.assertFalse(any((self.workspace / name).exists() for name in reserved_names()))
        self.assertIs(self.guard.finish(), receipt)

    def test_escaped_session_is_stopped_before_permissions_restore(self):
        self.guard.prepare()
        child = subprocess.Popen([*self.guard.command_prefix, sys.executable, "-c",
                                  "import time; print('ready', flush=True); time.sleep(60)"], start_new_session=True,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL)
        self.addCleanup(child.wait, 5)
        self.assertEqual(child.stdout.readline(), b"ready\n")
        receipt = self.guard.finish()
        self.assertEqual(receipt["status"], "complete")
        self.assertEqual(child.wait(timeout=5), -9)
        self.assertEqual(receipt["residual_native_processes_stopped"], 1)
        self.assertEqual(self.test.stat().st_uid, 0)

    def test_new_public_fixture_entries_are_blocked_but_separate_checks_are_writable(self):
        fixtures = self.workspace / "checkers/testdata/existing"
        fixtures.mkdir(parents=True)
        (fixtures / "positive.go").write_text("public baseline\n")
        self.guard = BenchmarkWriteGuard(self.workspace, self.state,
                                        ("tests/original.py", "checkers/testdata/existing/positive.go"))
        self.addCleanup(self.guard.finish)
        before = {p: (p.stat().st_uid, p.stat().st_gid, p.stat().st_mode)
                  for p in (fixtures.parent, fixtures, self.workspace / "tests")}
        self.guard.prepare()
        script = '''
import json, os
from pathlib import Path
r = Path(os.environ["CONTROL_WORKSPACE"])
replacement = r / "replacement"
replacement.mkdir()
blocked = []
for action in (lambda: (r / "checkers/testdata/new-checker").mkdir(),
               lambda: (r / "checkers/testdata/existing/new_case.go").write_text("bad"),
               lambda: (r / "tests/new_case.py").write_text("bad"),
               lambda: replacement.replace(r / "checkers/testdata/existing"),
               lambda: (r / "checkers/testdata/alias").symlink_to(replacement),
               lambda: (r / "checkers/testdata").chmod(0o777)):
    try:
        action()
    except (PermissionError, FileExistsError):
        blocked.append(True)
    else:
        blocked.append(False)
replacement.rmdir()
(r / "source.py").write_text("after\\n")
(r / "checks").mkdir()
(r / "checks/new_case_test.go").write_text("new check\\n")
print(json.dumps(blocked))
'''
        result = subprocess.run([*self.guard.command_prefix, sys.executable, "-c", script],
                                env=self.guard.environment(dict(os.environ, CONTROL_WORKSPACE=str(self.workspace))),
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15, check=True)
        self.assertEqual(json.loads(result.stdout), [True] * 6)
        shell = subprocess.run([*self.guard.command_prefix, "/bin/sh", "-c",
                                "mkdir -p checkers/testdata/new-checker && printf bad > tests/new_case.py"],
                               cwd=self.workspace, stdin=subprocess.DEVNULL, capture_output=True, check=False)
        self.assertNotEqual(shell.returncode, 0)
        receipt = self.guard.finish()
        self.assertEqual(receipt["status"], "complete")
        self.assertTrue(receipt["protected_directory_entries_unchanged"])
        self.assertEqual(receipt["protected_public_directories"], ["checkers/testdata", "tests"])
        self.assertFalse((fixtures.parent / "new-checker").exists())
        self.assertEqual((fixtures / "positive.go").read_text(), "public baseline\n")
        self.assertTrue((self.workspace / "checks/new_case_test.go").is_file())
        for path, identity in before.items():
            self.assertEqual((path.stat().st_uid, path.stat().st_gid, path.stat().st_mode), identity)

    def test_controller_addition_is_detected_and_retained_without_filtering(self):
        self.guard.prepare()
        addition = self.workspace / "tests/controller-added.py"
        addition.write_text("must remain for audit\n")
        receipt = self.guard.finish()
        self.assertEqual(receipt["status"], "integrity_failed")
        self.assertIn("tests/<entries>", receipt["violations"])
        self.assertFalse(receipt["candidate_filtering"])
        self.assertEqual(addition.read_text(), "must remain for audit\n")

    def test_preexisting_untracked_public_file_is_readonly_and_audited(self):
        subprocess.run(["git", "init", "-q", str(self.workspace)], check=True,
                       stdin=subprocess.DEVNULL, capture_output=True)
        subprocess.run(["git", "-C", str(self.workspace), "add", "tests/original.py"], check=True,
                       stdin=subprocess.DEVNULL, capture_output=True)
        extra = self.workspace / "tests/untracked.txt"
        extra.write_text("untracked public fixture\n")
        inventory = public_test_paths(self.workspace)
        self.assertNotIn("tests/untracked.txt", inventory)
        self.guard.public_paths = inventory
        before = (extra.stat().st_uid, extra.stat().st_gid, extra.stat().st_mode)
        self.guard.prepare()
        script = '''
from pathlib import Path
import sys
target = Path(sys.argv[1])
try:
    target.write_text("tampered")
except PermissionError:
    print("blocked")
else:
    print("modified")
'''
        result = subprocess.run([*self.guard.command_prefix, sys.executable, "-c", script, str(extra)],
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15, check=True)
        receipt = self.guard.finish()
        self.assertEqual(result.stdout.strip(), "blocked", receipt)
        self.assertEqual(receipt["status"], "complete")
        self.assertEqual(receipt["protected_file_count"], 2)
        self.assertEqual(extra.read_text(), "untracked public fixture\n")
        self.assertEqual((extra.stat().st_uid, extra.stat().st_gid, extra.stat().st_mode), before)

    def test_untracked_public_content_violation_is_detected_and_retained(self):
        extra = self.workspace / "tests/untracked.txt"
        extra.write_text("untracked public fixture\n")
        self.guard.prepare()
        extra.write_text("must remain for audit\n")
        receipt = self.guard.finish()
        self.assertEqual(receipt["status"], "integrity_failed")
        self.assertIn("tests/untracked.txt", receipt["violations"])
        self.assertFalse(receipt["protected_content_unchanged"])
        self.assertFalse(receipt["candidate_filtering"])
        self.assertEqual(extra.read_text(), "must remain for audit\n")

    def test_worker_persists_v2_receipt_matching_workspace_policy(self):
        subprocess.run(["git", "init", "-q", str(self.workspace)], check=True,
                       stdin=subprocess.DEVNULL, capture_output=True)
        (self.workspace / "tests/second.py").write_text("second public baseline\n")
        subprocess.run(["git", "-C", str(self.workspace), "add", "tests"], check=True,
                       stdin=subprocess.DEVNULL, capture_output=True)
        extra = self.workspace / "tests/untracked.txt"
        extra.write_text("untracked public fixture\n")
        executable = self.root / "native-control"
        executable.write_text(f"#!{sys.executable}\n" + '''
import json, sys
from pathlib import Path
request = json.load(sys.stdin)
r = Path(request["workspace"]["cwd"])
assert "read-only" in request["input"] and "checks/" in request["input"]
try:
    (r / "tests/untracked.txt").write_text("tampered")
except PermissionError:
    pass
else:
    raise AssertionError("public file remained writable")
(r / "source.py").write_text("candidate source\\n")
(r / "checks").mkdir()
(r / "checks/regression.py").write_text("candidate check\\n")
print(json.dumps(dict(schema_version="0.1", type="result", sequence=0,
    request_id=request["request_id"], session_id="guard-control", status="completed",
    exit_code=0, output="done", error=None, usage={})))
''')
        executable.chmod(0o755)
        config = self.root / "config.yaml"
        config.write_text("models: {}\n")
        worker = JiuwenWorker(executable, config, benchmark_write_guard=True)
        worker.benchmark_source_root = self.workspace
        output = self.root / "worker-output"
        result = asyncio.run(worker.run(stage="guard-control", prompt="exercise public source",
                                        workspace=self.workspace, output_dir=output))
        self.assertEqual(result.status, "completed")
        metadata = json.loads((output / "metadata.json").read_text())
        receipt = json.loads((output / "native-write-guard.json").read_text())
        policy = worker.benchmark_workspace_policy
        self.assertEqual(metadata["native_write_guard"], receipt)
        self.assertEqual(receipt["status"], "complete")
        self.assertEqual(receipt["policy"], policy["native_write_guard"])
        self.assertEqual(receipt["public_inventory_sha256"], policy["public_inventory_sha256"])
        self.assertEqual(receipt["public_inventory_count"], 2)
        self.assertEqual(receipt["protected_file_count"], 3)
        self.assertEqual(extra.read_text(), "untracked public fixture\n")
        self.assertEqual((self.workspace / "source.py").read_text(), "candidate source\n")
        self.assertEqual((self.workspace / "checks/regression.py").read_text(), "candidate check\n")

    def test_public_directory_alias_fails_closed(self):
        alias = self.workspace / "other"
        alias.mkdir()
        (alias / "public.py").write_text("public baseline\n")
        linked = self.workspace / "testdata"
        linked.symlink_to(alias, target_is_directory=True)
        guard = BenchmarkWriteGuard(self.workspace, self.state, ("testdata",))
        self.addCleanup(guard.finish)
        with self.assertRaisesRegex(WriteGuardError, "missing or aliased"):
            guard.prepare()
        self.assertEqual(guard.finish()["status"], "not_prepared_cleaned")
        self.assertTrue(linked.is_symlink())

    def test_untracked_public_hardlink_fails_closed(self):
        extra = self.workspace / "tests/untracked.txt"
        os.link(self.workspace / "source.py", extra)
        before = extra.stat().st_mode
        with self.assertRaisesRegex(WriteGuardError, "unaliased regular file"):
            self.guard.prepare()
        receipt = self.guard.finish()
        self.assertEqual(receipt["status"], "not_prepared_cleaned")
        self.assertEqual(extra.stat().st_mode, before)
        self.assertEqual(extra.read_text(), "before\n")

    def test_partial_prepare_restores_files_without_removing_existing_namespace(self):
        existing = self.workspace / ".pact"
        existing.mkdir()
        marker = existing / "original"
        marker.write_text("owned by controller\n")
        before = self.test.stat()
        with self.assertRaises(WriteGuardError):
            self.guard.prepare()
        receipt = self.guard.finish()
        self.assertEqual(receipt["status"], "not_prepared_cleaned")
        self.assertEqual(self.test.stat().st_mode, before.st_mode)
        self.assertEqual(marker.read_text(), "owned by controller\n")
        self.assertEqual(self.test.read_text(), "original\n")

    def test_external_graph_journal_remains_writable_and_restores_ownership(self):
        journal = self.root / "memory-calls.jsonl"
        journal.write_text('public preflight\n')
        journal.chmod(0o600)
        self.guard.writable_journals = (journal,)
        self.guard.prepare()
        subprocess.run([*self.guard.command_prefix, sys.executable, "-c",
                        "import sys; open(sys.argv[1], 'a').write('native query\\n')", str(journal)],
                       stdin=subprocess.DEVNULL, capture_output=True, timeout=15, check=True)
        receipt = self.guard.finish()
        self.assertEqual(receipt["status"], "complete")
        self.assertEqual(journal.read_text(), "public preflight\nnative query\n")
        self.assertEqual(journal.stat().st_uid, 0)
        self.assertEqual(journal.stat().st_mode & 0o777, 0o600)

    def test_root_import_does_not_poison_later_mutable_extension_scan(self):
        shared = self.root / "shared"
        package = shared / "extension_fixture"
        package.mkdir(parents=True, mode=0o755)
        shared.chmod(0o755)
        module = package / "__init__.py"
        module.write_text("value = 1\n")
        module.chmod(0o644)
        root_state = self.root / "readonly-state"
        root_state.mkdir()
        environment = dict(os.environ, PYTHONPATH=str(shared))
        environment.update(_state_environment(root_state))
        subprocess.run([sys.executable, "-c", "import os; os.umask(0o077); import extension_fixture"],
                       env=environment, stdin=subprocess.DEVNULL, capture_output=True, check=True)
        self.guard.prepare()
        environment.update(_state_environment(self.state))
        environment = self.guard.environment(environment)
        script = "import extension_fixture; from pathlib import Path; import sys; " \
                 "[(p/'extension.yaml').exists() for p in Path(sys.argv[1]).iterdir() if p.is_dir()]"
        result = subprocess.run([*self.guard.command_prefix, sys.executable, "-c", script, str(package)],
                                env=environment, stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, timeout=15, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((package / "__pycache__").exists())
        self.assertEqual(environment["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertFalse(any(root_state.rglob("*.pyc")))
        self.assertFalse(any(self.state.rglob("*.pyc")))
        self.assertEqual(self.guard.finish()["status"], "complete")

    def test_unconfirmed_process_exit_retains_protection(self):
        self.guard.prepare()
        with patch.object(self.guard, "_stop_native_processes", return_value=(1, False)):
            receipt = self.guard.finish()
        self.assertEqual(receipt["status"], "integrity_failed")
        self.assertTrue(receipt["protections_retained"])
        self.assertEqual(self.test.stat().st_mode & 0o222, 0)
        self.assertTrue((self.workspace / ".pact").is_file())


if __name__ == "__main__":
    unittest.main()
