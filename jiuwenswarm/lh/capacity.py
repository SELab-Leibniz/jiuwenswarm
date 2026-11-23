# Copyright (c) 2026 Chrys. All rights reserved.

"""One explicit, default-off capacity profile for the Jiuwen suite study.

The study asks for roughly 100x the former model-facing content capacities.
Those capacities are prompt/evidence limits, never acceptance rules.  A single
environment setting selects the profile so an experiment launcher can turn it
on for both arms while every other product keeps the historical defaults.

``LONG_HORIZON_CAPACITY_SCALE`` must be a positive integer.  It is unset by
default, which is exactly the historical scale of one.  The same setting is
read by the vendored Jiuwen worker (which cannot import this controller
package) so controller and worker cannot silently drift apart.
"""

from __future__ import annotations

import os
from typing import Final

CAPACITY_SCALE_ENV: Final = "LONG_HORIZON_CAPACITY_SCALE"
CAPACITY_PROFILE_SCHEMA: Final = "long-horizon/capacity-profile/v1"
DEFAULT_CAPACITY_SCALE: Final = 1
EXPERIMENT_CAPACITY_SCALE: Final = 100

# A guard against a typo turning a bounded process into an overflow.  The
# study needs 100; larger values remain possible for deliberate experiments.
MAX_CAPACITY_SCALE: Final = 10_000


class CapacityProfileError(ValueError):
    """The capacity setting is present but cannot describe a safe profile."""


def capacity_scale() -> int:
    """Return the positive integer scale selected by the deployment.

    Missing means the historical default.  A present but malformed value is a
    configuration error rather than a silent fallback, because accepting it
    would make a study receipt claim a capacity that was never applied.
    """
    raw = os.environ.get(CAPACITY_SCALE_ENV, "").strip()
    if not raw:
        return DEFAULT_CAPACITY_SCALE
    try:
        value = int(raw, 10)
    except (TypeError, ValueError):
        raise CapacityProfileError(
            f"{CAPACITY_SCALE_ENV} must be a positive integer, got {raw!r}"
        ) from None
    if isinstance(value, bool) or not 1 <= value <= MAX_CAPACITY_SCALE:
        raise CapacityProfileError(
            f"{CAPACITY_SCALE_ENV} must be an integer from 1 to {MAX_CAPACITY_SCALE}, got {raw!r}"
        )
    return value


def scaled(value: int, *, minimum: int = 1) -> int:
    """Scale one model-facing content bound by the selected profile."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CapacityProfileError("capacity base must be a non-negative integer")
    try:
        result = value * capacity_scale()
    except OverflowError:
        raise CapacityProfileError("capacity scale overflows the platform integer") from None
    return max(minimum, result)


def model_content_limit(base: int, *, minimum: int = 1) -> int | None:
    """Return the historical cap at 1x, or no controller cap at an opt-in profile.

    The experiment explicitly preserves model-facing content until the genuine
    native model window compacts it.  Keeping the old controller slice at a new
    fixed threshold would still silently remove content, so only the default
    profile retains the historical prompt cap.
    """
    return base if capacity_scale() == DEFAULT_CAPACITY_SCALE else None


def capacity_receipt() -> dict[str, object]:
    """A small, secret-free receipt included in runtime evidence."""
    return {
        "schema": CAPACITY_PROFILE_SCHEMA,
        "setting": CAPACITY_SCALE_ENV,
        "scale": capacity_scale(),
        "default_scale": DEFAULT_CAPACITY_SCALE,
        "experiment_scale": EXPERIMENT_CAPACITY_SCALE,
        "scope": "model-facing prompt and evidence content only; not acceptance, verdict, timeout, retry or count semantics",
    }


__all__ = [
    "CAPACITY_PROFILE_SCHEMA",
    "CAPACITY_SCALE_ENV",
    "DEFAULT_CAPACITY_SCALE",
    "EXPERIMENT_CAPACITY_SCALE",
    "MAX_CAPACITY_SCALE",
    "CapacityProfileError",
    "capacity_receipt",
    "capacity_scale",
    "model_content_limit",
    "scaled",
]
