# Copyright (c) 2026 Chrys. All rights reserved.

"""Read the complete deterministic check output that a prompt may cite.

The pinned PACT wheel records full ``check-NNN-stdout.log`` and
``check-NNN-stderr.log`` files next to ``check-results.json`` but exposes only
a short tail in ``VerificationEvidence``.  Enlarging the tail in a controller
prompt therefore cannot recover the removed bytes.  This module reassembles
the logged payloads and emits a permission-safe receipt for any stream that
exceeds the prompt budget, so the model receives actual content plus a stable
reference instead of a silent cut.

The runner uses fixed ``check-NNN-*`` names and rewrites them for every
verification.  Reading every matching file in the directory would therefore
attach an older or newer run's bytes to the current artifact.  This module
accepts only the checks listed by the verification's own
``check-results.json``, requires each log to end with that result's recorded
``stdout_tail``/``stderr_tail``, and (when the verification receipt carries
overall tails) requires those tails to be the exact suffix of the reassembled
check-runner streams.  Any mismatch yields an empty result so the caller falls
back to the wheel's own tail rather than presenting foreign evidence.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

_CHECK_RESULTS = "check-results.json"
_CHECK_RESULTS_SCHEMA = "pact-runtime/check-results/v1"
_MAX_RECEIPT_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class CheckOutput:
    """Complete or referenced deterministic check output for one artifact."""

    stdout: str
    stderr: str
    references: tuple[dict[str, object], ...]
    complete: bool
    total_bytes: int

    def prompt_block(self) -> dict[str, object]:
        return {
            "stdout": self.stdout,
            "stderr": self.stderr,
            "complete": self.complete,
            "total_bytes": self.total_bytes,
            "references": list(self.references),
        }


def _empty() -> CheckOutput:
    return CheckOutput("", "", (), True, 0)


def _json_file(path: Path) -> object | None:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_RECEIPT_BYTES:
            return None
        return json.loads(path.read_bytes())
    except (OSError, ValueError, TypeError):
        return None


def _stream_parts(results: list[dict[str, object]]) -> tuple[str, str]:
    """Reproduce exactly what ``check_runner`` writes to its two streams."""
    stdout: list[str] = []
    stderr: list[str] = []
    for result in results:
        stdout.append(f"[{result.get('name')}] {result.get('status')}\n")
        stdout.append(str(result.get("stdout_tail", "")))
        stderr.append(str(result.get("stderr_tail", "")))
    return "".join(stdout), "".join(stderr)


def _bound_rows(
    artifact: Path,
) -> list[tuple[int, str, Path, bytes, str]] | None:
    """Return only logs the verification receipt itself lists and binds.

    ``None`` means the available directories and files cannot be trusted as
    this verification's evidence.  An empty list means this verification ran
    no derived checks at all.
    """
    root = artifact.parent
    payload = _json_file(artifact)
    if not isinstance(payload, dict):
        return None
    results_payload = _json_file(root / _CHECK_RESULTS)
    if not isinstance(results_payload, dict):
        return None
    if results_payload.get("schema") != _CHECK_RESULTS_SCHEMA:
        return None
    results = results_payload.get("results")
    if not isinstance(results, list) or not results:
        return None
    if any(not isinstance(row, dict) for row in results):
        return None

    expected_stdout, expected_stderr = _stream_parts(results)  # type: ignore[arg-type]
    for key, expected in (("stdout_tail", expected_stdout), ("stderr_tail", expected_stderr)):
        recorded = payload.get(key)
        if isinstance(recorded, str) and recorded:
            if not expected.endswith(recorded):
                return None

    rows: list[tuple[int, str, Path, bytes, str]] = []
    for index, result in enumerate(results, start=1):
        assert isinstance(result, dict)
        for stream in ("stdout", "stderr"):
            tail = result.get(f"{stream}_tail")
            if not isinstance(tail, str):
                return None
            path = root / f"check-{index:03d}-{stream}.log"
            try:
                if path.is_symlink() or not path.is_file():
                    return None
                raw = path.read_bytes()
                text = raw.decode("utf-8")
            except (OSError, UnicodeError):
                return None
            if tail:
                if not text.endswith(tail):
                    return None
            elif text:
                # An empty tail with non-empty bytes belongs to a different
                # execution, not this verification.
                return None
            rows.append((index, stream, path, raw, text))
    return rows


def _display_text(raw: bytes, path: Path, *, maximum: int) -> tuple[str, bool]:
    """Show at most ``maximum`` bytes, with an explicit receipt for the rest."""
    if maximum <= 0:
        return "", not raw
    if len(raw) <= maximum:
        return raw.decode("utf-8", errors="replace"), True
    text = raw[:maximum].decode("utf-8", errors="ignore")
    text += (
        f"\n...[complete check log retained at {path}; "
        f"sha256={hashlib.sha256(raw).hexdigest()}; bytes={len(raw)}]"
    )
    return text, False


def read_check_outputs(artifact: Path | str | None, *, maximum: int | None) -> CheckOutput:
    """Return check logs bound to one verification artifact.

    ``artifact`` is the sealed verification receipt; its exact
    ``check-results.json`` names the checks and their per-check tails.  A
    missing, unreadable or stale receipt/log set yields an empty result rather
    than raising, matching the advisory nature of these diagnostic bytes.
    ``maximum`` is a byte budget for the combined displayed streams; ``None``
    keeps the complete logs, which is the study opt-in path.
    """
    if artifact is None:
        return _empty()
    try:
        path = Path(artifact)
    except (TypeError, ValueError):
        return _empty()
    rows = _bound_rows(path)
    if not rows:
        return _empty()

    references: list[dict[str, object]] = []
    total = 0
    for index, stream, log_path, raw, _text in rows:
        total += len(raw)
        references.append(
            {
                "check": index,
                "stream": stream,
                "path": str(log_path),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "bytes": len(raw),
            }
        )

    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    complete = True
    remaining = None if maximum is None else max(0, maximum)
    for index, stream, log_path, raw, text in rows:
        if remaining is None:
            shown, this_complete = text, True
        else:
            if remaining <= 0:
                # A zero budget must not silently claim that the remaining
                # logs were represented.
                complete = False
                continue
            shown, this_complete = _display_text(raw, log_path, maximum=remaining)
            remaining -= len(shown.encode("utf-8"))
        complete = complete and this_complete
        header = f"[check-{index:03d}-{stream}]\n"
        (stdout_parts if stream == "stdout" else stderr_parts).append(header + shown)
    return CheckOutput(
        stdout="\n".join(stdout_parts),
        stderr="\n".join(stderr_parts),
        references=tuple(references),
        complete=complete,
        total_bytes=total,
    )


__all__ = ["CheckOutput", "read_check_outputs"]
