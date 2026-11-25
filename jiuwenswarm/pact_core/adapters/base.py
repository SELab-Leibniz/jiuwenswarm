"""Adapter contract: any coding agent CLI normalized to one turn interface.

Adapters are process boundaries. They own prompt transport, subprocess
lifecycle, timeout and process-group termination, and host state isolation;
they never import host-internal packages (AGENTS.md invariant 7). The core
only ever sees ``TurnRequest`` in and ``TurnResult`` out.
"""

from __future__ import annotations

import os
from typing import Protocol, runtime_checkable

from ..schemas import AdapterProbe, TurnRequest, TurnResult

__all__ = ["AdapterProbe", "AgentAdapter", "TurnRequest", "TurnResult", "resolve_model"]


@runtime_checkable
class AgentAdapter(Protocol):
    """One coding agent CLI, reduced to headless run-one-turn semantics."""

    id: str

    def run_turn(self, request: TurnRequest) -> TurnResult:
        """Run one agent turn to process exit and normalize the outcome."""
        ...

    def probe(self) -> AdapterProbe:
        """Check CLI reachability and record the version for the manifest."""
        ...


def resolve_model(explicit: str | None, env_var: str) -> tuple[str | None, str]:
    """Resolve a configured model plus *how* it was configured.

    Distinguishing "argument" from "environment" requires the adapter to
    resolve its own model rather than have a caller pre-read the env var and
    forward it as a constructor argument (that would look identical to an
    explicit argument). Returns ``(model_for_cli, model_source)`` where
    ``model_for_cli`` is ``None`` when nothing is configured.
    """
    if explicit:
        return explicit, "argument"
    env_value = os.environ.get(env_var)
    if env_value:
        return env_value, "environment"
    return None, "cli_default_unknown"
