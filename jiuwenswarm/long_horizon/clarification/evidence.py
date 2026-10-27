# Copyright (c) 2026 Chrys. All rights reserved.

# Ported from Chrys 6fcfb3321892735631b49f33236b1e2a988ded2d: src/chrys/service/requirement_clarification/evidence.py; solver execution is JiuwenSwarm.
"""Deterministic frozen-repository evidence packets for proposal prompts."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from .compat import find_rg
from .snapshot import WorkspaceSnapshot

_PACKET_MAX_CHARS = 8000
_TERMS_MAX = 10
_HITS_PER_TERM = 6
_HITS_MAX = 60
_ANCESTOR_COMMITS_PER_TERM = 3
_ANCESTOR_COMMITS_PER_ROOT = 6
_STOP_WORDS = frozenset(
    {
        "actual",
        "additional",
        "application",
        "arguments",
        "behavior",
        "changes",
        "current",
        "description",
        "expected",
        "feature",
        "files",
        "function",
        "implement",
        "implementation",
        "introduced",
        "module",
        "option",
        "requirements",
        "should",
        "steps",
        "tests",
        "using",
    }
)


def extract_search_terms(requirement: str) -> list[str]:
    """Extract a stable, high-signal term list from user-authored text."""
    marked = re.findall(r"`([^`]{2,100})`", requirement)
    names = re.findall(r"(?im)^\s*Name:\s*`?([A-Za-z_][A-Za-z0-9_.-]*)", requirement)
    marked_words = [
        word
        for value in [*names, *marked]
        for word in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", value)
        if word.casefold() not in _STOP_WORDS
    ]
    prose_words = [
        word for word in re.findall(r"[A-Za-z_][A-Za-z0-9_]{4,}", requirement) if word.casefold() not in _STOP_WORDS
    ]
    counts: dict[str, int] = {}
    for word in prose_words:
        normalized = word.casefold()
        counts[normalized] = counts.get(normalized, 0) + 1
    ordered = list(dict.fromkeys(marked_words))
    seen = {word.casefold() for word in ordered}
    ordered.extend(
        word for word, _count in sorted(counts.items(), key=lambda item: (-item[1], item[0])) if word not in seen
    )
    return ordered[:_TERMS_MAX]


def collect_base_evidence(snapshot: WorkspaceSnapshot, requirement: str) -> str:
    """Search frozen HEAD/views without consulting the live P0 workspace."""
    terms = extract_search_terms(requirement)
    root_lines = [
        f"- root {index}: {root.label or Path(root.source_root).name}; frozen HEAD={root.git_head or '[non-git]'}"
        for index, root in enumerate(snapshot.roots, start=1)
    ]
    if not terms:
        return "Frozen roots:\n" + "\n".join(root_lines) + "\nNo task-specific source terms were extractable."

    hits: list[str] = []
    for root_index, root in enumerate(snapshot.roots, start=1):
        view = Path(root.view_root)
        ancestor_commits_used = 0
        for term in terms:
            if root.git_head:
                git = shutil.which("git")
                if git is None:
                    continue
                command = [
                    git,
                    "-C",
                    str(view),
                    "grep",
                    "-n",
                    "-I",
                    "-i",
                    "-F",
                    term,
                    root.git_head,
                    "--",
                ]
            else:
                rg = find_rg() or shutil.which("rg")
                if rg is None:
                    continue
                command = [rg, "-n", "-i", "-F", "--glob", "!.jiuwenswarm_snapshot_manifest.json", term, str(view)]
            try:
                result = subprocess.run(  # noqa: S603 - executable and arguments are code-owned
                    command,
                    stdin=subprocess.DEVNULL,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    capture_output=True,
                    check=False,
                    timeout=30,
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            hits.extend(f"root-{root_index}:{line}" for line in result.stdout.splitlines()[:_HITS_PER_TERM])
            if not root.git_head or git is None or ancestor_commits_used >= _ANCESTOR_COMMITS_PER_ROOT:
                continue
            try:
                history = subprocess.run(  # noqa: S603 - executable and arguments are code-owned
                    [
                        git,
                        "-C",
                        str(view),
                        "log",
                        "--format=%H",
                        f"-{_ANCESTOR_COMMITS_PER_TERM}",
                        "-S",
                        term,
                        root.git_head,
                        "--",
                    ],
                    stdin=subprocess.DEVNULL,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    capture_output=True,
                    check=False,
                    timeout=10,
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            for commit in history.stdout.splitlines()[:_ANCESTOR_COMMITS_PER_TERM]:
                if ancestor_commits_used >= _ANCESTOR_COMMITS_PER_ROOT:
                    break
                if not re.fullmatch(r"[0-9a-fA-F]{40,64}", commit):
                    continue
                ancestor_commits_used += 1
                try:
                    ancestor = subprocess.run(  # noqa: S603 - executable and arguments are code-owned
                        [git, "-C", str(view), "grep", "-n", "-I", "-i", "-F", term, commit, "--"],
                        stdin=subprocess.DEVNULL,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        capture_output=True,
                        check=False,
                        timeout=10,
                    )
                except (OSError, subprocess.TimeoutExpired):
                    continue
                hits.extend(
                    f"root-{root_index}:ancestor-{commit[:12]}:{line}"
                    for line in ancestor.stdout.splitlines()[:_HITS_PER_TERM]
                )
    unique_hits = list(dict.fromkeys(hits))[:_HITS_MAX]
    packet = (
        "Frozen roots:\n"
        + "\n".join(root_lines)
        + "\nSearch terms: "
        + ", ".join(terms)
        + "\nFrozen-repository matches (analogy is not proof):\n"
        + ("\n".join(unique_hits) if unique_hits else "[no matches]")
    )
    return packet[:_PACKET_MAX_CHARS]
