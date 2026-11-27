"""Reviewer-only Linux credential boundary. The controller never drops its own uid."""

from __future__ import annotations

import errno
import json
import os
import shutil
import stat
import subprocess
import sys
import threading
import time
from concurrent.futures import CancelledError as FutureCancelledError
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from .turn_environment import current_environment


class ReviewerProtectionError(RuntimeError):
    """An enforced reviewer cannot be started safely."""


class _MissingManagedPath(Exception):
    """Internal signal for fd-relative traversal that found an absent component."""


_VERIFY_CHILD = r"""
import json, os, sys
uid, gid, proof = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
fields = {}
for line in open('/proc/self/status', encoding='ascii'):
    key, _, value = line.partition(':')
    fields[key] = value.strip()
assert all(int(x) == uid for x in fields['Uid'].split())
assert all(int(x) == gid for x in fields['Gid'].split())
assert not fields['Groups'].split()
assert fields['NoNewPrivs'] == '1'
assert all(int(fields[key], 16) == 0 for key in ('CapInh', 'CapPrm', 'CapEff', 'CapBnd', 'CapAmb'))
assert os.getsid(0) == os.getpid()
with open(proof, 'x', encoding='utf-8') as stream:
    json.dump({'uid': uid, 'gid': gid, 'groups': [], 'no_new_privs': True,
               'capabilities': 'zero', 'pid': os.getpid(), 'session': os.getsid(0)}, stream)
os.execvpe(sys.argv[4], sys.argv[4:], os.environ)
"""

_CHECK_LIVE = r"""
import os, stat, sys
root = sys.argv[1]
child = root
for parent in [root, *list(__import__('pathlib').Path(root).parents)]:
    if parent == root:
        continue
    info = os.stat(parent)
    if info.st_uid == os.getuid():
        raise SystemExit('reviewer-owned live ancestor: ' + str(parent))
    if os.access(parent, os.W_OK | os.X_OK):
        protected_by_sticky = (info.st_mode & stat.S_ISVTX and info.st_uid != os.getuid()
                               and os.lstat(child).st_uid != os.getuid())
        if not protected_by_sticky:
            raise SystemExit('writable live ancestor: ' + str(parent))
    child = str(parent)
for base, dirs, files in os.walk(root, followlinks=False):
    if os.lstat(base).st_uid == os.getuid():
        raise SystemExit('reviewer owns live directory: ' + base)
    if os.access(base, os.W_OK | os.X_OK):
        raise SystemExit('writable directory: ' + base)
    for name in dirs + files:
        path = os.path.join(base, name)
        if os.lstat(path).st_uid == os.getuid() and not os.path.islink(path):
            raise SystemExit('reviewer owns live entry: ' + path)
        if os.access(path, os.W_OK):
            raise SystemExit('writable entry: ' + path)
"""

_CHECK_EXECUTABLE = r"""
import os, sys
path = sys.argv[1]
if not os.access(path, os.X_OK):
    raise SystemExit('executable is not accessible to reviewer uid')
try:
    with open(path, 'rb') as stream:
        header = stream.readline(256)
except OSError:
    header = b''
if header.startswith(b'#!'):
    interpreter = header[2:].decode('utf-8', 'replace').strip().split()[0]
    if not os.access(interpreter, os.X_OK):
        raise SystemExit('script interpreter is not accessible to reviewer uid')
"""

_CHECK_ROOT = r"""
import os, sys
if not os.access(sys.argv[1], os.X_OK):
    raise SystemExit('reviewer cannot traverse writable root')
"""

_CHECK_MANAGED = r"""
import os, sys
if not os.access(sys.argv[1], os.W_OK | os.X_OK):
    raise SystemExit('reviewer cannot write managed directory')
"""

_CHECK_VIEW = r"""
import os, sys
root = sys.argv[1]
for base, dirs, files in os.walk(root, followlinks=False):
    if os.access(base, os.W_OK):
        raise SystemExit('reviewer can write protected view directory: ' + base)
    for name in dirs + files:
        path = os.path.join(base, name)
        if os.access(path, os.W_OK):
            raise SystemExit('reviewer can write protected view entry: ' + path)
"""


def _overlap(a: Path, b: Path) -> bool:
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def _status(pid: int) -> dict[str, str]:
    return dict(line.split(":", 1) for line in Path(f"/proc/{pid}/status").read_text().splitlines() if ":" in line)


@dataclass
class ReviewerLaunchPolicy:
    uid: int | None
    gid: int | None
    live: Path | None
    root: Path
    root_spelling: Path = field(init=False)
    mode: str = field(init=False)
    reason: str | None = field(init=False)
    helper: str | None = field(init=False, default=None)
    python_executable: str = field(init=False)
    proofs: list[dict[str, object]] = field(default_factory=list)
    enforcement_error: str | None = None
    protected_views: tuple[Path, ...] = field(default_factory=tuple, init=False)
    cancel_requested: threading.Event = field(default_factory=threading.Event, init=False)

    def __post_init__(self) -> None:
        spelling = Path(self.root).expanduser()
        if not spelling.is_absolute():
            spelling = Path(os.path.abspath(spelling))
        self.root_spelling = Path(os.path.normpath(str(spelling)))
        self.root = spelling.resolve()
        self.live = Path(self.live).expanduser().resolve() if self.live else None
        if (self.uid is None) != (self.gid is None):
            raise ReviewerProtectionError("reviewer uid and gid must be supplied together")
        if self.uid is not None and (self.uid <= 0 or self.gid is None or self.gid <= 0):
            raise ReviewerProtectionError("reviewer uid and gid must be positive nonzero IDs")
        if self.uid is not None and self.uid == os.getuid():
            raise ReviewerProtectionError("reviewer identity must differ from the solver")
        if self.live and _overlap(self.root, self.live):
            raise ReviewerProtectionError("reviewer writable root overlaps the live workspace")
        self.python_executable = "/usr/bin/python3" if Path("/usr/bin/python3").is_file() else sys.executable
        if sys.platform == "linux" and os.geteuid() == 0 and self.uid is not None:
            if self.live is None or not self.live.is_dir():
                raise ReviewerProtectionError("canonical live workspace is missing or not a directory")
            self.mode, self.reason = "uid_drop", None
            self.helper = shutil.which("setpriv")
            if not self.helper:
                raise ReviewerProtectionError("setpriv is required for root reviewer uid-drop")
            try:
                existed = self.root.exists()
                self.root.mkdir(parents=True, exist_ok=True)
                if not existed:
                    self.root.chmod(0o711)
            except OSError as exc:
                raise ReviewerProtectionError(f"reviewer writable root is invalid: {exc}") from exc
            self.check_root()
            self.check_live()
        elif sys.platform == "linux" and os.geteuid() == 0 and self.live is not None:
            raise ReviewerProtectionError("Linux root review requires a dedicated --reviewer-uid and --reviewer-gid")
        else:
            self.mode = "degraded"
            self.reason = (
                "controller is not Linux root; no usable unprivileged namespace was established"
                if os.geteuid() != 0 or sys.platform != "linux"
                else "no dedicated reviewer uid/gid configured"
            )

    @classmethod
    def from_args(cls, args: object, *, root: Path | None = None) -> ReviewerLaunchPolicy | None:
        existing = getattr(args, "reviewer_launch_policy", None)
        if existing is not None:
            return existing
        uid = getattr(args, "reviewer_uid", None)
        gid = getattr(args, "reviewer_gid", None)
        live = getattr(args, "external_live_workdir", None)
        if live is None and uid is None and gid is None:
            return None
        return cls(
            uid,
            gid,
            live or getattr(args, "workdir", None),
            root or getattr(args, "output"),
        )

    def _prefix(self) -> list[str]:
        assert self.helper and self.uid is not None and self.gid is not None
        return [
            self.helper,
            "--reuid",
            str(self.uid),
            "--regid",
            str(self.gid),
            "--clear-groups",
            "--no-new-privs",
            "--bounding-set=-all",
            "--inh-caps=-all",
            "--ambient-caps=-all",
        ]

    def check_live(self) -> None:
        if self.live is None:
            raise ReviewerProtectionError("canonical live workspace is required for uid-drop")
        def fail_walk(exc: OSError) -> None:
            raise ReviewerProtectionError(f"live workspace ownership could not be verified: {exc}")

        for base, dirs, files in os.walk(self.live, followlinks=False, onerror=fail_walk):
            for path in (Path(base), *(Path(base) / name for name in [*dirs, *files])):
                try:
                    info = path.lstat()
                except OSError as exc:
                    raise ReviewerProtectionError(f"live workspace entry could not be checked: {exc}") from exc
                if info.st_uid == self.uid and not path.is_symlink():
                    raise ReviewerProtectionError("reviewer owns a live workspace entry")
        try:
            result = subprocess.run(
                [*self._prefix(), self.python_executable, "-c", _CHECK_LIVE, str(self.live)],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
                env=current_environment(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ReviewerProtectionError(f"live workspace protection could not be verified: {exc}") from exc
        if result.returncode:
            raise ReviewerProtectionError(
                "live workspace is writable or protection could not be verified: "
                + result.stderr[-300:]
                + result.stdout[-300:]
            )

    def check_root(self) -> None:
        try:
            result = subprocess.run(
                [*self._prefix(), self.python_executable, "-c", _CHECK_ROOT, str(self.root)],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
                env=current_environment(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ReviewerProtectionError(f"reviewer writable root could not be checked: {exc}") from exc
        if result.returncode:
            raise ReviewerProtectionError("reviewer uid cannot traverse the approved writable root")

    def check_executable(self, executable: str) -> None:
        try:
            result = subprocess.run(
                [*self._prefix(), self.python_executable, "-c", _CHECK_EXECUTABLE, executable],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
                env=current_environment(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ReviewerProtectionError(f"reviewer executable could not be checked: {exc}") from exc
        if result.returncode:
            raise ReviewerProtectionError(
                "reviewer executable is inaccessible: " + (result.stderr + result.stdout)[-300:]
            )

    @staticmethod
    def _directory_flags() -> int:
        return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC

    @staticmethod
    def _close_fds(fds: list[int]) -> None:
        while fds:
            try:
                os.close(fds.pop())
            except OSError:
                pass

    def _open_root_fd(self) -> int:
        try:
            return os.open(self.root, self._directory_flags())
        except OSError as exc:
            raise ReviewerProtectionError(f"approved reviewer root could not be opened: {exc}") from exc

    def _validate_managed_fd(self, fd: int, label: str) -> os.stat_result:
        assert self.uid is not None
        try:
            info = os.fstat(fd)
        except OSError as exc:
            raise ReviewerProtectionError(
                f"managed reviewer directory could not be checked: {label}: {exc}"
            ) from exc
        if not stat.S_ISDIR(info.st_mode):
            raise ReviewerProtectionError(f"managed reviewer path is not a directory: {label}")
        if info.st_uid not in (0, self.uid):
            raise ReviewerProtectionError(f"managed reviewer directory has an unexpected owner: {label}")
        return info

    def _open_component(
        self,
        parent_fd: int,
        name: str,
        *,
        create_mode: int | None,
        label: str,
        missing_ok: bool = False,
    ) -> int:
        attempts = 0
        while True:
            self._check_cancel()
            attempts += 1
            try:
                return os.open(name, self._directory_flags(), dir_fd=parent_fd)
            except FileNotFoundError:
                if create_mode is None:
                    if missing_ok:
                        raise _MissingManagedPath(label) from None
                    raise ReviewerProtectionError(f"managed reviewer directory is missing: {label}") from None
                try:
                    os.mkdir(name, create_mode, dir_fd=parent_fd)
                except FileExistsError:
                    pass
                except OSError as exc:
                    raise ReviewerProtectionError(
                        f"managed reviewer directory could not be created: {label}: {exc}"
                    ) from exc
                if attempts > 32:
                    raise ReviewerProtectionError(f"managed reviewer directory changed while opening: {label}")
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise ReviewerProtectionError(
                        f"managed reviewer directory is a symlink or not a directory: {label}"
                    ) from exc
                raise ReviewerProtectionError(
                    f"managed reviewer directory could not be opened: {label}: {exc}"
                ) from exc

    def _walk_managed_path(
        self,
        path: Path,
        *,
        create_mode: int | None,
        chmod_mode: int | None = None,
        chown: bool = True,
        add_traversal: bool = False,
    ) -> None:
        assert self.uid is not None and self.gid is not None
        path = self._disposable_path(path)
        try:
            resolved = path.resolve()
        except OSError as exc:
            raise ReviewerProtectionError(f"managed reviewer path could not be resolved: {exc}") from exc
        self._reject_protected(resolved)
        parts = () if path == self.root else path.relative_to(self.root).parts
        fds: list[int] = []
        try:
            root_fd = self._open_root_fd()
            fds.append(root_fd)
            self._validate_managed_fd(root_fd, str(self.root))
            for part in parts:
                if not part or part in (".", "..") or os.sep in part:
                    raise ReviewerProtectionError(f"invalid managed reviewer component: {part}")
                child_fd = self._open_component(fds[-1], part, create_mode=create_mode, label=part)
                fds.append(child_fd)
                info = self._validate_managed_fd(child_fd, part)
                try:
                    if chown:
                        os.fchown(child_fd, self.uid, self.gid)
                    if chmod_mode is not None:
                        os.fchmod(child_fd, chmod_mode)
                    elif add_traversal:
                        os.fchmod(child_fd, stat.S_IMODE(info.st_mode) | 0o001)
                except OSError as exc:
                    raise ReviewerProtectionError(
                        f"managed reviewer directory could not be updated: {part}: {exc}"
                    ) from exc
        finally:
            self._close_fds(fds)

    def _open_path_fd(self, path: Path, *, missing_ok: bool = False) -> int | None:
        path = self._disposable_path(path)
        parts = () if path == self.root else path.relative_to(self.root).parts
        fds: list[int] = [self._open_root_fd()]
        try:
            for part in parts:
                self._check_cancel()
                fds.append(
                    self._open_component(fds[-1], part, create_mode=None, label=part, missing_ok=missing_ok)
                )
            return fds.pop()
        except _MissingManagedPath:
            if not missing_ok:
                raise
            return None
        finally:
            self._close_fds(fds)

    def grant_traversal(self, path: Path) -> None:
        """Allow uid traversal through controller-owned output parents, never writes."""
        self._check_cancel()
        path = self._disposable_path(path)
        if self.mode != "uid_drop":
            return
        self._walk_managed_path(path, create_mode=None, chown=False, add_traversal=True)

    def check_managed(self, path: Path) -> None:
        if self.mode != "uid_drop":
            return
        result = subprocess.run(
            [*self._prefix(), self.python_executable, "-c", _CHECK_MANAGED, str(path)],
            capture_output=True, text=True, timeout=10, check=False,
            env=current_environment(),
        )
        if result.returncode:
            raise ReviewerProtectionError("reviewer cannot write its managed directory")

    def _managed(self) -> Path:
        self._check_cancel()
        path = self.root / "reviewer-owned"
        if self.mode == "uid_drop":
            self._walk_managed_path(path, create_mode=0o700, chmod_mode=0o700)
            return path
        path = self._disposable_path(path)
        if path.is_symlink():
            raise ReviewerProtectionError("reviewer-owned root must not be a symlink")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def prepare_dirs(self) -> dict[str, Path]:
        managed = self._managed()
        dirs = {name: managed / name for name in ("home", "sessions", "cache", "tmp", "proofs")}
        for path in dirs.values():
            self._check_cancel()
            if self.mode == "uid_drop":
                self._walk_managed_path(path, create_mode=0o700, chmod_mode=0o700)
                continue
            if path.is_symlink() or path.resolve() != path:
                raise ReviewerProtectionError(f"reviewer directory is a symlink: {path}")
            path.mkdir(parents=True, exist_ok=True)
        if self.mode == "uid_drop":
            for path in (
                dirs["home"] / ".config",
                dirs["home"] / ".local",
                dirs["home"] / ".local/share",
                *(
                    dirs["cache"] / name
                    for name in (
                        "pip",
                        "uv",
                        "npm",
                        "cargo",
                        "cargo-target",
                        "gradle",
                        "maven",
                        "yarn",
                        "pnpm",
                        "bun",
                        "puppeteer",
                    )
                ),
            ):
                self._check_cancel()
                self._walk_managed_path(path, create_mode=0o777)
        return dirs

    def cleanup(self) -> None:
        literal = self.root / "reviewer-owned"
        if self.mode != "uid_drop":
            path = self._disposable_path(literal)
            if path.is_symlink():
                raise ReviewerProtectionError("reviewer-owned cleanup root is a symlink")
            if path.exists():
                shutil.rmtree(path)
            return
        self._disposable_path(literal)
        try:
            root_fd = os.open(self.root, self._directory_flags())
        except FileNotFoundError:
            return
        except OSError as exc:
            raise ReviewerProtectionError(f"reviewer-owned cleanup root could not be opened: {exc}") from exc
        try:
            try:
                info = os.stat("reviewer-owned", dir_fd=root_fd, follow_symlinks=False)
            except FileNotFoundError:
                return
            except OSError as exc:
                raise ReviewerProtectionError(f"reviewer-owned cleanup root could not be checked: {exc}") from exc
            if stat.S_ISLNK(info.st_mode):
                raise ReviewerProtectionError("reviewer-owned cleanup root is a symlink")
            if not stat.S_ISDIR(info.st_mode):
                raise ReviewerProtectionError("reviewer-owned cleanup root is not a directory")
            if not shutil.rmtree.avoids_symlink_attacks:
                raise ReviewerProtectionError("symlink-resistant reviewer cleanup is unavailable")
            try:
                shutil.rmtree("reviewer-owned", dir_fd=root_fd)
            except FileNotFoundError:
                return
            except OSError as exc:
                raise ReviewerProtectionError(f"reviewer-owned cleanup failed: {exc}") from exc
        finally:
            os.close(root_fd)

    def _own_tree_fd(self, fd: int) -> None:
        assert self.uid is not None and self.gid is not None
        self._check_cancel()
        try:
            names = os.listdir(fd)
        except OSError as exc:
            raise ReviewerProtectionError(f"managed reviewer tree could not be read: {exc}") from exc
        for name in names:
            self._check_cancel()
            try:
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise ReviewerProtectionError(
                    f"managed reviewer tree entry could not be checked: {name}: {exc}"
                ) from exc
            try:
                if stat.S_ISDIR(info.st_mode):
                    child_fd = self._open_component(fd, name, create_mode=None, label=name)
                    try:
                        child_info = os.fstat(child_fd)
                        if not stat.S_ISDIR(child_info.st_mode):
                            raise ReviewerProtectionError(
                                f"managed reviewer tree entry is not a directory: {name}"
                            )
                        self._own_tree_fd(child_fd)
                    finally:
                        os.close(child_fd)
                else:
                    os.chown(name, self.uid, self.gid, dir_fd=fd, follow_symlinks=False)
            except OSError as exc:
                raise ReviewerProtectionError(
                    f"managed reviewer tree entry could not be updated: {name}: {exc}"
                ) from exc
        self._check_cancel()
        try:
            os.fchown(fd, self.uid, self.gid)
        except OSError as exc:
            raise ReviewerProtectionError(f"managed reviewer tree directory could not be updated: {exc}") from exc

    def own_tree(self, root: Path) -> None:
        self._check_cancel()
        root = self._disposable_path(root)
        if self.mode != "uid_drop":
            return
        # Preflight the entire tree before the first chown. A late alias must
        # never leave a partially changed protected tree behind.
        self._reject_protected(root.resolve())
        for base, dirs, files in os.walk(root, followlinks=False):
            self._check_cancel()
            for name in [*dirs, *files]:
                self._check_cancel()
                path = Path(base) / name
                if path.is_symlink():
                    self._reject_protected(path.resolve())
                else:
                    self._reject_protected(path)
        fd = self._open_path_fd(root, missing_ok=True)
        if fd is None:
            return
        try:
            self._own_tree_fd(fd)
        finally:
            os.close(fd)

    def protect_views(self, *views: Path) -> None:
        """Register frozen views before any reviewer-owned path is prepared."""
        resolved = tuple(Path(view).expanduser().resolve() for view in views)
        for view in resolved:
            if _overlap(self.root / "reviewer-owned", view):
                raise ReviewerProtectionError("reviewer-owned path overlaps a protected view")
            if self.mode == "uid_drop" and view not in self.protected_views:
                try:
                    result = subprocess.run(
                        [*self._prefix(), self.python_executable, "-c", _CHECK_VIEW, str(view)],
                        capture_output=True, text=True, timeout=10, check=False,
                        env=current_environment(),
                    )
                except (OSError, subprocess.TimeoutExpired) as exc:
                    raise ReviewerProtectionError(f"protected view access could not be verified: {exc}") from exc
                if result.returncode:
                    raise ReviewerProtectionError(
                        "reviewer can write a protected view: " + (result.stderr + result.stdout)[-300:]
                    )
        self.protected_views = tuple(dict.fromkeys((*self.protected_views, *resolved)))

    def _reject_protected(self, path: Path) -> None:
        if self.live is not None and _overlap(path, self.live):
            raise ReviewerProtectionError("managed reviewer path overlaps live workspace")
        if any(_overlap(path, view) for view in self.protected_views):
            raise ReviewerProtectionError("managed reviewer path overlaps a protected view")

    def _disposable_path(self, path: Path) -> Path:
        if self.mode == "uid_drop":
            lexical = Path(path).expanduser()
            if not lexical.is_absolute():
                lexical = Path(os.path.abspath(lexical))
            lexical = Path(os.path.normpath(str(lexical)))
            if lexical.is_relative_to(self.root_spelling):
                lexical = self.root / lexical.relative_to(self.root_spelling)
            elif not lexical.is_relative_to(self.root):
                raise ReviewerProtectionError("managed reviewer path is outside the approved output root")
            self._reject_protected(lexical)
            return lexical
        resolved = Path(path).expanduser().resolve()
        if not resolved.is_relative_to(self.root):
            raise ReviewerProtectionError("managed reviewer path is outside the approved output root")
        self._reject_protected(resolved)
        return resolved

    def _check_cancel(self) -> None:
        if self.cancel_requested.is_set():
            raise FutureCancelledError("reviewer preparation cancelled")

    def environment(self, base: dict[str, str]) -> dict[str, str]:
        if self.mode != "uid_drop":
            return dict(base)
        dirs = self.prepare_dirs()
        cache = dirs["cache"]
        return {
            **base,
            "HOME": str(dirs["home"]),
            "TMPDIR": str(dirs["tmp"]),
            "TMP": str(dirs["tmp"]),
            "TEMP": str(dirs["tmp"]),
            "XDG_CONFIG_HOME": str(dirs["home"] / ".config"),
            "XDG_CACHE_HOME": str(cache),
            "XDG_DATA_HOME": str(dirs["home"] / ".local/share"),
            "PIP_CACHE_DIR": str(cache / "pip"),
            "UV_CACHE_DIR": str(cache / "uv"),
            "npm_config_cache": str(cache / "npm"),
            "CARGO_HOME": str(cache / "cargo"),
            "CARGO_TARGET_DIR": str(cache / "cargo-target"),
            "GRADLE_USER_HOME": str(cache / "gradle"),
            "MAVEN_OPTS": f"{base.get('MAVEN_OPTS', '')} -Dmaven.repo.local={cache / 'maven'}".strip(),
            "YARN_CACHE_FOLDER": str(cache / "yarn"),
            "PNPM_HOME": str(cache / "pnpm"),
            "BUN_INSTALL_CACHE_DIR": str(cache / "bun"),
            "PUPPETEER_CACHE_DIR": str(cache / "puppeteer"),
            "XDG_RUNTIME_DIR": str(dirs["tmp"]),
        }

    def command(self, argv: list[str]) -> tuple[list[str], Path | None]:
        if self.mode != "uid_drop":
            return argv, None
        self.check_live()
        self.check_executable(argv[0])
        proof = self.prepare_dirs()["proofs"] / (uuid4().hex + ".json")
        return [
            *self._prefix(),
            self.python_executable,
            "-c",
            _VERIFY_CHILD,
            str(self.uid),
            str(self.gid),
            str(proof),
            *argv,
        ], proof

    def verify(self, pid: int, proof: Path | None, *, timeout: float = 5.0) -> dict[str, object]:
        if self.mode != "uid_drop":
            return {"review_isolation": "degraded", "reason": self.reason}
        assert proof is not None
        until = time.monotonic() + timeout
        while time.monotonic() < until and not proof.exists():
            time.sleep(0.01)
        if not proof.exists():
            raise ReviewerProtectionError("reviewer child did not establish credential proof")
        row = json.loads(proof.read_text())
        if row.get("pid") != pid or row.get("uid") != self.uid or row.get("gid") != self.gid:
            raise ReviewerProtectionError("reviewer child credential proof mismatch")
        fields = _status(pid)
        if (
            any(int(x) != self.uid for x in fields["Uid"].split())
            or any(int(x) != self.gid for x in fields["Gid"].split())
            or fields["Groups"].split()
            or fields["NoNewPrivs"].strip() != "1"
            or any(int(fields[key], 16) for key in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"))
            or os.getsid(pid) != pid
        ):
            raise ReviewerProtectionError("reviewer child credentials changed after proof")
        evidence = {
            "review_isolation": "uid_drop",
            "uid": self.uid,
            "gid": self.gid,
            "launch_mechanism": "setpriv",
            "pid": pid,
            "proof": row,
        }
        self.proofs.append(evidence)
        return evidence


__all__ = ["ReviewerLaunchPolicy", "ReviewerProtectionError"]
