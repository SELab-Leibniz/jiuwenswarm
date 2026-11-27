"""Small public values for work operations; callers never manufacture receipts."""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import cast

from ..kernel import PromotionReceipt
from .schemas import RuntimeContractError


@dataclass(frozen=True, slots=True)
class AcceptancePolicy:
    verify_command: str | None = None
    closeout_verify_command: str | None = None
    allow_unverified: bool = False
    verify_timeout_seconds: float = 1800.0
    final_verify_command: str | None = None
    final_verify_timeout_seconds: float = 1800.0

    def __post_init__(self) -> None:
        for name in ('verify_command', 'closeout_verify_command', 'final_verify_command'):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise RuntimeContractError(f'{name} must be a nonempty command or None')
        if not isinstance(self.allow_unverified, bool):
            raise RuntimeContractError('allow_unverified must be boolean')
        for name in ('verify_timeout_seconds', 'final_verify_timeout_seconds'):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise RuntimeContractError(f'{name} must be positive and finite')

    def payload(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def parse(cls, payload: object) -> AcceptancePolicy:
        if not isinstance(payload, dict) or set(payload) != {f.name for f in fields(cls)}:
            raise RuntimeContractError('AcceptancePolicy requires exactly the current fields')
        return cls(**payload)


@dataclass(frozen=True, slots=True)
class CandidateRef:
    work_id: str
    artifact: Path
    checkpoint: str
    tree: str


@dataclass(frozen=True, slots=True)
class VerificationEvidence:
    artifact: Path
    candidate: CandidateRef
    status: str
    command: str | None
    exit_code: int | None
    stdout_tail: str
    stderr_tail: str
    check_results: tuple[dict[str, object], ...] = ()


@dataclass(frozen=True, slots=True)
class ReviewEvidence:
    artifact: Path
    candidate: CandidateRef
    complete: bool
    feedback: str
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AcceptanceReceipt:
    work_id: str
    artifact: Path
    candidate: CandidateRef


@dataclass(frozen=True, slots=True)
class WorkSnapshot:
    work_id: str
    revision: int
    status: str
    contract_revision: int
    contract: dict[str, object]
    policy: AcceptancePolicy
    constraints: tuple[str, ...]
    candidate: CandidateRef
    advisory: dict[str, object]
    handoff: dict[str, object]
    verification: VerificationEvidence | None
    review: ReviewEvidence | None
    acceptance: AcceptanceReceipt | None
    delivery: PromotionReceipt | None
    review_requested: bool
    unresolved_assumptions: tuple[dict[str, object], ...]
    user_decisions: tuple[dict[str, object], ...]
    artifacts: Path
    acceptance_brief: dict[str, object] = field(default_factory=dict)
    checks: tuple[dict[str, object], ...] = ()


def object_record(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise RuntimeContractError(f'{label} must be an object')
    return cast(dict[str, object], value)
