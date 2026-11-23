"""Fake native CLI used by the deterministic stage-write tests.

It deliberately changes cwd to the request workspace before opening a rotating
log and a WAL SQLite database, like openjiuwen's lazy logging and memory index.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path


def main() -> None:
    request = json.load(sys.stdin)
    initial_cwd = Path(os.getcwd())
    workspace = Path(request["workspace"]["project_dir"])
    receipt = Path(os.environ["STAGE_WRITE_RECEIPT"])
    view_source = (workspace / "SOURCE.txt").read_text(encoding="utf-8")

    # A relative write before native workspace activation lands in the stage
    # state by construction, not in the frozen view.
    (initial_cwd / "logs").mkdir(parents=True, exist_ok=True)
    (initial_cwd / "logs/pre-activation.log").write_text("state only\n", encoding="utf-8")

    # Native workspace activation changes the process cwd to the view. The
    # companion redirect is then the second layer for these writes.
    os.chdir(workspace)

    handler = RotatingFileHandler("logs/logs/run/child.log", maxBytes=512,
                                  backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    sink = logging.getLogger("stage-writes-fake-native")
    sink.handlers[:] = []
    sink.propagate = False
    sink.setLevel(logging.INFO)
    sink.addHandler(handler)
    for index in range(40):
        sink.info("rotation " + "x" * 40 + f" {index}")
    handler.close()

    db = sqlite3.connect(str(workspace / "memory.db"))
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE IF NOT EXISTS memory (value TEXT)")
    db.execute("INSERT INTO memory VALUES (?)", ("remembered",))
    db.commit()
    actual = Path(db.execute("PRAGMA database_list").fetchone()[2])
    wal = Path(str(actual) + "-wal")
    shm = Path(str(actual) + "-shm")
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(json.dumps({
        "uid": os.geteuid(), "pid": os.getpid(), "view_read": view_source,
        "initial_cwd": str(initial_cwd), "cwd": os.getcwd(), "db_path": str(actual),
        "wal_path": str(wal), "wal_exists": wal.exists(),
        "shm_path": str(shm), "shm_exists": shm.exists(),
        "journal_mode": db.execute("PRAGMA journal_mode").fetchone()[0],
    }), encoding="utf-8")
    db.close()

    forced = os.environ.get("STAGE_WRITE_EXIT_CODE")
    timestamp = os.environ.get("STAGE_WRITE_TERMINAL_TIMESTAMP")
    if forced is not None:
        code = int(forced)
        status = "failed"
    elif request["input"].endswith("cancel after writes"):
        status, code = "cancelled", 130
    else:
        status, code = "completed", 0
    session = request.get("session_id") or "stage-write-session"
    sequence = 0
    # A genuine tool-free native turn emits this proof before its terminal.
    # This fixture only supplies it when a test explicitly asks for the
    # tool_policy=none protocol; ordinary stage-write cases are unchanged.
    if os.environ.get("STAGE_WRITE_TOOL_POLICY_PROOF") == "none":
        print(json.dumps({
            "schema_version": "0.1", "type": "event", "sequence": sequence,
            "request_id": request["request_id"], "session_id": session,
            "event_type": "chat.tool_policy",
            "payload": {
                "policy": "none", "registered_tools": [], "verified": True,
                "profile": "code.no-tools.v1",
            },
        }), flush=True)
        sequence += 1
    terminal = {
        "schema_version": "0.1", "type": "result", "sequence": sequence,
        "request_id": request["request_id"],
        "session_id": session,
        "status": status, "exit_code": code,
        "output": "cancelled" if status == "cancelled" else "failed" if code else "completed",
        "usage": {},
    }
    if timestamp is not None:
        terminal["finished_at_unix"] = float(timestamp)
    print(json.dumps(terminal), flush=True)

    # An explicit barrier lets a test hold the child alive after its terminal
    # event without a sleep-based cancellation trigger.
    barrier = os.environ.get("STAGE_WRITE_EXIT_BARRIER")
    if barrier:
        Path(str(barrier) + ".ready").write_text("ready\n", encoding="utf-8")
        until = time.monotonic() + 10
        while not Path(barrier).exists() and time.monotonic() < until:
            time.sleep(0.01)
    if code < 0:
        os.kill(os.getpid(), -code)
        raise SystemExit(128 - code)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
