# Copyright (c) 2026 Chrys. All rights reserved.
# Deterministic evidence validators ported from Chrys 6fcfb3321892735631b49f33236b1e2a988ded2d.
# No Chrys agent, session, kernel, or model code is imported.
from __future__ import annotations
import re
from .types import ClarificationProposal, InvestigationToolCall, VerifiedEvidenceReference

_PLACEHOLDER_PATTERNS = (
    re.compile(
        r"^\s*(?:placeholder|todo|pending|test|no|n/?a|tbd|beginning|probe_start|initial_exploration)\s*$",
        re.IGNORECASE,
    ),
    re.compile(r"\bwill\s+(?:refine|investigate|inspect)\b", re.IGNORECASE),
    re.compile(r"\bneed(?:s|ed)?\s+to\s+(?:inspect|investigate)\b", re.IGNORECASE),
    re.compile(r"^\s*need(?:s|ed)?\s+to\s+(?:confirm|determine|verify|check)\b", re.IGNORECASE),
    re.compile(r"\binvestigat(?:e|ing|ion)\b.*\b(?:first|pending|before)\b", re.IGNORECASE),
    re.compile(r"\bno\s+guidance\s+yet\b", re.IGNORECASE),
    re.compile(r"\bto\s+be\s+replaced\b", re.IGNORECASE),
    re.compile(r"\b(?:schema|validation)\b.*\b(?:not\s+provided|switch\s+to)\b", re.IGNORECASE),
    re.compile(r"\bsynthetic\s+investiga(?:t)?ion\b", re.IGNORECASE),
)
_INVESTIGATION_MAX_ITERATIONS = 10

def _contains_placeholder(value: str) -> bool:
    return any(pattern.search(value) for pattern in _PLACEHOLDER_PATTERNS)

def _investigation_errors(calls: list[InvestigationToolCall]) -> list[str]:
    successful = [call for call in calls if call.successful]
    errors: list[str] = []
    if not any(call.name == "grep" for call in successful):
        errors.append("investigation has no successful grep")
    elif not any(call.name == "grep" and call.query.strip() for call in successful):
        errors.append("investigation has no recorded search query")
    if not any(call.name == "read_file" for call in successful):
        errors.append("investigation has no successful read_file")
    return errors


def _normalized_path(value: str) -> str:
    normalized = value.replace("\\", "/").strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.rstrip("/")


def _path_aliases(value: str) -> tuple[str, ...]:
    normalized = _normalized_path(value)
    if not normalized:
        return ()
    parts = [part for part in normalized.split("/") if part]
    aliases = [normalized]
    aliases.extend("/".join(parts[index:]) for index in range(1, max(1, len(parts) - 1)))
    if len(parts) == 1:
        aliases.append(parts[0])
    return tuple(dict.fromkeys(aliases))


def _anchor_read_paths(anchor: str, read_paths: tuple[str, ...]) -> tuple[str, ...]:
    normalized_anchor = anchor.replace("\\", "/")
    matched = tuple(
        path for path in read_paths if any(alias and alias in normalized_anchor for alias in _path_aliases(path))
    )
    if matched:
        return matched
    # A file at the repository root has no directory component to cite, and the
    # suffix aliases above never go down to a single segment -- so `pyproject.toml`
    # was uncitable no matter how carefully it had been read. Fall back to the
    # bare name, but only for an anchor that names no directory at all, and only
    # when exactly one inspected path carries it: an anchor that DID name a
    # directory and still missed is a real miss, and a name shared by several
    # inspected files is the ambiguity the suffix rule exists to prevent.
    cited = normalized_anchor.split(":", 1)[0].strip()
    if not cited or "/" in cited:
        return ()
    by_name = tuple(path for path in read_paths if path.rsplit("/", 1)[-1] == cited)
    return by_name if len(by_name) == 1 else ()


def _proposal_current_repo_anchors(proposal: ClarificationProposal) -> list[str]:
    anchors = [*proposal.evidence]
    anchors.extend(anchor for finding in proposal.coverage for anchor in finding.evidence)
    anchors.extend(anchor for point in proposal.guidance_points for anchor in point.evidence)
    return list(dict.fromkeys(anchor.anchor for anchor in anchors if anchor.kind == "current_repo"))


def _anchor_line_range(anchor: str, path: str) -> tuple[int | None, int | None]:
    normalized_anchor = anchor.replace("\\", "/")
    for alias in sorted(_path_aliases(path), key=len, reverse=True):
        match = re.search(rf"{re.escape(alias)}:(\d+)(?:-(\d+))?", normalized_anchor)
        if match is None:
            continue
        start = int(match.group(1))
        return start, int(match.group(2)) if match.group(2) else start
    return None, None


def _verified_evidence(
    proposal: ClarificationProposal,
    calls: list[InvestigationToolCall],
) -> list[VerifiedEvidenceReference]:
    successful_reads = [
        call
        for call in calls
        if call.successful and call.name == "read_file" and call.path and call.call_id and call.result_sha256
    ]
    verified: list[VerifiedEvidenceReference] = []
    seen: set[tuple[str, str]] = set()
    for anchor in _proposal_current_repo_anchors(proposal):
        matched = next(
            (call for call in successful_reads if _anchor_read_paths(anchor, (_normalized_path(call.path),))),
            None,
        )
        if matched is None:
            continue
        path = _normalized_path(matched.path)
        key = path, anchor
        if key in seen:
            continue
        seen.add(key)
        line_start, line_end = _anchor_line_range(anchor, path)
        verified.append(
            VerifiedEvidenceReference(
                path=path,
                line_start=line_start,
                line_end=line_end,
                claim=anchor,
                tool_call_id=matched.call_id,
                result_sha256=matched.result_sha256,
            )
        )
    return verified


def _successful_search_queries(calls: list[InvestigationToolCall]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            call.query.strip()
            for call in calls
            if call.successful and call.name in {"grep", "glob"} and call.query.strip()
        )
    )


def _successful_read_paths(calls: list[InvestigationToolCall]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            _normalized_path(call.path)
            for call in calls
            if call.successful and call.name == "read_file" and _normalized_path(call.path)
        )
    )


def _proposal_semantic_errors(proposal: ClarificationProposal) -> list[str]:
    errors: list[str] = []
    text_fields = [proposal.rationale]
    text_fields.extend(anchor.anchor for anchor in proposal.evidence)
    for finding in proposal.coverage:
        text_fields.append(finding.summary)
        text_fields.extend(anchor.anchor for anchor in finding.evidence)
    for point in proposal.guidance_points:
        text_fields.extend((point.statement, point.decision_impact, point.risk))
        text_fields.extend(anchor.anchor for anchor in point.evidence)
    if any(_contains_placeholder(value) for value in text_fields):
        errors.append("proposal contains placeholder or unfinished investigation text")
    return errors


def _proposal_coverage_errors(
    proposal: ClarificationProposal,
    calls: list[InvestigationToolCall],
) -> list[str]:
    errors: list[str] = []
    read_paths = _successful_read_paths(calls)
    search_queries = _successful_search_queries(calls)
    all_anchors = [
        *proposal.evidence,
        *(anchor for finding in proposal.coverage for anchor in finding.evidence),
        *(anchor for point in proposal.guidance_points for anchor in point.evidence),
    ]
    current_repo_anchors = [anchor for anchor in all_anchors if anchor.kind == "current_repo"]
    if not current_repo_anchors:
        errors.append("proposal has no current-repository evidence")
    uncited = [anchor.anchor for anchor in current_repo_anchors if not _anchor_read_paths(anchor.anchor, read_paths)]
    if uncited:
        # Name them: the rejected proposal is not persisted, so without the
        # offending anchors the artifact records that a gate fired but not
        # what tripped it -- and a bare file name looks identical to an
        # invented one in the record.
        errors.append(
            "current-repository evidence cites a file not inspected with read_file: "
            + ", ".join(sorted(dict.fromkeys(uncited))[:5])
        )
    for point in proposal.guidance_points:
        if not any(anchor.kind in {"current_repo", "exact_ancestor"} for anchor in point.evidence):
            errors.append("guidance point has no current-repository or exact-ancestor evidence")
            break
    errors.extend(
        f"{finding.target} coverage has no current-repository evidence"
        for finding in proposal.coverage
        if not any(anchor.kind == "current_repo" for anchor in finding.evidence)
    )
    if proposal.verdict == "requirement_complete":
        if len(read_paths) < 2 and len(search_queries) < 2:
            errors.append("requirement_complete lacks a second inspected surface or targeted search")
        cited_paths = {
            path
            for anchor in proposal.evidence
            if anchor.kind == "current_repo"
            for path in _anchor_read_paths(anchor.anchor, read_paths)
        }
        if len(read_paths) >= 2 and len(cited_paths) < 2:
            errors.append("requirement_complete does not cite two inspected repository surfaces")
    return list(dict.fromkeys(errors))


def _proposal_errors(proposal: ClarificationProposal, calls: list[InvestigationToolCall]) -> list[str]:
    return [*_proposal_semantic_errors(proposal), *_proposal_coverage_errors(proposal, calls)]
