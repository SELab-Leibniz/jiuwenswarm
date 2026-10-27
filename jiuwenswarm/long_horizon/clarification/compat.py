# Copyright (c) 2026 Chrys. All rights reserved.
# Adapted for JiuwenSwarm from Chrys revision
# 6fcfb3321892735631b49f33236b1e2a988ded2d (workspace, scanner, encoding,
# trajectory keys, and owner-only file helpers). No Chrys runtime dependency.

"""Small local platform boundary for the transplanted clarification services.

The supported execution environment is a Linux container; POSIX writes use an
opened parent directory and never follow the destination file. These helpers
do not provide a sandbox for an actively hostile process running as the owner.
"""

from __future__ import annotations

import codecs
import contextlib
import os
import secrets
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path, PurePath


@dataclass
class WorkingDir:
    path: str
    label: str = ""
    is_primary: bool = False

    @property
    def exists(self) -> bool:
        return os.path.isdir(self.path)


@dataclass
class Workspace:
    primary_cwd: str
    working_dirs: list[WorkingDir] = field(default_factory=list)
    reference_files: list[str] = field(default_factory=list)

    @classmethod
    def from_cwd(cls, cwd: str | None = None) -> Workspace:
        return cls(primary_cwd=os.path.abspath(os.path.expanduser(cwd or os.getcwd())))


@dataclass(frozen=True)
class _Platform:
    is_windows: bool = os.name == "nt"


def get_platform() -> _Platform:
    return _Platform()


def find_rg() -> str | None:
    return shutil.which("rg")


def surrogate_safe_text(value: str) -> str:
    """Escape lone surrogates for display, never for filesystem identity."""
    return value.encode("utf-8", errors="backslashreplace").decode("utf-8")


def _open_posix_directory(path: Path, *, create: bool) -> int:
    """Walk every component through directory handles, refusing symlinks."""
    if os.name == "nt":
        raise OSError("Owner-only artifact storage requires POSIX permissions")
    absolute = Path(os.path.abspath(path))
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(absolute.anchor, flags)
    try:
        for part in absolute.parts[1:]:
            try:
                child_fd = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                child_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def ensure_owner_only_directory(path: Path) -> None:
    """Create/tighten an artifact directory without following any symlinks."""
    path = Path(path)
    fd = _open_posix_directory(path, create=True)
    try:
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(f"Owner-only path is not a directory: {path}")
        os.fchmod(fd, 0o700)
    finally:
        os.close(fd)


def validate_link_free_directory(path: Path, *, create: bool = False) -> None:
    """Validate/create a directory chain without changing existing modes."""
    fd = _open_posix_directory(Path(path), create=create)
    os.close(fd)


def atomic_write_owner_only_bytes(path: Path, payload: bytes, *, create_parents: bool = True) -> None:
    """Publish a complete 0600 file; reject symlink parents and preserve dirs' modes.

The caller supplies a trusted artifact root or a checked destination. The leaf
directory is held open across creation and replacement so replacing that name
cannot redirect the write. Replacing a destination symlink replaces the link,
not its target.
    """
    path = Path(path)
    parent_fd = _open_posix_directory(path.parent, create=create_parents)
    temp_name = f".{path.name}.{secrets.token_hex(16)}.tmp"
    try:
        fd = os.open(
            temp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=parent_fd,
        )
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp_name, dir_fd=parent_fd)
        os.close(parent_fd)


def atomic_write_owner_only_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    atomic_write_owner_only_bytes(path, text.encode(encoding, errors="backslashreplace"))


# This is the original scanner's generated/dependency exclusion set. Snapshot
# scanning additionally lets tracked Git paths through these name exclusions.
DEFAULT_EXCLUDES: frozenset[str] = frozenset(
    {
        ".git", ".svn", ".hg", ".bzr", "node_modules", "bower_components",
        ".next", ".nuxt", ".turbo", ".parcel-cache", "__pycache__", ".venv",
        "venv", "env", ".eggs", "*.egg-info", ".tox", ".nox", ".pytest_cache",
        ".mypy_cache", ".ruff_cache", ".pytype", "htmlcov", ".coverage", "target",
        ".gradle", ".mvn", "bin", "obj", "vendor", ".bundle", "build", "dist",
        "out", ".idea", ".vscode", ".vs", ".eclipse", ".cache", ".sass-cache",
        ".eslintcache", ".stylelintcache", ".DS_Store", "Thumbs.db", ".terraform",
        ".serverless", ".pact-io",
    }
)


class GitignoreFilter:
    """A strict pathspec filter, with no approximate glob fallback.

    Only an absent ignore file means no rules. Unreadable or symlinked ignore
    files fail capture rather than silently changing snapshot scope.
    """

    def __init__(self, rules: list[str] | None = None) -> None:
        self._spec = None
        if rules:
            import pathspec

            self._spec = pathspec.GitIgnoreSpec.from_lines(rules)

    @classmethod
    def from_file(cls, path: str) -> GitignoreFilter:
        source = Path(path)
        if source.is_symlink():
            raise OSError(f"Ignore file must not be a symlink: {source}")
        try:
            text = source.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return cls()
        return cls.from_text(text)

    @classmethod
    def from_text(cls, text: str) -> GitignoreFilter:
        return cls(text.splitlines())

    def is_ignored(self, rel_path: str, *, is_dir: bool = False) -> bool:
        if self._spec is None:
            return False
        relative = PurePath(rel_path).as_posix().strip("/")
        if is_dir:
            relative = relative.rstrip("/") + "/"
        return bool(self._spec.match_file(relative))


_BINARY_SIGNATURES = (
    b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"%PDF-", b"PK\x03\x04",
    b"\x1f\x8b", b"\x7fELF", b"MZ", b"SQLite format 3\x00", b"GIF87a",
    b"GIF89a", b"RIFF",
)
_BOM_PREFIXES = (codecs.BOM_UTF8, codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE,
                 codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)
_SUSPICIOUS_CTRL_BYTES = frozenset(set(range(32)) - {8, 9, 10, 12, 13})


class EncodingDetector:
    """The original binary heuristic used to bound the model-visible view."""

    @staticmethod
    def looks_binary(sample: bytes) -> bool:
        sample = sample[:64 * 1024]
        if not sample:
            return False
        if sample.startswith(_BINARY_SIGNATURES):
            return True
        if sample.startswith(_BOM_PREFIXES):
            return False
        if b"\x00" in sample:
            for encoding in ("utf-32-le", "utf-32-be", "utf-16-le", "utf-16-be"):
                try:
                    text = sample.decode(encoding)
                except (UnicodeDecodeError, LookupError):
                    continue
                controls = sum(ord(char) in _SUSPICIOUS_CTRL_BYTES for char in text)
                if not text or controls / len(text) <= 0.05:
                    return False
            return True
        controls = sum(byte in _SUSPICIOUS_CTRL_BYTES for byte in sample)
        return controls > 0 and controls / len(sample) > 0.30
