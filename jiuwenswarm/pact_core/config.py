"""Loop configuration: everything `pact run` decides before the loop starts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class LoopConfig:
    """Configuration for one PACT loop."""

    workspace: Path
    plan_file: Path
    max_rounds: int = 3
    verify_command: str | None = None
    verify_timeout_seconds: float = 1800.0
    allow_unverified: bool = False
    final_verify_command: str | None = None
    final_verify_timeout_seconds: float = 1800.0
    turn_timeout_seconds: float = 3600.0
    loop_id: str | None = None
    worktree_root: Path | None = None
