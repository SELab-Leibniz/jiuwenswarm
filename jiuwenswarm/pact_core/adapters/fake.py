"""Deterministic scripted adapter for hermetic tests.

Each ``FakeTurn`` describes one agent turn: optional filesystem mutations
applied to the request workdir (simulating agent work — or reviewer
tampering) followed by a preset result. E2E scenarios extend these scripts
instead of mocking controller internals (AGENTS.md testing rules).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..schemas import (
    AdapterProbe,
    ReviewDecisionCapture,
    TurnRequest,
    TurnResult,
    TurnStatus,
)


@dataclass(frozen=True, slots=True)
class FakeTurn:
    """Script for one turn: filesystem effects plus the returned result."""

    final_text: str = ""
    status: TurnStatus = "completed"
    exit_code: int | None = 0
    write_files: dict[str, str] = field(default_factory=dict)
    delete_files: tuple[str, ...] = ()
    review_decision: ReviewDecisionCapture = field(
        default_factory=lambda: ReviewDecisionCapture(
            verdict_status="unsupported",
            plan_challenge_status="unsupported",
        )
    )


class FakeAdapter:
    """Replay a fixed turn script; no subprocess, no model calls."""

    id = "fake"

    def __init__(self, turns: list[FakeTurn]) -> None:
        self._turns = list(turns)
        self._next = 0

    def run_turn(self, request: TurnRequest) -> TurnResult:
        if self._next >= len(self._turns):
            raise AssertionError(
                f"FakeAdapter script exhausted after {len(self._turns)} turns "
                f"(role={request.role})"
            )
        turn = self._turns[self._next]
        self._next += 1

        for relative, content in turn.write_files.items():
            target = request.workdir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        for relative in turn.delete_files:
            (request.workdir / relative).unlink(missing_ok=True)

        return TurnResult(
            status=turn.status,
            final_text=turn.final_text if turn.status == "completed" else "",
            output_source="scripted",
            exit_code=turn.exit_code,
            review_decision=turn.review_decision,
        )

    def probe(self) -> AdapterProbe:
        return AdapterProbe(
            adapter_id=self.id,
            available=True,
            version="scripted",
            binary_name="fake",
            isolation_mode="none",
        )
