# Copyright (c) 2026 Chrys. All rights reserved.

"""Keep the live Meridian product-owner channel outside candidate publication."""

from __future__ import annotations

from pathlib import PurePosixPath

LIVE_PATHS = ["QUESTIONS.md", "ISSUES"]


def validate_live_paths(value: object) -> None:
    if type(value) is not list or value != LIVE_PATHS:
        raise ValueError("Meridian live channel policy must be explicit and exact")


def publication_names(names: set[str], live_paths: list[str]) -> set[str]:
    """Filter both the copy allowlist and its old-file deletion allowlist.

    Notices, logs and controller state live outside /work. They are not part
    of either input allowlist. Linux containers use their own case-sensitive
    filesystem; distinct case spellings remain distinct candidate paths.
    """
    validate_live_paths(live_paths)
    result = set()
    for name in names:
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or path.as_posix() != name:
            raise ValueError("candidate publication path is not normalized and relative")
        if path.parts[0] in LIVE_PATHS:
            continue
        result.add(name)
    return result
