"""Public policy inventory and fail-closed boundary tests, without inference."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from jiuwenswarm.long_horizon.benchmark_write_guard import (
    BenchmarkWriteGuard, WriteGuardError, public_test_paths, reserved_names,
)


class PublicWritePolicyTests(unittest.TestCase):
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
