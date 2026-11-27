"""Filesystem-race regressions for the uid-drop reviewer directory boundary."""

from __future__ import annotations

import os
import shutil
import stat
import sys
import tempfile
import threading
from concurrent.futures import CancelledError as FutureCancelledError
from pathlib import Path

import pytest

from jiuwenswarm.lh.reviewer_privilege import ReviewerLaunchPolicy, ReviewerProtectionError

ROOT_LINUX_SETPRIV = pytest.mark.skipif(
    sys.platform != "linux" or os.geteuid() != 0 or not shutil.which("setpriv"),
    reason="requires Linux root and setpriv",
)


def _policy_fixture() -> tuple[Path, Path, Path, ReviewerLaunchPolicy]:
    base = Path(tempfile.mkdtemp(prefix="lh-reviewer-races-", dir="/tmp"))
    base.chmod(0o755)
    live = base / "live"
    live.mkdir(mode=0o755)
    (live / "source.txt").write_text("source\n")
    output = base / "output"
    output.mkdir(mode=0o755)
    return base, live, output, ReviewerLaunchPolicy(65534, 65534, live, output)


def _tree_state(root: Path) -> tuple[tuple[object, ...], ...]:
    rows = []
    for path in (root, *sorted(root.rglob("*"))):
        info = path.lstat()
        content = path.read_bytes() if path.is_file() and not path.is_symlink() else None
        rows.append(
            (
                str(path.relative_to(root)),
                info.st_mode,
                info.st_uid,
                info.st_gid,
                os.readlink(path) if path.is_symlink() else None,
                content,
            )
        )
    return tuple(rows)


def _fd_path(fd: int) -> Path | None:
    for directory in ("/proc/self/fd", "/dev/fd"):
        try:
            return Path(os.readlink(f"{directory}/{fd}"))
        except OSError:
            continue
    return None


def _chown_path(path: object, dir_fd: int | None) -> Path | None:
    try:
        candidate = Path(path)
        if dir_fd is not None and not candidate.is_absolute():
            parent = _fd_path(dir_fd)
            return parent / candidate if parent is not None else None
        return candidate
    except (OSError, TypeError, ValueError):
        return None


@ROOT_LINUX_SETPRIV
def test_prepare_dirs_rejects_nested_cache_symlink():
    base, _live, output, policy = _policy_fixture()
    try:
        dirs = policy.prepare_dirs()
        target = output / "root-owned-cache-target"
        target.mkdir(mode=0o755)
        marker = target / "marker.txt"
        marker.write_text("keep\n")
        marker.chmod(0o640)
        before = _tree_state(target)
        pip = dirs["cache"] / "pip"
        shutil.rmtree(pip)
        pip.symlink_to(target, target_is_directory=True)
        with pytest.raises(ReviewerProtectionError):
            policy.prepare_dirs()
        assert _tree_state(target) == before
    finally:
        shutil.rmtree(base, ignore_errors=True)


@ROOT_LINUX_SETPRIV
def test_prepare_dirs_symlink_swap_race(monkeypatch):
    base, _live, output, policy = _policy_fixture()
    dirs = policy.prepare_dirs()
    pip = dirs["cache"] / "pip"
    backup = dirs["cache"] / "pip-race-backup"
    target = output / "root-owned-race-target"
    target.mkdir(mode=0o755)
    marker = target / "marker.txt"
    marker.write_text("keep\n")
    marker.chmod(0o640)
    before = _tree_state(target)

    request = threading.Event()
    swapped = threading.Event()
    stop = threading.Event()
    worker_errors: list[BaseException] = []
    state = {"triggered": False, "performed": False}

    def swap() -> None:
        if state["triggered"]:
            return
        state["triggered"] = True
        request.set()
        if not swapped.wait(timeout=10):
            raise AssertionError("synchronized symlink swap did not complete")

    def worker() -> None:
        try:
            while not stop.is_set():
                if not request.wait(timeout=0.1):
                    continue
                request.clear()
                if backup.exists():
                    shutil.rmtree(backup)
                if pip.is_symlink():
                    pip.unlink()
                elif pip.exists():
                    os.rename(pip, backup)
                pip.symlink_to(target, target_is_directory=True)
                state["performed"] = True
                swapped.set()
                while not stop.wait(timeout=0.1):
                    pass
        except BaseException as exc:  # pragma: no cover - surfaced below
            worker_errors.append(exc)
            swapped.set()

    thread = threading.Thread(target=worker, name="reviewer-symlink-swap", daemon=True)
    thread.start()
    real_chown = os.chown
    real_fchown = os.fchown

    def chown_hook(path, owner, group, *args, **kwargs):
        if _chown_path(path, kwargs.get("dir_fd")) == pip:
            swap()
        return real_chown(path, owner, group, *args, **kwargs)

    def fchown_hook(fd, owner, group):
        if _fd_path(fd) == pip:
            swap()
        return real_fchown(fd, owner, group)

    monkeypatch.setattr(os, "chown", chown_hook)
    monkeypatch.setattr(os, "fchown", fchown_hook)
    try:
        for _attempt in range(200):
            if pip.is_symlink():
                pip.unlink()
            if backup.exists() and not pip.exists():
                os.rename(backup, pip)
            try:
                policy.prepare_dirs()
            except ReviewerProtectionError:
                pass
            assert _tree_state(target) == before
            if state["performed"]:
                break
        assert state["performed"], "the synchronized swap never overlapped preparation"
    finally:
        stop.set()
        request.set()
        thread.join(timeout=10)
    assert not thread.is_alive()
    if worker_errors:
        raise worker_errors[0]
    assert _tree_state(target) == before
    shutil.rmtree(base, ignore_errors=True)


@ROOT_LINUX_SETPRIV
def test_prepare_dirs_rejects_non_directory_component():
    base, _live, _output, policy = _policy_fixture()
    try:
        dirs = policy.prepare_dirs()
        pip = dirs["cache"] / "pip"
        shutil.rmtree(pip)
        pip.write_text("not a directory\n")
        with pytest.raises(ReviewerProtectionError):
            policy.prepare_dirs()
    finally:
        shutil.rmtree(base, ignore_errors=True)


@ROOT_LINUX_SETPRIV
def test_prepare_dirs_rejects_foreign_owned_directory():
    base, _live, _output, policy = _policy_fixture()
    try:
        dirs = policy.prepare_dirs()
        pip = dirs["cache"] / "pip"
        os.chown(pip, 12345, 12345)
        with pytest.raises(ReviewerProtectionError, match="owner"):
            policy.prepare_dirs()
    finally:
        shutil.rmtree(base, ignore_errors=True)


@ROOT_LINUX_SETPRIV
def test_prepare_dirs_is_repeatable_for_reviewer_owned_directories():
    base, _live, _output, policy = _policy_fixture()
    try:
        first = policy.prepare_dirs()
        second = policy.prepare_dirs()
        assert first == second
        for name in ("home", "sessions", "cache", "tmp", "proofs"):
            info = second[name].stat()
            assert (info.st_uid, info.st_gid) == (65534, 65534)
            assert stat.S_IMODE(info.st_mode) == 0o700
        for path in (
            second["home"] / ".config",
            second["home"] / ".local",
            second["home"] / ".local" / "share",
            *(second["cache"] / name for name in (
                "pip", "uv", "npm", "cargo", "cargo-target", "gradle",
                "maven", "yarn", "pnpm", "bun", "puppeteer",
            )),
        ):
            info = path.stat()
            assert stat.S_ISDIR(info.st_mode)
            assert (info.st_uid, info.st_gid) == (65534, 65534), path
    finally:
        shutil.rmtree(base, ignore_errors=True)


@ROOT_LINUX_SETPRIV
def test_prepare_dirs_rejects_in_root_directory_symlink():
    base, _live, output, policy = _policy_fixture()
    try:
        dirs = policy.prepare_dirs()
        target = output / "inside-root-target"
        target.mkdir(mode=0o755)
        pip = dirs["cache"] / "pip"
        shutil.rmtree(pip)
        pip.symlink_to(target, target_is_directory=True)
        before = _tree_state(target)
        with pytest.raises(ReviewerProtectionError):
            policy.prepare_dirs()
        assert _tree_state(target) == before
    finally:
        shutil.rmtree(base, ignore_errors=True)


@ROOT_LINUX_SETPRIV
@pytest.mark.parametrize("operation", ["failure", "cancellation"])
def test_prepare_dirs_closes_fds_on_failure_and_cancellation(monkeypatch, operation):
    base, _live, _output, policy = _policy_fixture()
    try:
        opened: list[int] = []
        real_component = policy._open_component
        real_validate = policy._validate_managed_fd

        def tracking_component(parent_fd, name, **kwargs):
            fd = real_component(parent_fd, name, **kwargs)
            opened.append(fd)
            return fd

        def observing_validate(fd, label):
            info = real_validate(fd, label)
            if operation == "failure" and label == "pip":
                raise ReviewerProtectionError("synthetic component failure")
            if operation == "cancellation" and label == "cache":
                policy.cancel_requested.set()
            return info

        monkeypatch.setattr(policy, "_open_component", tracking_component)
        monkeypatch.setattr(policy, "_validate_managed_fd", observing_validate)
        with pytest.raises(ReviewerProtectionError if operation == "failure" else FutureCancelledError):
            policy.prepare_dirs()
        assert opened
        for fd in opened:
            with pytest.raises(OSError):
                os.fstat(fd)
    finally:
        shutil.rmtree(base, ignore_errors=True)


@ROOT_LINUX_SETPRIV
@pytest.mark.parametrize("kind", ["leaf", "intermediate"])
def test_own_tree_swap_does_not_change_external_target(monkeypatch, kind):
    base, _live, output, policy = _policy_fixture()
    try:
        candidate = output / "candidate"
        candidate.mkdir(mode=0o755)
        external = output / "external-target"
        external.mkdir(mode=0o755)
        (external / "keep.txt").write_text("keep\n")
        (external / "keep.txt").chmod(0o640)
        if kind == "leaf":
            victim = candidate / "leaf.txt"
            victim.write_text("leaf\n")
            victim.chmod(0o644)
            target = external / "keep.txt"
            backup = None
        else:
            victim = candidate / "sub"
            victim.mkdir(mode=0o755)
            (victim / "child.txt").write_text("child\n")
            target = external
            backup = candidate / "sub-backup"
        before = _tree_state(external)
        state = {"triggered": False}

        def swap() -> None:
            if state["triggered"]:
                return
            state["triggered"] = True
            if backup is None:
                victim.unlink()
            else:
                os.rename(victim, backup)
            victim.symlink_to(target, target_is_directory=backup is not None)

        real_chown = os.chown
        real_fchown = os.fchown

        def chown_hook(path, owner, group, *args, **kwargs):
            if _chown_path(path, kwargs.get("dir_fd")) == victim:
                swap()
            return real_chown(path, owner, group, *args, **kwargs)

        def fchown_hook(fd, owner, group):
            if _fd_path(fd) == victim:
                swap()
            return real_fchown(fd, owner, group)

        monkeypatch.setattr(os, "chown", chown_hook)
        monkeypatch.setattr(os, "fchown", fchown_hook)
        try:
            policy.own_tree(candidate)
        except ReviewerProtectionError:
            pass
        assert state["triggered"], "the synchronized swap never overlapped own_tree"
        assert _tree_state(external) == before
    finally:
        shutil.rmtree(base, ignore_errors=True)


@ROOT_LINUX_SETPRIV
def test_own_tree_changes_symlink_owner_without_touching_target():
    base, _live, output, policy = _policy_fixture()
    try:
        candidate = output / "candidate"
        candidate.mkdir(mode=0o755)
        external = output / "external-target"
        external.mkdir(mode=0o755)
        (external / "keep.txt").write_text("keep\n")
        link = candidate / "link"
        link.symlink_to(external, target_is_directory=True)
        before = _tree_state(external)
        policy.own_tree(candidate)
        info = link.lstat()
        assert (info.st_uid, info.st_gid) == (65534, 65534)
        assert _tree_state(external) == before
    finally:
        shutil.rmtree(base, ignore_errors=True)


@ROOT_LINUX_SETPRIV
def test_cleanup_rejects_symlink_root_without_deleting_target():
    base, _live, output, policy = _policy_fixture()
    try:
        policy.prepare_dirs()
        target = output / "cleanup-target"
        target.mkdir(mode=0o755)
        marker = target / "keep.txt"
        marker.write_text("keep\n")
        marker.chmod(0o640)
        before = _tree_state(target)
        managed = output / "reviewer-owned"
        backup = output / "reviewer-owned-backup"
        os.rename(managed, backup)
        managed.symlink_to(target, target_is_directory=True)
        try:
            with pytest.raises(ReviewerProtectionError, match="symlink"):
                policy.cleanup()
            assert _tree_state(target) == before
        finally:
            if managed.is_symlink():
                managed.unlink()
            if backup.exists():
                os.rename(backup, managed)
    finally:
        shutil.rmtree(base, ignore_errors=True)


@ROOT_LINUX_SETPRIV
@pytest.mark.parametrize("mode", [0o555, 0o1555])
def test_check_live_rejects_reviewer_owned_readonly_ancestor(mode):
    base = Path(tempfile.mkdtemp(prefix="lh-reviewer-ancestor-", dir="/tmp"))
    try:
        base.chmod(0o755)
        owned = base / "reviewer-owned-ancestor"
        owned.mkdir()
        os.chown(owned, 65534, 65534)
        owned.chmod(mode)
        live = owned / "live"
        live.mkdir(mode=0o755)
        (live / "source.txt").write_text("source\n")
        output = base / "output"
        output.mkdir(mode=0o755)
        with pytest.raises(ReviewerProtectionError, match="reviewer-owned live ancestor"):
            ReviewerLaunchPolicy(65534, 65534, live, output)
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _symlinked_output_fixture(kind: str) -> tuple[Path, Path, Path, Path, ReviewerLaunchPolicy]:
    base = Path(tempfile.mkdtemp(prefix="lh-reviewer-symlink-", dir="/tmp"))
    base.chmod(0o755)
    live = base / "live"
    live.mkdir(mode=0o755)
    (live / "source.txt").write_text("source\n")
    real = base / "real"
    real.mkdir(mode=0o755)
    canonical_output = real / "output"
    canonical_output.mkdir(mode=0o755)
    if kind == "output":
        original_output = base / "output-link"
        original_output.symlink_to(canonical_output, target_is_directory=True)
    else:
        ancestor = base / "ancestor-link"
        ancestor.symlink_to(real, target_is_directory=True)
        original_output = ancestor / "output"
    return base, live, original_output, canonical_output, ReviewerLaunchPolicy(65534, 65534, live, original_output)


def _open_fds() -> set[int]:
    fds = set()
    for name in os.listdir("/proc/self/fd"):
        if os.path.exists(f"/proc/self/fd/{name}"):
            fds.add(int(name))
    return fds


@ROOT_LINUX_SETPRIV
@pytest.mark.parametrize("kind", ["output", "ancestor"])
def test_grant_traversal_accepts_symlinked_output_root(kind):
    base, _live, original_output, _canonical_output, policy = _symlinked_output_fixture(kind)
    try:
        parent = original_output / "runs"
        parent.mkdir(mode=0o750)
        parent.chmod(0o750)
        candidate = parent / "candidate"
        candidate.mkdir(mode=0o755)
        artifact = candidate / "artifact.txt"
        artifact.write_text("artifact\n")
        artifact.chmod(0o640)
        before = parent.lstat()
        policy.grant_traversal(candidate.parent)
        after = parent.lstat()
        assert stat.S_IMODE(after.st_mode) == stat.S_IMODE(before.st_mode) | 0o001
        assert (after.st_uid, after.st_gid) == (before.st_uid, before.st_gid)
        policy.own_tree(candidate)
        for path in (candidate, artifact):
            info = path.lstat()
            assert (info.st_uid, info.st_gid) == (65534, 65534)
    finally:
        shutil.rmtree(base, ignore_errors=True)


@ROOT_LINUX_SETPRIV
@pytest.mark.parametrize("kind", ["output", "ancestor"])
@pytest.mark.parametrize("link_kind", ["leaf", "intermediate"])
@pytest.mark.parametrize("target_kind", ["in_root", "external", "live", "protected", "escape"])
def test_symlinked_output_root_preserves_descendant_guards(kind, link_kind, target_kind):
    base, live, original_output, canonical_output, policy = _symlinked_output_fixture(kind)
    try:
        if target_kind == "escape":
            escape_parent = Path(os.path.normpath(str(original_output / "..")))
            target = escape_parent / "escape-target"
            target.mkdir(mode=0o755)
            if link_kind == "intermediate":
                (target / "child").mkdir(mode=0o755)
                survivor = target / "child" / "keep.txt"
            else:
                survivor = target / "keep.txt"
            survivor.write_text("keep\n")
            survivor.chmod(0o640)
            before = _tree_state(target)
            victim = original_output / ".." / "escape-target"
            if link_kind == "intermediate":
                victim = victim / "child"
            for operation in ("grant_traversal", "own_tree"):
                with pytest.raises(ReviewerProtectionError, match="outside the approved output root"):
                    getattr(policy, operation)(victim)
                assert _tree_state(target) == before
            return
        if target_kind == "in_root":
            target = canonical_output / "inside-target"
        elif target_kind == "external":
            target = base / "external-target"
        elif target_kind == "live":
            target = live
        else:
            target = canonical_output / "protected-view"
        target.mkdir(mode=0o755, exist_ok=True)
        (target / "keep.txt").write_text("keep\n")
        (target / "keep.txt").chmod(0o640)
        if link_kind == "intermediate":
            (target / "child").mkdir(mode=0o755)
            (target / "child" / "keep.txt").write_text("child\n")
            (target / "child" / "keep.txt").chmod(0o640)
        if target_kind == "protected":
            policy.protect_views(target)
        before = _tree_state(target)
        alias = original_output / "alias"
        alias.symlink_to(target, target_is_directory=True)
        victim = alias / "child" if link_kind == "intermediate" else alias
        for operation in ("grant_traversal", "own_tree"):
            with pytest.raises(ReviewerProtectionError) as excinfo:
                getattr(policy, operation)(victim)
            assert "outside the approved output root" not in str(excinfo.value), (
                operation,
                target_kind,
                str(excinfo.value),
            )
            assert _tree_state(target) == before
    finally:
        shutil.rmtree(base, ignore_errors=True)


@ROOT_LINUX_SETPRIV
@pytest.mark.parametrize(
    ("existing", "parts"),
    [((), ("missing-leaf",)), (("existing",), ("existing", "missing", "leaf"))],
)
def test_own_tree_missing_path_is_noop(existing, parts):
    base, _live, output, policy = _policy_fixture()
    try:
        for name in existing:
            (output / name).mkdir(mode=0o755)
        (output / "present.txt").write_text("present\n")
        before = _tree_state(output)
        fds_before = _open_fds()
        policy.own_tree(output.joinpath(*parts))
        assert _tree_state(output) == before
        assert _open_fds() == fds_before
    finally:
        shutil.rmtree(base, ignore_errors=True)


@ROOT_LINUX_SETPRIV
@pytest.mark.parametrize(
    "case",
    [
        "cancellation",
        "dangling_symlink",
        "symlinked_parent",
        "non_directory_parent",
        "outside_root",
        "protected_path",
    ],
)
def test_own_tree_missing_path_preserves_rejections(case):
    base, _live, output, policy = _policy_fixture()
    try:
        if case == "cancellation":
            policy.cancel_requested.set()
            with pytest.raises(FutureCancelledError):
                policy.own_tree(output / "missing-cancel")
        elif case == "dangling_symlink":
            (output / "dangling").symlink_to(output / "missing-target", target_is_directory=True)
            with pytest.raises(ReviewerProtectionError, match="symlink|not a directory"):
                policy.own_tree(output / "dangling")
        elif case == "symlinked_parent":
            (output / "real-parent").mkdir(mode=0o755)
            (output / "alias-parent").symlink_to(output / "real-parent", target_is_directory=True)
            with pytest.raises(ReviewerProtectionError, match="symlink|not a directory"):
                policy.own_tree(output / "alias-parent" / "missing")
        elif case == "non_directory_parent":
            (output / "plain-file").write_text("not a directory\n")
            with pytest.raises(ReviewerProtectionError, match="not a directory"):
                policy.own_tree(output / "plain-file" / "missing")
        elif case == "outside_root":
            outside = base / "outside"
            outside.mkdir(mode=0o755)
            with pytest.raises(ReviewerProtectionError, match="outside the approved output root"):
                policy.own_tree(outside / "missing")
        else:
            view = output / "protected-view"
            view.mkdir(mode=0o755)
            (view / "keep.txt").write_text("keep\n")
            policy.protect_views(view)
            with pytest.raises(ReviewerProtectionError, match="protected view"):
                policy.own_tree(view / "missing")
    finally:
        shutil.rmtree(base, ignore_errors=True)
