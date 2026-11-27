"""Private environment snapshots for synchronous and cross-loop role turns."""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar

_turn_environment: ContextVar[dict[str, str] | None] = ContextVar("turn_environment", default=None)


def current_environment() -> dict[str, str]:
    """Return an independent copy; standalone helpers inherit a fresh snapshot."""
    value = _turn_environment.get()
    return dict(os.environ if value is None else value)


@contextmanager
def turn_environment(overlay: Mapping[str, str], *, base: Mapping[str, str] | None = None) -> Iterator[None]:
    environment = current_environment() if base is None else dict(base)
    environment.update(overlay)
    token = _turn_environment.set(environment)
    try:
        yield
    finally:
        _turn_environment.reset(token)
