# Copyright (c) 2026 Chrys. All rights reserved.

"""Bounded requirement-coverage audit after an unusually early acceptance.

The controller freezes the first accepted candidate as an immutable fallback,
then asks the isolated executable Reviewer to enumerate and execute every
requirement, including secondary behavior. Gaps go to a bounded repair Worker;
only a freshly accepted and publicly checked candidate may replace the fallback.
This module owns policy, parsing and the delivery-deferral seam. Core remains
the only acceptance and promotion authority.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any, Callable

from .public_reference_evidence import method_reason

SCHEMA = "long-horizon/early-acceptance-audit/v1"
DEFAULT_MAX_ROUNDS = 2
DEFAULT_MIN_REMAINING_RATIO = 0.40
DEFAULT_MIN_REMAINING_SECONDS = 45 * 60.0
DEFAULT_PUBLICATION_RESERVE_SECONDS = 60.0
MAX_ROUNDS_LIMIT = 10

_AUDIT_BLOCK = re.compile(
    r"<pact-coverage-audit>\s*(.*?)\s*</pact-coverage-audit>", re.DOTALL | re.IGNORECASE
)
_FENCED = re.compile(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", re.DOTALL | re.IGNORECASE)
_GAP_MARKERS = (
    "pact_audit_gaps",
    "coverage gap",
    "missing requirement",
    "remaining gap",
    "not covered",
    "unverified requirement",
)
_VERDICT_CLEAN = "clean"
_VERDICT_GAPS = "gaps"
_GAP_STATUSES = {"gap", "failed", "missing", "incomplete", "unverified", "broken", "rejected"}
_OFFICIAL_VALIDATION_MARKERS = (
    "official-client-tested",
    "official client tested",
    "official client end-to-end",
    "official-client-validation",
    "official client validation",
    "官方客户端实测",
)
_SELF_TEST_MARKERS = (
    "self-test-only",
    "self-test only",
    "self test only",
    "only self-test",
    "only self test",
    "self-test",
    "仅自测",
    "仅做自测",
    "只做自测",
)
_SELF_TEST_EVIDENCE_MARKERS = (
    "self-test-only",
    "self-test only",
    "self test only",
    "only self-test",
    "only self test",
    "仅自测",
    "仅做自测",
    "只做自测",
)
_VALIDATION_KEYS = ("validation", "validation_kind", "evidence_kind", "method", "source", "test_method")
_OFFICIAL_AVAILABILITY_KEYS = (
    "official_client_available",
    "reference_client_available",
    "official_client_provided",
    "reference_client_provided",
    "client_sdk_available",
)
_NOVEL_INPUT_MARKERS = (
    "novel-input-tested",
    "novel input tested",
    "novel-input-validation",
    "novel input validation",
    "novel-input",
    "novel input",
    "new-input-tested",
    "new input tested",
    "新输入实测",
    "新输入验证",
)
_PUBLIC_FIXTURE_MARKERS = (
    "public-fixture-only",
    "public fixture only",
    "public-fixtures-only",
    "public fixtures only",
    "only-public-fixtures",
    "only public fixtures",
    "only the public fixtures",
    "only public fixture",
    "only the public fixture",
    "public-fixture-validation",
    "public fixture validation",
    "仅公开样例",
    "只测公开样例",
    "仅公开测试",
)
_NOVEL_INPUT_KEYS = (
    "novel_input_tested",
    "novel_input_validation",
    "novel_input_verified",
    "new_input_tested",
)
_PUBLIC_FIXTURE_KEYS = (
    "public_fixture_only",
    "public_fixture_validated",
    "only_public_fixtures",
)
_NOVELTY_TEXT_KEYS = (
    "validation",
    "validation_kind",
    "validation_scope",
    "evidence_kind",
    "method",
    "source",
    "test_method",
    "input",
    "input_kind",
    "input_validation",
    "novelty",
    "coverage",
    "evidence",
)
_REQUIREMENT_KEYS = ("requirement", "requirement_id", "id", "name")


@dataclass(frozen=True, slots=True)
class EarlyAuditPolicy:
    """Host-side trigger and time bounds; no candidate or grading authority."""

    max_rounds: int = DEFAULT_MAX_ROUNDS
    min_remaining_ratio: float = DEFAULT_MIN_REMAINING_RATIO
    min_remaining_seconds: float = DEFAULT_MIN_REMAINING_SECONDS
    max_seconds: float | None = None
    finish_reserve_seconds: float = 0.0
    publication_reserve_seconds: float = DEFAULT_PUBLICATION_RESERVE_SECONDS

    def __post_init__(self) -> None:
        if isinstance(self.max_rounds, bool) or not isinstance(self.max_rounds, int):
            raise ValueError("early audit max_rounds must be an integer")
        if not 0 <= self.max_rounds <= MAX_ROUNDS_LIMIT:
            raise ValueError(f"early audit max_rounds must be from 0 to {MAX_ROUNDS_LIMIT}")
        for name, value, allow_zero in (
            ("min_remaining_ratio", self.min_remaining_ratio, False),
            ("min_remaining_seconds", self.min_remaining_seconds, True),
            ("finish_reserve_seconds", self.finish_reserve_seconds, True),
            ("publication_reserve_seconds", self.publication_reserve_seconds, True),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"early audit {name} must be finite")
            if value < 0 or (not allow_zero and value <= 0):
                raise ValueError(f"early audit {name} is out of range")
        if self.min_remaining_ratio > 1:
            raise ValueError("early audit min_remaining_ratio must be at most 1")
        if self.max_seconds is not None and (
            isinstance(self.max_seconds, bool)
            or not isinstance(self.max_seconds, (int, float))
            or not math.isfinite(self.max_seconds)
            or self.max_seconds <= 0
        ):
            raise ValueError("early audit max_seconds must be positive and finite or None")

    def triggered(self, *, remaining_seconds: float, total_seconds: float) -> bool:
        """Both host thresholds must hold; zero rounds disables the audit."""
        if self.max_rounds <= 0:
            return False
        if (
            isinstance(remaining_seconds, bool)
            or not isinstance(remaining_seconds, (int, float))
            or not math.isfinite(remaining_seconds)
            or isinstance(total_seconds, bool)
            or not isinstance(total_seconds, (int, float))
            or not math.isfinite(total_seconds)
            or total_seconds <= 0
        ):
            return False
        return (
            remaining_seconds > total_seconds * self.min_remaining_ratio
            and remaining_seconds >= self.min_remaining_seconds
        )

    def time_limit(self, *, remaining_seconds: float, total_seconds: float) -> float:
        """Cap the audit at half the trial and always leave both reserves."""
        if self.max_seconds is not None:
            return float(self.max_seconds)
        residual = max(
            0.0,
            float(remaining_seconds)
            - float(self.finish_reserve_seconds)
            - float(self.publication_reserve_seconds),
        )
        return max(0.0, min(float(total_seconds) * 0.5, residual))

    def audit_deadline(
        self,
        *,
        now: float,
        soft_deadline: float,
        remaining_seconds: float,
        total_seconds: float,
    ) -> float:
        """End the audit before the soft deadline and leave fallback publish room."""
        publish_by = float(soft_deadline) - float(self.publication_reserve_seconds)
        return min(publish_by, float(now) + self.time_limit(
            remaining_seconds=remaining_seconds, total_seconds=total_seconds
        ))


@dataclass(frozen=True, slots=True)
class AuditReport:
    """Parsed gap list plus an explicit usable/clean classification."""

    gaps: tuple[str, ...] = ()
    usable: bool = False
    clean: bool = False
    checked: tuple[str, ...] = ()
    unverified: tuple[str, ...] = ()

    @property
    def gap_count(self) -> int:
        return len(self.gaps)


def _gap_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        pieces = []
        for key in ("requirement", "expected", "observed", "evidence", "detail", "reason"):
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                pieces.append(item.strip())
        if pieces:
            return " — ".join(pieces)
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value).strip()


def _requirement_gap(row: Any) -> str | None:
    if not isinstance(row, dict):
        return _gap_text(row) if row is not None else None
    status = str(row.get("status", "")).strip().casefold()
    verified = row.get("verified")
    if verified is False or status in _GAP_STATUSES:
        return _gap_text(row)
    if verified is True or status in {"verified", "passed", "ok", "complete"}:
        return None
    # An unclassified requirement is not proof of coverage.
    return _gap_text(row)


def _normalized_validation_text(value: str) -> str:
    return " ".join(value.casefold().replace("_", "-").split())


def _official_client_availability(value: dict[str, Any]) -> bool | None:
    """Read an explicit availability flag; absence leaves the strict default."""
    for key in _OFFICIAL_AVAILABILITY_KEYS:
        if key not in value:
            continue
        item = value[key]
        if isinstance(item, bool):
            return item
        if isinstance(item, str):
            text = _normalized_validation_text(item)
            if text in {"false", "no", "none", "not available", "not-available", "not provided", "unavailable"}:
                return False
            if text in {"true", "yes", "available", "provided"}:
                return True
    return None


def _row_is_self_test_only(row: dict[str, Any]) -> bool:
    """An explicit self-test-only label is not official-client validation."""
    for key in ("official_client_tested", "official_client_validation", "official_client_verified"):
        item = row.get(key)
        if item is True:
            return False
        if item is False:
            return True
    validation_text = _normalized_validation_text(
        " ".join(str(row[key]) for key in _VALIDATION_KEYS if isinstance(row.get(key), str))
    )
    if validation_text:
        if any(marker in validation_text for marker in _OFFICIAL_VALIDATION_MARKERS):
            return False
        if any(marker in validation_text for marker in _SELF_TEST_MARKERS):
            return True
    evidence = row.get("evidence")
    if isinstance(evidence, str):
        evidence_text = _normalized_validation_text(evidence)
        if any(marker in evidence_text for marker in _OFFICIAL_VALIDATION_MARKERS):
            return False
        if any(marker in evidence_text for marker in _SELF_TEST_EVIDENCE_MARKERS):
            return True
    return False


def _row_novelty_text(row: dict[str, Any]) -> str:
    return _normalized_validation_text(
        " ".join(str(row[key]) for key in _NOVELTY_TEXT_KEYS if isinstance(row.get(key), str))
    )


def _row_is_public_fixture_only(row: dict[str, Any]) -> bool:
    """An explicit public-fixture-only label is not novel-input validation."""
    for key in _PUBLIC_FIXTURE_KEYS:
        item = row.get(key)
        if item is True:
            return True
        if item is False:
            return False
    for key in _NOVEL_INPUT_KEYS:
        item = row.get(key)
        if item is True:
            return False
        if item is False:
            return True
    text = _row_novelty_text(row)
    if any(marker in text for marker in _PUBLIC_FIXTURE_MARKERS):
        return True
    return False


def _row_is_novel_input_tested(row: dict[str, Any]) -> bool:
    for key in _PUBLIC_FIXTURE_KEYS:
        item = row.get(key)
        if item is True:
            return False
        if item is False:
            return True
    for key in _NOVEL_INPUT_KEYS:
        item = row.get(key)
        if item is True:
            return True
        if item is False:
            return False
    text = _row_novelty_text(row)
    if any(marker in text for marker in _PUBLIC_FIXTURE_MARKERS):
        return False
    if any(marker in text for marker in _NOVEL_INPUT_MARKERS):
        return True
    return False


def _row_requirement_key(row: dict[str, Any]) -> str:
    for key in _REQUIREMENT_KEYS:
        item = row.get(key)
        if isinstance(item, str) and item.strip():
            return _normalized_validation_text(item)
    return ""


def _checked_items(
    value: Any, *, require_official_validation: bool = True
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return ``(checked_text, contradicted_rows)`` from an explicit checklist."""
    if not isinstance(value, list):
        return (), ()
    checked: list[str] = []
    contradicted: list[str] = []
    public_only: list[tuple[str, str]] = []
    novel_requirements: set[str] = set()
    for row in value:
        if isinstance(row, dict):
            text = _gap_text(row)
            if not text:
                continue
            ref = row.get("public_reference_check")
            if ref is not None and (not isinstance(ref, dict) or ref.get("status") != "match"):
                # The reference assessment separately routes setup failures and product defects.
                continue
            if _requirement_gap(row) is not None:
                contradicted.append(text)
                continue
            requirement_key = _row_requirement_key(row)
            public_fixture_only = _row_is_public_fixture_only(row)
            if require_official_validation and _row_is_self_test_only(row):
                message = (
                    f"{text} — validation is self-test-only; official-client end-to-end validation is required"
                )
                if public_fixture_only:
                    message += "; novel inputs outside the public fixtures are also required"
                contradicted.append(message)
            elif public_fixture_only:
                public_only.append((text, requirement_key))
            else:
                checked.append(text)
                if requirement_key and _row_is_novel_input_tested(row):
                    novel_requirements.add(requirement_key)
            continue
        text = _gap_text(row)
        if text:
            checked.append(text)
    # A function class checked on public fixtures is covered when the same
    # class also has an explicit novel-input check; otherwise it is a gap.
    for text, requirement_key in public_only:
        if requirement_key and requirement_key in novel_requirements:
            checked.append(text)
        else:
            contradicted.append(
                f"{text} — validation is public-fixture-only; novel inputs outside the public fixtures are required"
            )
    return tuple(checked), tuple(contradicted)


def _located_mismatch(receipt):
    field = receipt.get("field")
    return (
        type(receipt.get("step")) is int
        and receipt["step"] >= 0
        and isinstance(field, str)
        and (not field or field.startswith("/"))
        and len(field) <= 2048
    )


def _reference_outcomes(value, receipts):
    gaps = []
    unverified = []
    checked = value.get("checked")
    claims = (
        [row.get("public_reference_check") for row in checked if isinstance(row, dict)]
        if isinstance(checked, list)
        else []
    )
    for row in checked if isinstance(checked, list) else ():
        if not isinstance(row, dict):
            continue
        claim = row.get("public_reference_check")
        if claim is None or claim in receipts:
            continue
        if isinstance(claim, dict):
            status = claim.get("status")
            requirement = row.get("requirement")
            evidence = row.get("evidence")
            digest = claim.get("request_sha256")
            applicable = (
                isinstance(requirement, str)
                and requirement.strip()
                and isinstance(evidence, str)
                and evidence.strip()
                and isinstance(digest, str)
                and re.fullmatch(r"[0-9a-f]{64}", digest)
            )
            if applicable and (
                (status == "mismatch" and _located_mismatch(claim))
                or (status == "candidate_unavailable" and not method_reason(claim))
            ):
                location = (
                    f" at step {claim['step']}, JSON Pointer {json.dumps(claim['field'])}"
                    if status == "mismatch"
                    else ""
                )
                gaps.append(
                    f"Reproduce {status}{location} before changing the product. Requirement: {requirement[:1024]}. "
                    f"Public request SHA-256 {digest}. Reviewer reproduction evidence: {evidence[:4096]}"
                )
                continue
        unverified.append("current_candidate_comparison_unverified")
    # A standalone successful probe establishes availability only. It cannot
    # satisfy a stateful row, but can accompany a separately registered compare.
    for receipt in receipts:
        status = receipt.get("status")
        if status == "available" and receipt not in claims:
            continue
        identity = (
            receipt.get("request_sha256"),
            receipt.get("reference_argv_sha256"),
            receipt.get("candidate_argv_sha256"),
        )
        bound = (
            all(isinstance(item, str) and re.fullmatch(r"[0-9a-f]{64}", item) for item in identity)
            and identity[1] != identity[2]
        )
        if status == "match" and bound and type(receipt.get("checkpoints")) is int and receipt["checkpoints"] >= 2:
            continue
        if status == "mismatch" and bound and _located_mismatch(receipt):
            gaps.append(
                f"Public reference mismatch at step {receipt['step']}, JSON Pointer {json.dumps(receipt['field'])}."
            )
        elif status == "candidate_unavailable" and bound and not method_reason(receipt):
            gaps.append(
                f"Public reference comparison: candidate_unavailable ({receipt.get('reason')}, exit {receipt.get('exit_code')})."
            )
        else:
            unverified.append(method_reason(receipt) or "incomplete_comparison")
    return list(dict.fromkeys(gaps)), tuple(dict.fromkeys(unverified))


def _parse_json_report(value: Any, *, text: str = "", reference_receipts=()) -> AuditReport:
    if isinstance(value, list):
        # A bare list has no explicit verdict and cannot prove clean coverage.
        gaps = tuple(item for item in (_gap_text(row) for row in value) if item)
        return AuditReport(
            gaps=gaps or ("Reviewer returned an unclassified list, not an explicit clean verdict.",),
            usable=False,
            clean=False,
        )
    if not isinstance(value, dict):
        return AuditReport(gaps=("Reviewer returned no structured coverage report.",), usable=False)
    verdict = value.get("verdict")
    verdict = verdict.strip().casefold() if isinstance(verdict, str) else ""
    raw_gaps = value.get("gaps")
    gaps, unverified = _reference_outcomes(value, reference_receipts)
    if isinstance(raw_gaps, list):
        gaps.extend(item for item in (_gap_text(row) for row in raw_gaps) if item)
    elif raw_gaps is not None:
        item = _gap_text(raw_gaps)
        if item:
            gaps.append(item)
    requirements = value.get("requirements")
    if isinstance(requirements, list):
        for row in requirements:
            item = _requirement_gap(row)
            if item:
                gaps.append(item)
    official_client_available = _official_client_availability(value)
    checked, contradicted = _checked_items(
        value.get("checked"), require_official_validation=official_client_available is not False
    )
    gaps.extend(contradicted)
    unique = tuple(dict.fromkeys(gaps))
    if unverified:
        return AuditReport(gaps=unique, usable=True, checked=checked, unverified=unverified)
    if verdict not in {_VERDICT_CLEAN, _VERDICT_GAPS}:
        return AuditReport(
            gaps=unique or ("Reviewer did not return an explicit verdict.",),
            usable=False,
            clean=False,
            checked=checked,
        )
    if verdict == _VERDICT_GAPS:
        return AuditReport(
            gaps=unique or ("Reviewer reported coverage gaps without listing them.",),
            usable=True,
            clean=False,
            checked=checked,
        )
    # ``clean`` is accepted only when it is explicit, gap-free, and backed by a
    # nonempty checklist of requirements the Reviewer actually verified.
    if unique or not checked:
        missing = () if unique else ("Reviewer marked coverage clean without a nonempty checked list.",)
        return AuditReport(
            gaps=unique or missing,
            usable=True,
            clean=False,
            checked=checked,
        )
    if any(marker in text.casefold() for marker in _GAP_MARKERS):
        return AuditReport(
            gaps=("Reviewer contradicted its clean verdict with a reported coverage gap.",),
            usable=True,
            clean=False,
            checked=checked,
        )
    return AuditReport(usable=True, clean=True, checked=checked)


def parse_audit_report(text: str, *, reference_receipts=()) -> AuditReport:
    """Parse the Reviewer's explicit report; do not invent coverage from prose."""
    if not isinstance(text, str) or not text.strip():
        return AuditReport(usable=False)
    candidates: list[str] = []
    candidates.extend(match.group(1) for match in _AUDIT_BLOCK.finditer(text))
    candidates.extend(match.group(1) for match in _FENCED.finditer(text))
    stripped = text.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        candidates.append(stripped)
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        report = _parse_json_report(value, text=text, reference_receipts=reference_receipts)
        if report.usable or report.gaps:
            return report
    lower = text.casefold()
    if any(marker in lower for marker in _GAP_MARKERS):
        return AuditReport(
            gaps=("Reviewer reported unresolved coverage gaps.",),
            usable=True,
            clean=False,
            checked=(),
        )
    # Free-form prose, clean-sounding or otherwise, is never proof of coverage.
    return AuditReport(
        gaps=("Reviewer did not return a usable structured coverage report.",),
        usable=False,
        clean=False,
        checked=(),
    )


class DeferredDeliveryRuntime:
    """Delegate every Core operation except one early delivery.

    The ordinary driver delivers immediately after the first acceptance. During
    an eligible early acceptance the controller freezes that candidate and runs
    the bounded audit instead; the driver still receives a valid accepted
    snapshot, but the Work remains undelivered so a new candidate can be tried.
    """

    def __init__(self, runtime: Any, should_defer: Callable[[], bool]) -> None:
        self._runtime = runtime
        self._should_defer = should_defer
        self.deferred = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._runtime, name)

    def begin_execution(self, *args: Any, **kwargs: Any) -> Any:
        # Core starts a Worker checkout here. This is the last safe moment to
        # reject a delayed writer before that view is handed to the Worker.
        from .reviewer_execution import release_recovered_views

        workspace = getattr(self._runtime, "workspace", None)
        if workspace is not None:
            release_recovered_views((workspace,))
        return self._runtime.begin_execution(*args, **kwargs)

    def deliver(self, work_id: str, *, expected_revision: int, acceptance: Any) -> Any:
        if not self.deferred and self._should_defer():
            state = self._runtime.read_work(work_id)
            if state.acceptance == acceptance and state.delivery is None:
                self.deferred = True
                return state
        return self._runtime.deliver(
            work_id, expected_revision=expected_revision, acceptance=acceptance
        )
