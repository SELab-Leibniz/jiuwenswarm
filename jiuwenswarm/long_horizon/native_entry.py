"""Stage-local write bootstrap, followed by the unchanged native machine CLI.

The pinned Process CLI does not initialize agent-core's absolute log directory
and its memory index resolves ``memory.db`` against the request workspace.
Without this bootstrap those companion-owned writes land in a read-only frozen
view after native workspace activation. No agent behavior or machine protocol
is reimplemented here.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
from pathlib import Path


def _real(path: Path) -> Path:
    return Path(os.path.realpath(path))


def _overlaps(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _stage_views() -> tuple[Path, ...]:
    raw = os.environ.get("JIUWENSWARM_STAGE_VIEW_DIRS", "")
    views: list[Path] = []
    for value in raw.split(os.pathsep):
        if not value:
            continue
        path = Path(value)
        if not path.is_absolute():
            raise ValueError("stage view must be absolute")
        resolved = _real(path)
        if resolved not in views:
            views.append(resolved)
    return tuple(views)


def _validate_stage_paths(state: Path, views: tuple[Path, ...]) -> None:
    state = _real(state)
    for view in views:
        if _overlaps(state, view):
            raise ValueError("stage state aliases or overlaps a frozen view")


def _prepare_stage(state: Path, *, read_only: bool) -> None:
    for directory in ("openjiuwen-logs", "logs", "startup", "context-debug",
                      "tasks", "task-registry", "memory", "pycache", "tmp"):
        (state / directory).mkdir(parents=True, exist_ok=True)
    os.chdir(state)
    if not read_only:
        return
    temporary = state / "tmp"
    os.environ.update({
        "JIUWENSWARM_STAGE_READ_ONLY": "1",
        "JIUWENSWARM_STAGE_WRITES_DIR": str(state),
        "TMPDIR": str(temporary), "TEMP": str(temporary), "TMP": str(temporary),
        "PYTHONPYCACHEPREFIX": str(state / "pycache"),
    })


def _write_target(value: object, state: Path, views: tuple[Path, ...],
                  *, parents: bool = False) -> object | None:
    """Map a relative or view-owned write into the stage state.

    Absolute paths already outside the views are left alone; they are not
    established companion-owned destinations. Relative paths that resolve
    outside the state are contained as well, so a native ``..`` cannot escape.
    """
    try:
        raw = os.fspath(value)
    except TypeError:
        return None
    if not isinstance(raw, str) or not raw or raw == ":memory:" or raw.startswith("file:"):
        return None
    path = Path(raw)
    absolute = path if path.is_absolute() else Path.cwd() / path
    normalized = Path(os.path.normpath(str(absolute)))
    resolved = _real(absolute)
    target: Path | None = None
    for view in views:
        for candidate in (normalized, resolved):
            if candidate == view or view in candidate.parents:
                target = state / "stage-writes" / candidate.relative_to(view)
                break
        if target is not None:
            break
    if target is None and not path.is_absolute():
        if not (resolved == state or state in resolved.parents):
            relative = Path(os.path.normpath(raw))
            while relative.parts and relative.parts[0] == "..":
                relative = Path(*relative.parts[1:])
            target = state / "stage-writes" / relative
    if target is None:
        return None
    target = Path(os.path.normpath(str(target)))
    if target != state and state not in target.parents:
        target = state / "stage-writes" / target.name
    if parents:
        target.parent.mkdir(parents=True, exist_ok=True)
    return str(target)


def _install_write_redirects(state: Path, views: tuple[Path, ...]) -> None:
    """Redirect rotating logs and SQLite databases opened by the child.

    This is deliberately limited to two APIs the native companion uses for its
    own diagnostics and memory index. WAL, SHM and journal files follow the
    redirected database path inside SQLite.
    """
    if getattr(_install_write_redirects, "_installed", False):
        return
    original_file_handler = logging.FileHandler.__init__

    def file_handler(handler, filename, *args, **kwargs):
        target = _write_target(filename, state, views, parents=True)
        return original_file_handler(handler, filename if target is None else target, *args, **kwargs)

    logging.FileHandler.__init__ = file_handler

    original_connect = sqlite3.connect

    def connect(database, *args, **kwargs):
        target = _write_target(database, state, views, parents=True)
        return original_connect(database if target is None else target, *args, **kwargs)

    sqlite3.connect = connect
    _install_write_redirects._installed = True


def _configure_logging(state: Path) -> None:
    from openjiuwen.core.common.logging.log_config import configure_log_config

    maximum = os.environ.get("JIUWENSWARM_STAGE_LOG_MAX_BYTES", "").strip()
    try:
        max_bytes = max(1024, int(maximum)) if maximum else 20 * 1024 * 1024
    except ValueError:
        max_bytes = 20 * 1024 * 1024
    configure_log_config({
        "backend": "default", "level": "INFO", "log_path": str(state / "openjiuwen-logs") + os.sep,
        "output": ["console", "file"], "max_bytes": max_bytes,
    })


def main() -> None:
    state_value = os.environ.get("JIUWENSWARM_DATA_DIR", "")
    state = Path(state_value)
    if not state_value or not state.is_absolute() or not state.is_dir():
        print("Native stage requires an existing absolute state directory.", file=sys.stderr)
        raise SystemExit(1)

    read_only = os.environ.get("JIUWENSWARM_STAGE_READ_ONLY") == "1"
    try:
        views = _stage_views()
        _validate_stage_paths(state, views)
        _prepare_stage(state, read_only=read_only)
        if read_only:
            _install_write_redirects(state, views)
    except (OSError, ValueError):
        print("Native stage write boundary is invalid.", file=sys.stderr)
        raise SystemExit(1) from None

    # Importing this module only defines the stdout protocol helper; it does
    # not initialize openjiuwen logging. Absolute log destinations are set
    # below before the native CLI can import the runtime or chdir to the view.
    from jiuwenswarm.channels.process_cli.machine_entry import protocol_stdout

    try:
        with protocol_stdout():
            _configure_logging(state)
            from jiuwenswarm.channels.process_cli.main import main as native_main
            # The shell tool may live in a frozen Jiuwen tree, so this
            # companion patches its class before the native CLI mounts tools.
            try:
                from jiuwenswarm.long_horizon.process_kill_guard import install_shell_tool_guard

                install_shell_tool_guard()
            except Exception:
                pass
    except Exception:
        # Do not print third-party exceptions that can include provider config.
        print("Native stage logging bootstrap failed.", file=sys.stderr)
        raise SystemExit(1) from None
    native_main()


if __name__ == "__main__":
    main()
