"""Budget stops cross the clarification boundary without becoming retries."""

from __future__ import annotations


class SideCallBudgetExhausted(RuntimeError):
    """The real Jiuwen session is incomplete and its artifacts must be retained."""

    def __init__(self, message: str, *, result: object = None, output_dir: object = None):
        super().__init__(message)
        self.result = result
        self.output_dir = output_dir


class SideCallInterrupted(RuntimeError):
    """A native interrupt is not evidence insufficiency or a completed proposal."""

    code = "INTERRUPTED"

    def __init__(self, message: str, *, result: object = None, output_dir: object = None):
        super().__init__(message)
        self.result = result
        self.output_dir = output_dir


class SideCallIntegrityError(RuntimeError):
    """The purported frozen public source changed; no model verdict is admissible."""

    code = "SNAPSHOT_CHANGED"


from dataclasses import dataclass
from typing import Literal, Protocol

SideCallStatus = Literal["completed", "timeout", "error"]

@dataclass(frozen=True, slots=True)
class SideCallTicket:
    token: str
    timeout_seconds: float | None

class SideCallAdmission(Protocol):
    def admit(self, stage: str) -> SideCallTicket: ...
    def finish(self, ticket: SideCallTicket, *, status: SideCallStatus, detail: str = "") -> None: ...
