"""Common output and receipt validation, independent of native artifact formats."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from jiuwenswarm.pact_core.artifacts import write_json_atomic
from jiuwenswarm.pact_core.runtime import RuntimeContractError
from jiuwenswarm.pact_core.runtime.export import ExportReceipt


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def validate_output_root(output: Path, *, protected: tuple[Path, ...]) -> Path:
    path = output.expanduser().absolute()
    if path.exists() and (path.is_symlink() or not path.is_dir()):
        raise RuntimeContractError("output root must be a real directory")
    if any(part.casefold() in {".git", ".pact", ".pact-io"} for part in path.parts):
        raise RuntimeContractError("output root cannot use a control namespace")
    resolved = path.resolve()
    for root in protected:
        root = root.resolve()
        if resolved.is_relative_to(root) or root.is_relative_to(resolved):
            raise RuntimeContractError(f"output root overlaps protected source or evidence: {root}")
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def contained(root: Path, path: Path) -> str:
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise RuntimeContractError(f"evidence path escapes the output root: {path}")
    return resolved.relative_to(root.resolve()).as_posix()


def contained_in_roots(
    path: Path, *, output_root: Path, archive_roots: tuple[Path, ...] = ()
) -> dict[str, str]:
    """Locate evidence under the output or one explicitly trusted archive root.

    Archive candidates are exported outside ``output`` by design.  Only the
    output and the archive roots supplied by the controller are trusted; a
    symlink or ``..`` that resolves anywhere else still fails.
    """
    resolved = path.resolve()
    output = output_root.resolve()
    if resolved.is_relative_to(output):
        return {"root": "output", "path": resolved.relative_to(output).as_posix()}
    for archive_root in archive_roots:
        root = archive_root.resolve()
        if resolved.is_relative_to(root):
            return {"root": "archive", "path": resolved.relative_to(root).as_posix()}
    raise RuntimeContractError(f"evidence path escapes the output root and trusted archive roots: {path}")


def receipt_payload(receipt: ExportReceipt) -> dict[str, object]:
    return json.loads(json.dumps(asdict(receipt), ensure_ascii=True, default=str))


def publish_selection(
    output_root: Path,
    payload: dict[str, object],
    *,
    schema: str,
    archive_roots: tuple[Path, ...] = (),
) -> Path:
    if payload.get("schema") != schema:
        raise RuntimeContractError("selection uses a different result schema")
    export = payload.get("export")
    if export is not None:
        if not isinstance(export, dict) or not isinstance(export.get("artifact"), str):
            raise RuntimeContractError("selection export must be an encoded ExportReceipt")
        contained_in_roots(Path(export["artifact"]), output_root=output_root, archive_roots=archive_roots)
    target = output_root / "selection.json"
    write_json_atomic(target, json.loads(json.dumps(payload, ensure_ascii=True, default=str)))
    return target
