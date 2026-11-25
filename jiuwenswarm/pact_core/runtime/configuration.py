"""Advisory scheduling settings for the optional built-in workflow."""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
import math

from .schemas import RuntimeContractError


@dataclass(frozen=True, slots=True)
class DriverConfiguration:
    """Scheduling preferences for the optional default workflow, never acceptance authority."""

    execution_policy: str = "continuous"
    max_rounds: int = 3
    review_interval: int | None = None
    max_unchanged_rounds: int = 2
    turn_timeout_seconds: float = 3600.0

    def payload(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def parse(cls, value: object) -> DriverConfiguration:
        if not isinstance(value, dict) or set(value) != {field.name for field in fields(cls)}:
            raise RuntimeContractError("Driver configuration must contain exactly current fields")
        policy = value["execution_policy"]
        if not isinstance(policy, str) or policy not in {"continuous", "review_each_round"}:
            raise RuntimeContractError("execution_policy must be continuous or review_each_round")
        for name in ("max_rounds", "max_unchanged_rounds"):
            item = value[name]
            if isinstance(item, bool) or not isinstance(item, int) or item < 1:
                raise RuntimeContractError(f"{name} must be a positive integer")
        interval = value["review_interval"]
        if interval is not None and (isinstance(interval, bool) or not isinstance(interval, int) or interval < 1):
            raise RuntimeContractError("review_interval must be a positive integer or null")
        timeout = value["turn_timeout_seconds"]
        if isinstance(timeout, bool) or not isinstance(timeout, (float, int)) or not math.isfinite(timeout) or timeout <= 0:
            raise RuntimeContractError("turn_timeout_seconds must be positive and finite")
        return cls(policy, value["max_rounds"], interval, value["max_unchanged_rounds"], float(timeout))
