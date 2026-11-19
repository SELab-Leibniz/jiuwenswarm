"""Join native Jiuwen tool events and bind read evidence to the frozen view."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any

from .snapshot import WorkspaceSnapshot
from .compat import validate_link_free_directory
from .budget import SideCallIntegrityError
from .types import ClarificationProposal, InvestigationToolCall, VerifiedEvidenceReference
from .validation import _anchor_line_range, _proposal_current_repo_anchors


def snapshot_workspace(snapshot: WorkspaceSnapshot) -> Path:
    roots = snapshot.roots
    if not roots:
        raise ValueError("snapshot has no model view")
    primary = next((root for root in roots if root.is_primary), roots[0])
    return Path(primary.view_root).resolve(strict=True)


def validate_model_view(snapshot: WorkspaceSnapshot) -> None:
    """Validate the captured object, not a model-editable manifest in its tree."""
    for root_info in snapshot.roots:
        root = Path(root_info.view_root)
        try:
            validate_link_free_directory(root)
            if root.is_symlink() or not root.is_dir():
                raise ValueError("frozen snapshot root changed")
        except (OSError, ValueError) as exc:
            raise SideCallIntegrityError("frozen snapshot root changed") from exc
        if root_info.model_view_directories is None:
            raise SideCallIntegrityError("frozen snapshot lacks a captured model-view directory inventory")
        expected = {entry.relative_path: entry for entry in root_info.entries if entry.model_visible}
        for relative, entry in expected.items():
            path = root / relative
            try:
                info = path.lstat()
                resolved = path.resolve(strict=True)
                resolved.relative_to(root.resolve(strict=True))
                if entry.kind == "file":
                    if not stat.S_ISREG(info.st_mode) or resolved != path.absolute():
                        raise ValueError("frozen regular file became a symlink")
                    if hashlib.sha256(path.read_bytes()).hexdigest() != entry.content_hash:
                        raise ValueError("frozen source bytes changed")
                elif (not stat.S_ISLNK(info.st_mode) or entry.model_symlink_target is None
                      or os.readlink(path) != entry.model_symlink_target):
                    raise ValueError("frozen symlink target or type changed")
            except (OSError, ValueError, RuntimeError) as exc:
                raise SideCallIntegrityError(f"frozen snapshot differs from captured evidence: {relative}") from exc

        def scan_error(error: OSError) -> None:
            raise SideCallIntegrityError(f"incomplete frozen snapshot scan: {error}") from error

        directories = {"."}
        try:
            for directory, dirs, files in os.walk(root, followlinks=False, onerror=scan_error):
                current = Path(directory)
                descend = []
                # Directory symlinks must be checked as entries before pruning
                # traversal. Neither ignore rules nor source exclusions apply
                # to the already-materialized model view.
                for name in [*dirs, *files]:
                    path = current / name
                    relative = path.relative_to(root).as_posix()
                    info = path.lstat()
                    if stat.S_ISDIR(info.st_mode):
                        if relative == ".git" and root_info.history_bundle:
                            continue
                        directories.add(relative)
                        descend.append(name)
                    elif relative == ".jiuwenswarm_snapshot_manifest.json" and stat.S_ISREG(info.st_mode):
                        continue
                    elif relative not in expected:
                        raise SideCallIntegrityError(f"unexpected source appeared in frozen snapshot: {relative}")
                dirs[:] = descend
        except OSError as exc:
            raise SideCallIntegrityError("frozen snapshot changed during inventory scan") from exc
        if directories != set(root_info.model_view_directories):
            raise SideCallIntegrityError("frozen snapshot directory inventory changed")


def _text(value: object) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=True, sort_keys=True) if value is not None else ""


def _read_text(value: object, depth: int = 0) -> str:
    if isinstance(value, str):
        return value
    if depth >= 4:
        return ""
    if isinstance(value, dict):
        return "\n".join(_read_text(value[key], depth + 1) for key in
                         ("content", "text", "output", "data", "result") if key in value)
    if isinstance(value, list):
        return "\n".join(_read_text(item, depth + 1) for item in value)
    return ""


def _arguments(value: object) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _relative_path(value: str, root: Path) -> tuple[str, Path] | None:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve(strict=True)
        relative = resolved.relative_to(root)
    except (OSError, ValueError):
        return None
    if ".git" in relative.parts or (relative.parts and relative.parts[0] == ".jiuwenswarm_snapshot_manifest.json"):
        return None
    return relative.as_posix(), resolved


def _observed_lines(source: str, result: str) -> tuple[int, int] | None:
    """Require source bytes in an actual read result, allowing line-number prefixes."""
    source_lines = source.splitlines()
    # Native read_file emits a padded line number, one tab, then the exact
    # source line. Whitespace after that tab belongs to the source: removing
    # even one character rejects every indented Python line. Other renderers
    # use a colon/pipe/arrow with one optional presentation space.
    numbered_lines: list[tuple[int, str]] = []
    for line in result.splitlines():
        numbered = re.match(r"^[ \t]*(\d+) *\t(.*)$", line)
        if numbered is None:
            numbered = re.match(r"^[ \t]*(\d+)[ \t]*[:|→] ?(.*)$", line)
        if numbered is not None:
            numbered_lines.append((int(numbered.group(1)), numbered.group(2)))
    if numbered_lines:
        # A native line number is evidence for that exact source position, not
        # every identical source line. Never fall back to stripped/plain text
        # when numbered output exists, even when its claimed positions fail.
        matched = sorted({number for number, content in numbered_lines
                          if 1 <= number <= len(source_lines)
                          and content.strip() and source_lines[number - 1] == content})
    else:
        output_lines = set(result.splitlines())
        matched = [index for index, line in enumerate(source_lines, 1)
                   if line.strip() and line in output_lines]
    if not matched:
        return None
    # Do not convert disjoint returned snippets into a claim that intervening
    # lines were read. Keep the longest contiguous observed range.
    runs: list[list[int]] = []
    for index in matched:
        if runs and all(not source_lines[n - 1].strip() for n in range(runs[-1][-1] + 1, index)):
            runs[-1].append(index)
        else:
            runs.append([index])
    selected = max(runs, key=len)
    return selected[0], selected[-1]


def investigation_tool_calls(events: list[dict], snapshot: WorkspaceSnapshot) -> list[InvestigationToolCall]:
    """A claimed path or model-written final JSON can never become read evidence."""
    from ..worker import normalize_tool_events

    normalized = normalize_tool_events(events)
    root = snapshot_workspace(snapshot)
    pending: dict[tuple[str, str, str], dict] = {}
    duplicate: set[tuple[str, str, str]] = set()
    calls: list[InvestigationToolCall] = []
    aliases = {"read": "read_file", "read_file": "read_file", "grep": "grep", "glob": "glob", "view_image": "view_image"}
    for event in normalized:
        call_id = str(event.get("call_id") or "")
        if not call_id:
            continue
        provenance = event.get("provenance") or {}
        key = (str(provenance.get("session_id") or ""), str(provenance.get("request_id") or ""), call_id)
        if event.get("type") == "tool_call":
            if key in pending:
                duplicate.add(key)
            pending[key] = event
            continue
        if event.get("type") != "tool_result" or key not in pending or key in duplicate:
            continue
        call = pending.pop(key)
        name = aliases.get(str(call.get("name", "")).lower())
        if name is None:
            continue
        result_name = str(event.get("name", "")).lower()
        if result_name and aliases.get(result_name) != name:
            continue
        args = _arguments(call.get("arguments"))
        raw = event.get("result", event.get("output"))
        result = _text(raw)
        success = event.get("is_error") is False
        path = str(args.get("path") or args.get("file_path") or args.get("filePath") or "")
        query = str(args.get("query") or args.get("pattern") or args.get("glob") or "")
        line_range = None
        if name == "read_file":
            target = _relative_path(path, root) if path else None
            if target is None or not target[1].is_file():
                success = False
            else:
                path = target[0]
                try:
                    source = target[1].read_text(encoding="utf-8")
                    observed = _read_text(raw)
                    if isinstance(event.get("rendered_result"), str):
                        observed += "\n" + event["rendered_result"]
                    line_range = _observed_lines(source, observed)
                except (OSError, UnicodeError):
                    success = False
                else:
                    success = success and line_range is not None
        elif name in {"grep", "glob"}:
            target = _relative_path(path or ".", root)
            success = success and target is not None and bool(query.strip()) and bool(result.strip())
            if target is not None:
                path = target[0]
        else:
            # Images cannot stand in for the required text read.
            success = False
        evidence_call_id = f"{key[1]}:{call_id}" if key[1] else call_id
        calls.append(InvestigationToolCall(
            call_id=evidence_call_id[:300], name=name, query=query[:1200], path=path[:1200],
            line_range=line_range, successful=success, result_chars=len(result),
            result_sha256=hashlib.sha256(result.encode()).hexdigest(),
        ))
    return calls[-100:]


def discovered_source_paths(events: list[dict], snapshot: WorkspaceSnapshot) -> list[str]:
    """Hints from paired successful searches, never model-claimed file paths."""
    from ..worker import normalize_tool_events

    accepted = {call.call_id for call in investigation_tool_calls(events, snapshot)
                if call.successful and call.name == "grep"}
    root = snapshot_workspace(snapshot)
    candidates: list[str] = []
    for event in normalize_tool_events(events):
        if event.get("type") != "tool_result" or event.get("name") != "grep":
            continue
        request_id = (event.get("provenance") or {}).get("request_id")
        call_id = f"{request_id}:{event['call_id']}" if request_id else event["call_id"]
        if call_id[:300] not in accepted:
            continue
        result = _read_text(event.get("result")) + "\n" + _read_text(event.get("rendered_result"))
        for line in result.splitlines():
            candidate = line.strip().removeprefix("File: ").split(":", 1)[0].strip()
            if not candidate or len(candidate) >= 4096 or any(char in candidate for char in "{}[]\t"):
                continue
            target = _relative_path(candidate, root)
            if target is not None and target[1].is_file() and target[1].suffix and target[0] not in candidates:
                candidates.append(target[0])
                if len(candidates) >= 250:
                    return candidates
    return candidates


def verified_evidence(proposal: ClarificationProposal, calls: list[InvestigationToolCall]) -> tuple[list[VerifiedEvidenceReference], list[str]]:
    """Require an unambiguous exact relative path and a read-covered line range."""
    references: list[VerifiedEvidenceReference] = []
    errors: list[str] = []
    reads = [call for call in calls if call.successful and call.name == "read_file"]
    for anchor in _proposal_current_repo_anchors(proposal):
        cited = re.sub(r":\d+(?:-\d+)?$", "", anchor.strip()).removeprefix("./")
        matches = [call for call in reads if call.path == cited]
        if not matches:
            errors.append(f"evidence is not an exact inspected frozen path: {anchor}")
            continue
        start, end = _anchor_line_range(anchor, cited)
        match = next((call for call in matches if start is None or (
            call.line_range is not None and call.line_range[0] <= start <= (end or start) <= call.line_range[1]
        )), None)
        if match is None:
            errors.append(f"evidence line range was not observed in read output: {anchor}")
            continue
        references.append(VerifiedEvidenceReference(
            path=cited, line_start=start, line_end=end, claim=anchor,
            tool_call_id=match.call_id, result_sha256=match.result_sha256,
        ))
    return references, errors
