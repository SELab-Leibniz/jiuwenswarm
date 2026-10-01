"""Linux DAC isolation for mutable native benchmark roles.

The controller retains root ownership. The native process and all descendants
run as nobody with no capabilities. Sticky controller-owned directories allow
ordinary source edits while preventing replacement of protected public tests.
Controller-owned namespace sentinels are removed before Core checkpoints;
candidate code is never filtered, rewritten, or recovered by this module.
"""

from __future__ import annotations

import hashlib
import ctypes
import itertools
import json
import os
import platform
from pathlib import Path
import select
import signal
import stat
import subprocess
import sys
import time


NATIVE_UID = 65534
NATIVE_GID = 65534
NATIVE_WRITE_GUARD_POLICY = "linux-dac/v2"
_PUBLIC_TEST_DIRS = frozenset({"test", "tests", "__tests__", "testdata", "fixtures", "__fixtures__"})


class WriteGuardError(RuntimeError):
    """A guard could not be established or its protected files changed."""


def _linux_syscall(number: int, *arguments) -> int:
    # The pinned manylinux Python omits the stdlib pidfd wrappers. These two
    # Linux syscall numbers are shared by the supported amd64/arm64 ABI.
    libc = ctypes.CDLL(None, use_errno=True)
    value = libc.syscall(ctypes.c_long(number), *arguments)
    if value < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return value


def _pidfd_open(pid: int) -> int:
    return _linux_syscall(434, ctypes.c_int(pid), ctypes.c_uint(0))


def _pidfd_kill(descriptor: int) -> None:
    _linux_syscall(424, ctypes.c_int(descriptor), ctypes.c_int(signal.SIGKILL),
                   ctypes.c_void_p(), ctypes.c_uint(0))


def public_test_paths(source: Path) -> tuple[str, ...]:
    """Classify only tracked, visible baseline paths; never verifier metadata."""
    result = subprocess.run(
        ["git", "-C", str(source), "ls-files", "-z"], stdin=subprocess.DEVNULL,
        capture_output=True, check=True, timeout=15,
    )
    protected = []
    for raw in result.stdout.split(b"\0"):
        if not raw:
            continue
        path = Path(os.fsdecode(raw))
        if path.is_absolute() or ".." in path.parts:
            raise WriteGuardError("Public source inventory contains an invalid path.")
        name = path.name.casefold()
        if (any(part.casefold() in _PUBLIC_TEST_DIRS for part in path.parts)
                or name.startswith("test_") or ".test." in name or ".test-" in name or ".spec." in name
                or "_test." in name or "_tests." in name):
            protected.append(path.as_posix())
    return tuple(sorted(protected))


def public_test_directories(public_paths: tuple[str, ...]) -> tuple[str, ...]:
    """Find visible public-test directory roots from the frozen Git inventory."""
    roots = set()
    for value in public_paths:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts:
            raise WriteGuardError("Public source inventory contains an invalid path.")
        for index, part in enumerate(path.parts):
            if part.casefold() in _PUBLIC_TEST_DIRS:
                roots.add(Path(*path.parts[:index + 1]).as_posix())
                break
    return tuple(sorted(roots))


def _directory_entries(root: Path) -> tuple[tuple[str, int, str], ...]:
    """Inventory names and kinds without following links or reading file bodies."""
    entries = []
    for parent, directories, files in os.walk(root, followlinks=False):
        for name in directories + files:
            path = Path(parent) / name
            info = path.lstat()
            entries.append((path.relative_to(root).as_posix(), stat.S_IFMT(info.st_mode),
                            os.readlink(path) if stat.S_ISLNK(info.st_mode) else ""))
    return tuple(sorted(entries))


def reserved_names() -> tuple[str, ...]:
    """Match Core's ASCII case-insensitive namespace contract."""
    names = {".semantic-search", ".chrys"}
    for namespace in (".pact", ".pact-io"):
        variants = [(char.lower(), char.upper()) if char.isalpha() else (char,)
                    for char in namespace]
        names.update("".join(chars) for chars in itertools.product(*variants))
    return tuple(sorted(names))


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class BenchmarkWriteGuard:
    """One serial mutable role in a dedicated Linux solver container."""

    def __init__(self, workspace: Path, state: Path, public_paths: tuple[str, ...],
                 writable_journals: tuple[Path, ...] = ()) -> None:
        self.workspace = workspace.resolve()
        self.state = state.resolve()
        self.public_paths = public_paths
        self.writable_journals = writable_journals
        self.original: dict[Path, tuple[int, int, int]] = {}
        self.sentinels: dict[Path, tuple[int, int]] = {}
        self.protected: dict[Path, str] = {}
        self.protected_directories: dict[Path, tuple[int, int]] = {}
        self.public_directory_entries: dict[Path, tuple[tuple[str, int, str], ...]] = {}
        self.lock = None
        self.prepared = False
        self.finished = False
        self.receipt: dict = {"schema": "jiuwenswarm-benchmark/native-write-guard/v2",
                              "policy": NATIVE_WRITE_GUARD_POLICY,
                              "status": "not_prepared", "uid": NATIVE_UID, "gid": NATIVE_GID,
                              "public_inventory_sha256": hashlib.sha256(
                                  json.dumps(public_paths, ensure_ascii=True).encode()).hexdigest(),
                              "public_inventory_count": len(public_paths),
                              "protected_public_directories": list(public_test_directories(public_paths)),
                              "public_directory_additions_allowed": False,
                              "automatic_path_rewrite": False, "candidate_filtering": False}

    @property
    def command_prefix(self) -> tuple[str, ...]:
        return ("/usr/bin/setpriv", "--reuid", str(NATIVE_UID), "--regid", str(NATIVE_GID),
                "--clear-groups", "--bounding-set=-all", "--inh-caps=-all", "--ambient-caps=-all",
                "--no-new-privs", "--")

    def _remember(self, path: Path) -> os.stat_result:
        info = path.lstat()
        self.original.setdefault(path, (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)))
        return info

    @staticmethod
    def _processes() -> list[int]:
        result = []
        for path in Path("/proc").glob("[0-9]*/status"):
            try:
                fields = dict(line.split(":", 1) for line in path.read_text().splitlines() if ":" in line)
                if (not fields.get("State", "").strip().startswith(("Z", "X"))
                        and NATIVE_UID in map(int, fields["Uid"].split())):
                    result.append(int(path.parent.name))
            except (OSError, KeyError, ValueError, ProcessLookupError):
                continue
        return result

    def _traversable_parents(self, path: Path) -> None:
        for parent in path.parents:
            if parent == Path("/"):
                break
            info = self._remember(parent)
            os.chmod(parent, stat.S_IMODE(info.st_mode) | 0o001)

    def prepare(self) -> None:
        if sys.platform != "linux" or os.geteuid() != 0:
            raise WriteGuardError("Native benchmark write isolation requires a Linux root controller.")
        if platform.machine() not in {"x86_64", "aarch64"}:
            raise WriteGuardError("Native benchmark write isolation requires the amd64/arm64 Linux ABI.")
        try:
            descriptor = _pidfd_open(os.getpid())
            os.close(descriptor)
        except OSError as error:
            raise WriteGuardError("Kernel process-handle cleanup is unavailable.") from error
        if (self.workspace == self.state or self.state.is_relative_to(self.workspace)
                or self.workspace.is_relative_to(self.state)):
            raise WriteGuardError("Native state and role workspace must be separate directories.")
        import fcntl

        executable = Path(self.command_prefix[0])
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise WriteGuardError("Pinned base image lacks the privilege-drop executable.")
        lock_path = Path("/opt/jiuwenswarm-bench/.native-write-guard.lock")
        if not lock_path.parent.is_dir():
            raise WriteGuardError("Native benchmark runtime ownership anchor is unavailable.")
        self.lock = lock_path.open("a")
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise WriteGuardError("Another mutable native role owns the write guard.") from error
        if self._processes():
            raise WriteGuardError("Native sandbox UID is already in use in this container.")
        self.receipt["privilege_drop_sha256"] = _digest(executable)
        self._traversable_parents(self.workspace)
        self._traversable_parents(self.state)
        protected = {self.workspace / name for name in self.public_paths
                     if (self.workspace / name).exists() or (self.workspace / name).is_symlink()}
        for path in tuple(protected):
            if path.is_symlink():
                target = path.resolve(strict=True)
                if not target.is_relative_to(self.workspace):
                    raise WriteGuardError("Protected public test links outside the role workspace.")
                protected.add(target)
        public_roots = tuple(self.workspace / name for name in public_test_directories(self.public_paths))
        for root in public_roots:
            if (not root.is_dir() or root.is_symlink()
                    or any(parent.is_symlink() for parent in root.parents
                           if parent.is_relative_to(self.workspace))):
                raise WriteGuardError("Protected public test directory is missing or aliased.")
            self.public_directory_entries[root] = _directory_entries(root)
            for parent, directories, _ in os.walk(root, followlinks=False):
                path = Path(parent)
                info = path.lstat()
                self.protected_directories[path] = (info.st_dev, info.st_ino)
                if any((path / name).is_symlink() for name in directories):
                    raise WriteGuardError("Protected public test directory contains a directory link.")
        for root, directories, files in os.walk(self.workspace, followlinks=False):
            parent = Path(root)
            info = self._remember(parent)
            os.chown(parent, 0, 0)
            os.chmod(parent, 0o1777)
            for name in directories:
                path = parent / name
                if path.is_symlink():
                    self._remember(path)
                    os.chown(path, 0 if path in protected else NATIVE_UID,
                             0 if path in protected else NATIVE_GID, follow_symlinks=False)
            directories[:] = [name for name in directories
                              if name != ".git" and name not in reserved_names()
                              and not (parent / name).is_symlink()]
            for name in files:
                path = parent / name
                info = self._remember(path)
                if name == ".git" or name in reserved_names():
                    continue
                if path in protected:
                    os.chown(path, 0, 0, follow_symlinks=False)
                    if not path.is_symlink():
                        os.chmod(path, stat.S_IMODE(info.st_mode) & ~0o222)
                        self.protected[path] = _digest(path)
                else:
                    os.chown(path, NATIVE_UID, NATIVE_GID, follow_symlinks=False)
        # A sticky writable directory protects existing root-owned entries but
        # permits new files and subdirectories. Lock the visible public trees
        # after the ownership walk, while source and separate checks stay writable.
        for path in self.protected_directories:
            os.chmod(path, 0o555)
        for name in reserved_names():
            path = self.workspace / name
            if path.exists() or path.is_symlink():
                raise WriteGuardError("Mutable role workspace already contains a reserved namespace.")
            with path.open("xb"):
                pass
            info = path.stat()
            self.sentinels[path] = (info.st_dev, info.st_ino)
            os.chmod(path, 0)
        for root, directories, files in os.walk(self.state, followlinks=False):
            for path in (Path(root), *(Path(root) / name for name in directories + files)):
                self._remember(path)
                os.chown(path, NATIVE_UID, NATIVE_GID, follow_symlinks=False)
        for journal in self.writable_journals:
            if (not journal.is_absolute() or journal.is_symlink() or not journal.is_file()
                    or journal.resolve().is_relative_to(self.workspace) or journal.stat().st_nlink != 1):
                raise WriteGuardError("Native diagnostic journal is not an existing external regular file.")
            self._traversable_parents(journal)
            self._remember(journal)
            os.chown(journal, NATIVE_UID, NATIVE_GID)
        self.prepared = True
        self.receipt.update(status="prepared", protected_file_count=len(self.protected),
                            protected_directory_count=len(self.protected_directories),
                            reserved_namespace_count=len(self.sentinels), capabilities="none",
                            no_new_privileges=True, writable_diagnostic_journals=len(self.writable_journals))

    def environment(self, inherited: dict[str, str]) -> dict[str, str]:
        environment = dict(inherited)
        home = self.state / "native-home"
        home.mkdir(mode=0o700, exist_ok=True)
        os.chown(home, NATIVE_UID, NATIVE_GID)
        exclude = home / "controller-namespace-excludes"
        exclude.write_text("".join(f"/{name}\n" for name in reserved_names()))
        os.chmod(exclude, 0o444)
        count = int(environment.get("GIT_CONFIG_COUNT", "0"))
        for key, value in (("safe.directory", str(self.workspace)), ("core.excludesFile", str(exclude))):
            environment[f"GIT_CONFIG_KEY_{count}"] = key
            environment[f"GIT_CONFIG_VALUE_{count}"] = value
            count += 1
        environment.update(HOME=str(home), GIT_CONFIG_COUNT=str(count))
        return environment

    def finish(self) -> dict:
        if self.finished:
            return self.receipt
        violations = []
        try:
            if self.prepared:
                stopped, complete = self._stop_native_processes()
                self.receipt["residual_native_processes_stopped"] = stopped
                if not complete:
                    # Do not restore write access while an escaped descendant can
                    # still change protected files or controller namespaces.
                    self.receipt.update(status="integrity_failed", protected_content_unchanged=False,
                                        violations=["native_process_did_not_exit"], protections_retained=True)
                    return self.receipt
            for path, digest in self.protected.items():
                try:
                    intact = path.is_file() and not path.is_symlink() and _digest(path) == digest
                except OSError:
                    intact = False
                if not intact:
                    violations.append(path.relative_to(self.workspace).as_posix())
            for path, identity in self.protected_directories.items():
                try:
                    info = path.lstat()
                    intact = stat.S_ISDIR(info.st_mode) and (info.st_dev, info.st_ino) == identity
                except OSError:
                    intact = False
                if not intact:
                    violations.append(path.relative_to(self.workspace).as_posix())
            for root, entries in self.public_directory_entries.items():
                try:
                    intact = root.is_dir() and not root.is_symlink() and _directory_entries(root) == entries
                except OSError:
                    intact = False
                if not intact:
                    violations.append(root.relative_to(self.workspace).as_posix() + "/<entries>")
            for path, identity in self.sentinels.items():
                try:
                    info = path.lstat()
                except OSError:
                    violations.append(path.relative_to(self.workspace).as_posix())
                    continue
                if (info.st_dev, info.st_ino) != identity or not stat.S_ISREG(info.st_mode) or info.st_size:
                    violations.append(path.relative_to(self.workspace).as_posix())
                else:
                    path.unlink()
            if self.prepared:
                for root in (self.workspace, self.state):
                    for parent, directories, files in os.walk(root, followlinks=False):
                        if root == self.workspace:
                            directories[:] = [name for name in directories
                                              if name != ".git" and name not in reserved_names()]
                        for path in (Path(parent), *(Path(parent) / name for name in directories + files)):
                            if path not in self.original:
                                os.chown(path, 0, 0, follow_symlinks=False)
            for path, (uid, gid, mode) in reversed(tuple(self.original.items())):
                if path.exists() or path.is_symlink():
                    root = next((root for root in (self.workspace, self.state)
                                 if path.is_relative_to(root)), None)
                    if root is not None and any(parent.is_symlink() for parent in path.parents
                                                if parent.is_relative_to(root)):
                        violations.append("aliased_restore_path")
                        continue
                    os.chown(path, uid, gid, follow_symlinks=False)
                    if not path.is_symlink():
                        os.chmod(path, mode)
            status = "complete" if self.prepared else "not_prepared_cleaned"
            self.receipt.update(status="integrity_failed" if violations else status,
                                protected_content_unchanged=not violations,
                                protected_directory_entries_unchanged=not violations, violations=violations)
            return self.receipt
        except OSError:
            self.receipt.update(status="integrity_failed", protected_content_unchanged=False,
                                violations=[*violations, "cleanup_io_error"], protections_retained=True)
            return self.receipt
        finally:
            if self.lock is not None:
                self.lock.close()
                self.lock = None
            self.finished = True

    def _stop_native_processes(self) -> tuple[int, bool]:
        """Kill escaped descendants in batches before restoring permissions."""
        stopped: set[int] = set()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            processes = self._processes()
            if not processes:
                return len(stopped), True
            descriptors = []
            poll = select.poll()
            try:
                for pid in processes:
                    descriptor = None
                    try:
                        descriptor = _pidfd_open(pid)
                        fields = dict(line.split(":", 1) for line in
                                      (Path("/proc") / str(pid) / "status").read_text().splitlines()
                                      if ":" in line)
                        if NATIVE_UID in map(int, fields["Uid"].split()):
                            _pidfd_kill(descriptor)
                            stopped.add(pid)
                            descriptors.append(descriptor)
                            poll.register(descriptor, select.POLLIN)
                            descriptor = None
                    except (ProcessLookupError, FileNotFoundError):
                        pass
                    except OSError:
                        return len(stopped), False
                    finally:
                        if descriptor is not None:
                            os.close(descriptor)
                if descriptors:
                    poll.poll(max(1, int((deadline - time.monotonic()) * 1000)))
            finally:
                for descriptor in descriptors:
                    os.close(descriptor)
        return len(stopped), not self._processes()
