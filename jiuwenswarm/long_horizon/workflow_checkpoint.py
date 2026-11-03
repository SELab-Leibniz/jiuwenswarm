"""Offline verification/export tools; these commands never call a model."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("verify", "restore", "export-p0", "verify-p0", "restore-p0"))
    parser.add_argument("source", type=Path)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--requirement-file", type=Path)
    parser.add_argument("--sha256")
    args = parser.parse_args(argv)
    if args.command == "export-p0":
        from .recovery import export_p0_recovery
        if args.destination is None:
            parser.error("--destination is required")
        manifest = export_p0_recovery(args.source, args.destination)
        print(json.dumps({"manifest": str(manifest), "sha256": manifest.with_suffix(".sha256").read_text().strip()}))
    elif args.command in {"verify-p0", "restore-p0"}:
        from .recovery import load_p0_recovery, restore_p0_recovery
        if args.requirement_file is None or args.sha256 is None:
            parser.error("--requirement-file and --sha256 are required")
        if args.command == "restore-p0":
            if args.destination is None:
                parser.error("--destination is required")
            _payload, receipt = restore_p0_recovery(args.source, workspace=args.destination,
                requirement_bytes=args.requirement_file.read_bytes(), expected_sha256=args.sha256)
            print(json.dumps(receipt))
        else:
            payload, _s0, p0 = load_p0_recovery(args.source, requirement_bytes=args.requirement_file.read_bytes(),
                                             expected_sha256=args.sha256, require_publication=True)
            print(json.dumps({"schema": payload["schema"], "verified_source_entries": p0.entry_count, "resume_stage": payload["resume_stage"]}))
    elif args.command == "verify":
        from .checkpoints import verify_stage_checkpoint
        payload, snapshot = verify_stage_checkpoint(args.source)
        print(json.dumps({"stage": payload["stage"], "status": payload["status"], "verified_source_entries": snapshot.entry_count}))
    else:
        from .checkpoints import restore_stage_to_empty_directory
        if args.destination is None:
            parser.error("--destination is required")
        payload = restore_stage_to_empty_directory(args.source, args.destination)
        print(json.dumps({"restored_stage": payload["stage"], "destination": str(args.destination)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
