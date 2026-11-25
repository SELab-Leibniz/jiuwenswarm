"""Configuration and bounded results of the optional benchmark workflow."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from pathlib import Path
import re

from ..runtime.schemas import RuntimeContractError
from ..runtime.types import WorkSnapshot


@dataclass(frozen=True, slots=True)
class BenchConfig:
    task_id: str
    trial_id: str
    budget_seconds: float
    max_role_calls: int | None = None
    turn_timeout_seconds: float = 3600.0
    review_interval: int = 2
    review_each_round: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, str) or not self.task_id.strip():
            raise RuntimeContractError("task_id must be nonempty")
        if not isinstance(self.trial_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", self.trial_id):
            raise RuntimeContractError("trial_id must be a safe nonempty identifier")
        for name in ("budget_seconds", "turn_timeout_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value <= 0:
                raise RuntimeContractError(f"{name} must be positive and finite")
        if self.max_role_calls is not None and (isinstance(self.max_role_calls, bool)
                or not isinstance(self.max_role_calls, int) or self.max_role_calls < 1):
            raise RuntimeContractError("max_role_calls must be a positive integer or null")
        if isinstance(self.review_interval, bool) or not isinstance(self.review_interval, int) or self.review_interval < 1:
            raise RuntimeContractError("review_interval must be a positive integer")
        if not isinstance(self.review_each_round, bool):
            raise RuntimeContractError("review_each_round must be boolean")

    def payload(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def parse(cls, value: dict[str, object]) -> BenchConfig:
        try:
            return cls(**value)  # type: ignore[arg-type]
        except TypeError as exc:
            raise RuntimeContractError(f"Invalid benchmark configuration: {exc}") from exc


@dataclass(frozen=True, slots=True)
class BenchResult:
    snapshot: WorkSnapshot
    stop_reason: str
    budget: dict[str, object]
    trial_dir: Path
