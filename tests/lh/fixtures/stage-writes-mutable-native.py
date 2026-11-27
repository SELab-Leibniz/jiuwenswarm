#!/usr/bin/env python3
"""Writable solver fixture: writes stay in the solver workspace."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


def main() -> None:
    request = json.load(sys.stdin)
    workspace = Path(request["workspace"]["cwd"])
    os.chdir(workspace)

    Path("logs").mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler("logs/mutable.log", maxBytes=128,
                                  backupCount=1, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    sink = logging.getLogger("stage-writes-mutable-native")
    sink.handlers[:] = []
    sink.propagate = False
    sink.setLevel(logging.INFO)
    sink.addHandler(handler)
    sink.info("solver write " + "y" * 200)
    handler.close()

    db = sqlite3.connect("memory.db")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE IF NOT EXISTS memory (value TEXT)")
    db.execute("INSERT INTO memory VALUES ('solver')")
    db.commit()
    db.close()

    print(json.dumps({
        "schema_version": "0.1", "type": "result", "sequence": 0,
        "request_id": request["request_id"],
        "session_id": request.get("session_id") or "mutable-session",
        "status": "completed", "exit_code": 0, "output": "done", "usage": {},
    }), flush=True)


if __name__ == "__main__":
    main()
