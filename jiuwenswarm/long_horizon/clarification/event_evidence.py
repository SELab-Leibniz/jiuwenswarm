"""Join native Jiuwen tool events and bind read evidence to the frozen view."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path

from .budget import SideCallIntegrityError
from .compat import validate_link_free_directory
from .snapshot import WorkspaceSnapshot
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


_PAGE_START = re.compile(r"^\[read_file (?:page=|last=)", re.MULTILINE)
_PAGE_FOOTER = re.compile(
    r"\[read_file (?P<last>last=true )?page=[1-9][0-9]* source_id=(?P<id>rfp1_[a-f0-9]{32}) "
    r"rows=(?P<start>[0-9]+)-(?P<end>[0-9]+) "
    r"(?:next_offset=(?P<next>[0-9]+) has_more=true last=false|complete=(?:true|false)) "
    r"full_sha256=(?P<sha>[a-f0-9]{64})(?: total_lines=(?P<total>[0-9]+))?\]"
)


def _read_integer(value: object) -> int:
    if type(value) is int or isinstance(value, str):
        return int(value)
    raise ValueError("invalid read window")


def _receipt_range(source: str, result: str, skip: int,
                   limit: int | None, cursor: object) -> tuple[int, int] | None:
    """Verify the native reader's final footer and every page line against the frozen file."""
    body, separator, footer = result.rstrip("\n").rpartition("\n\n")
    match = _PAGE_FOOTER.fullmatch(footer)
    if not separator or not match or match["sha"] != hashlib.sha256(source.encode("utf-8")).hexdigest():
        return None
    # The native reader splits raw files on LF and strips a CR before it.
    # Unicode separators inside a physical line must not shift later rows.
    source_lines = source.removesuffix("\n").split("\n")
    start, end = int(match["start"]), int(match["end"])
    if not 1 <= start <= end <= len(source_lines) or (limit is not None and end - start + 1 > limit):
        return None
    if match["last"]:
        if match["next"] or match["total"] != str(len(source_lines)) or end != len(source_lines):
            return None
    elif match["total"] or match["next"] != str(end) or end >= len(source_lines):
        return None

    if cursor is not None:
        if isinstance(cursor, str):
            cursor = {"source_id": cursor}
        if not isinstance(cursor, dict) or cursor.get("source_id") not in (None, match["id"]):
            return None
        if any(key in cursor for key in ("next_offset", "offset", "next_byte")):
            offset = cursor.get("next_offset", cursor.get("offset", 0)) or 0
            if max(0, _read_integer(offset)) + 1 != start:
                return None
        byte = _read_integer(cursor.get("next_byte") or 0)
        if byte and byte != sum(len(line.encode("utf-8")) + 1 for line in source_lines[:start - 1]):
            # A supplied seek position can point at identical text elsewhere
            # in this file. The native reader trusts its separate row counter.
            return None
    elif (skip or limit is not None) and start != skip + 1:
        return None

    page = [line.removesuffix("\r") for line in source_lines[start - 1:end]]
    # _read_page_bytes strips trailing newlines before _cat_n numbers the
    # visible rows. The footer still counts those unrendered empty rows.
    while page and not page[-1]:
        page.pop()
    if not any(line.strip() for line in page):
        return None
    # _cat_n labels display fragments; one physical row can produce several.
    fragments = "\n".join(page).splitlines()
    expected = "\n".join(f"{i:>6}\t{line}" for i, line in enumerate(fragments, 1))
    return (start, start + len(page) - 1) if body == expected else None


_DISPLAY_BREAK = re.compile(r"[\v\f\x1c-\x1e\x85\u2028\u2029]|\r(?!\n)")


def _lf_lines(text: str) -> list[str]:
    return [line.removesuffix("\r") for line in text.removesuffix("\n").split("\n")] if text else []


def _native_fragment_range(lines: list[str], numbered: list[tuple[int, str]],
                           skip: int, count: int | None, final_lf: bool) -> tuple[int, int] | None:
    """Map a complete legacy display window to the LF rows it covers in full."""
    if any(n != expected for expected, (n, _) in enumerate(numbered, 1)):
        return None
    # Q7's filesystem slices splitlines(keepends=True) before _cat_n renders
    # each fragment. The current local reader can also return an unmarked
    # whole file, starting at zero, with several labels per physical row.
    stop = skip + len(numbered)
    if skip and count is not None and len(numbered) > count:
        return None
    fragments: list[str] = []
    complete: list[int] = []
    for row, line in enumerate(lines, 1):
        start = len(fragments)
        # Appending LF preserves interior/trailing empty display fragments.
        text = line + "\n" if row < len(lines) or final_lf else line
        fragments.extend(text.splitlines())
        # A final empty fragment can represent only the physical LF after
        # a display delimiter. Both SDKs may omit it at a read boundary.
        content_end = len(fragments) - (text.endswith("\n") and fragments[-1] == "")
        if skip <= start and content_end <= stop and line.strip():
            complete.append(row)
        if len(fragments) >= stop:
            break
    if fragments[skip:stop] != [text for _, text in numbered] or not complete:
        return None
    if not skip and count is not None and complete[-1] > count:
        return None
    return complete[0], complete[-1]


def _observed_lines(source: str, result: str, *, offset: object = 0,
                    limit: object = None, cursor: object = None) -> tuple[int, int] | None:
    """Bind exact returned text to absolute positions inside the paired read's window."""
    # JSON tool arguments may contain decimal strings. Reject malformed windows
    # rather than falling back to a different part of the frozen file.
    try:
        skip = max(0, _read_integer(offset))
        count = None if limit is None else _read_integer(limit)
        if _PAGE_START.search(result):
            return _receipt_range(source, result, skip, count, cursor)
        if cursor is not None:
            return None  # The actual starting line of a continuation needs its receipt.
        source_lines = _lf_lines(source)
        end = len(source_lines) if count is None else min(len(source_lines), skip + count)
    except ValueError:
        return None
    if count is not None and count <= 0:
        return None
    # Native read_file emits a padded line number, one tab, then the exact
    # source line. Whitespace after that tab belongs to the source: removing
    # even one character rejects every indented Python line. Other renderers
    # use a colon/pipe/arrow with one optional presentation space.
    numbered_lines: list[tuple[int, str]] = []
    native_tabs = True
    for line in _lf_lines(result):
        numbered = re.match(r"^[ \t]*(\d+) *\t(.*)$", line)
        if numbered is None:
            numbered = re.match(r"^[ \t]*(\d+)[ \t]*[:|→] ?(.*)$", line)
            if numbered is not None:
                native_tabs = False
        if numbered is not None:
            numbered_lines.append((int(numbered.group(1)), numbered.group(2)))
    if numbered_lines and native_tabs and _DISPLAY_BREAK.search(source) and numbered_lines[0][0] == 1:
        return _native_fragment_range(source_lines, numbered_lines, skip, count, source.endswith("\n"))
    if skip >= end:
        return None
    if numbered_lines:
        # Native _cat_n starts at 1 for each page. Identify its complete
        # numbering before checking content: repeated source text cannot choose
        # the origin. Other renderers and full-file reads use absolute numbers.
        local_page = skip > 0 and native_tabs and numbered_lines[0][0] == 1
        if local_page and (
            len(numbered_lines) > end - skip
            or any(n != expected for expected, (n, _) in enumerate(numbered_lines, 1))
        ):
            return None
        check_overlap = skip > 0 and native_tabs and not local_page
        if check_overlap:
            absolute_fits = all(skip < n <= end and source_lines[n - 1] == text for n, text in numbered_lines)
            shifted_fits = all(0 < n <= end - skip and source_lines[skip + n - 1] == text
                               for n, text in numbered_lines)
            # A complete, uniquely absolute excerpt can contain repeated rows.
            check_overlap = not absolute_fits or shifted_fits
        matched_numbers = set()
        for number, content in numbered_lines:
            index = skip + number if local_page else number
            if not (skip < index <= end and content.strip() and source_lines[index - 1] == content):
                continue
            if (check_overlap and skip + number <= end
                    and source_lines[skip + number - 1] == content):
                # A partial tab-numbered excerpt with repeated text could also
                # refer to a different row inside this page.
                continue
            matched_numbers.add(index)
        matched = sorted(matched_numbers)
    elif skip:
        # A plain excerpt has no line labels. Require one exact alignment in
        # the requested window, including indentation, blank and repeated lines.
        excerpt = _lf_lines(result)
        if not excerpt:
            return None
        starts = (i for i in range(skip, end - len(excerpt) + 1)
                  if source_lines[i] == excerpt[0] and source_lines[i:i + len(excerpt)] == excerpt)
        start = next(starts, None)
        if start is None or next(starts, None) is not None:
            return None
        matched = [start + i for i, line in enumerate(excerpt, 1) if line.strip()]
    else:
        output_lines = set(_lf_lines(result))
        matched = [index for index, line in enumerate(source_lines[:end], 1)
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
                    source = target[1].read_bytes().decode("utf-8")
                    observed = _read_text(raw)
                    rendered = event.get("rendered_result")
                    cursor = args.get("cursor")
                    if cursor is None:
                        cursor = args.get("page_token")
                    if cursor is None and args.get("source_id"):
                        cursor = {"source_id": args["source_id"]}
                    window = {"offset": args.get("offset", 0), "limit": args.get("limit"), "cursor": cursor}
                    views = [observed]
                    if isinstance(rendered, str) and rendered and rendered != observed:
                        views.append(rendered)
                    marked = [view for view in views if _PAGE_START.search(view)]
                    if marked or args.get("offset"):
                        # A footer is authoritative, including when it fails
                        # verification. Never undo it with unlabelled raw text.
                        ranges = {_observed_lines(source, text, **window) for text in marked or views}
                        if not marked:
                            ranges.discard(None)
                        line_range = ranges.pop() if len(ranges) == 1 else None
                    else:
                        line_range = _observed_lines(source, "\n".join(views), **window)
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
        for line in _lf_lines(result):
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
