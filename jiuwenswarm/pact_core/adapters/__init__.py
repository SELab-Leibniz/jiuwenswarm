"""Adapter registry: the only place concrete adapter implementations load.

Modules outside this package depend on ``AgentAdapter`` and this factory
alone; an architecture test enforces the boundary.
"""

from __future__ import annotations

from typing import cast

from .base import AgentAdapter
from ..schemas import Role

SUPPORTED_ADAPTERS = ("fake", "chrys", "opencode")


class UnknownAdapterError(ValueError):
    """Raised for adapter ids outside the supported list."""


def _require_role(adapter_id: str, role: str) -> Role:
    if role not in ("worker", "reviewer"):
        raise UnknownAdapterError(f"unsupported role for {adapter_id} adapter: {role!r}")
    return cast(Role, role)


def create_adapter(adapter_id: str, *, role: str, max_rounds: int) -> AgentAdapter:
    """Build a supported adapter for one role.

    Only adapters with a passing E2E smoke may appear in
    ``SUPPORTED_ADAPTERS`` — no enum-before-implementation (AGENTS.md
    invariant 7).
    """
    if adapter_id == "fake":
        from .fake import FakeAdapter, FakeTurn

        validated_role = _require_role("fake", role)
        if validated_role == "worker":
            turns = [
                FakeTurn(final_text="Scripted worker summary: no changes made.\nPACT_REVIEW_READY")
                for _ in range(max_rounds)
            ]
        else:
            turns = [
                FakeTurn(final_text="Scripted review.\n\nPACT_COMPLETE\n")
                for _ in range(max_rounds)
            ]
        return FakeAdapter(turns)
    if adapter_id == "chrys":
        from .chrys import ChrysAdapter

        return ChrysAdapter(role=_require_role("chrys", role))
    if adapter_id == "opencode":
        from .opencode import OpenCodeAdapter

        return OpenCodeAdapter(_require_role("opencode", role))
    raise UnknownAdapterError(
        f"unknown {role} adapter: {adapter_id!r} (supported: {', '.join(SUPPORTED_ADAPTERS)})"
    )
