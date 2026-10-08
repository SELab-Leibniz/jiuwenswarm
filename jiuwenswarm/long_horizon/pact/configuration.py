# Copyright (c) 2026 Chrys. All rights reserved.
# Adapted from Chrys 3f78185e298800b3e3f735f932a7ee2abb7bc25f; all inference uses native Jiuwen.

"""Trusted launch preferences and persisted default-workflow configuration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pact_core.runtime import AcceptancePolicy, RuntimeContractError, WorkSnapshot
from pact_core.runtime.configuration import DriverConfiguration

Workflow = Literal["default", "benchmark"]

_CONFIGURATION_SCHEMA = "jiuwenswarm-pact/configuration/v1"


@dataclass(frozen=True, slots=True)
class DefaultRunOptions:
    """None means unspecified; it must never overwrite resumed work settings."""

    profile_name: str | None = None
    verify_command: str | None = None
    allow_unverified: bool | None = None
    closeout_verify_command: str | None = None
    verify_timeout_seconds: float | None = None
    turn_timeout_seconds: float | None = None
    max_rounds: int | None = None
    review_interval: int | None = None
    review_each_round: bool | None = None

    def initial(self) -> tuple[str, AcceptancePolicy, DriverConfiguration]:
        """Resolve creation defaults, requiring an explicit verification choice."""
        policy = self.policy()
        if not policy.verify_command and not policy.allow_unverified:
            raise RuntimeContractError("default work creation requires --verify or explicit --allow-unverified")
        payload = DriverConfiguration().payload()
        for key, value in self._driver_values().items():
            if value is not None:
                payload[key] = value
        return self.profile_name or "Code", policy, DriverConfiguration.parse(payload)

    def policy(self) -> AcceptancePolicy:
        """Create explicit acceptance policy; benchmark may execute a draft."""
        return AcceptancePolicy(
            verify_command=self.verify_command,
            closeout_verify_command=self.closeout_verify_command,
            allow_unverified=self.allow_unverified is True,
            verify_timeout_seconds=1800.0 if self.verify_timeout_seconds is None else self.verify_timeout_seconds,
        )

    def validate_policy(self, state: WorkSnapshot) -> None:
        for key, value in {
            "verify_command": self.verify_command,
            "closeout_verify_command": self.closeout_verify_command,
            "allow_unverified": self.allow_unverified,
            "verify_timeout_seconds": self.verify_timeout_seconds,
        }.items():
            if value is not None and state.policy.payload()[key] != value:
                raise RuntimeContractError(f"existing work uses a different {key}")

    def _driver_values(self) -> dict[str, object]:
        return {
            "turn_timeout_seconds": self.turn_timeout_seconds,
            "max_rounds": self.max_rounds,
            "review_interval": self.review_interval,
            "execution_policy": None
            if self.review_each_round is None
            else ("review_each_round" if self.review_each_round else "continuous"),
        }

    def validate_existing(self, state: WorkSnapshot) -> str:
        """Reject explicit overrides; resume obtains its defaults from Core state."""
        workflow, profile = read_jiuwenswarm_configuration(state)
        if workflow != "default":
            raise RuntimeContractError("existing work is not the default workflow")
        if self.profile_name is not None and self.profile_name != profile:
            raise RuntimeContractError("existing work uses a different Jiuwen profile")
        self.validate_policy(state)
        config = DriverConfiguration.parse(state.advisory.get("driver_configuration"))
        for key, value in self._driver_values().items():
            if value is not None and config.payload()[key] != value:
                raise RuntimeContractError(f"existing work uses a different {key}")
        return profile


def read_jiuwenswarm_configuration(state: WorkSnapshot) -> tuple[Workflow, str]:
    """Read the versioned host identity independently of workflow preferences."""
    raw = state.advisory.get("jiuwenswarm_configuration")
    if (
        not isinstance(raw, dict)
        or set(raw) != {"schema", "workflow", "profile_name"}
        or raw.get("schema") != _CONFIGURATION_SCHEMA
        or raw.get("workflow") not in ("default", "benchmark")
        or not isinstance(raw.get("profile_name"), str)
        or not raw["profile_name"]
    ):
        raise RuntimeContractError("unsupported or missing Jiuwen work configuration")
    return raw["workflow"], raw["profile_name"]


def jiuwenswarm_configuration(profile_name: str, *, workflow: Workflow = "default") -> dict[str, object]:
    """Metadata is a host preference, never acceptance authority."""
    return {"schema": _CONFIGURATION_SCHEMA, "workflow": workflow, "profile_name": profile_name}
