"""Stage-local logging bootstrap, followed by the unchanged native machine CLI.

The pinned Process CLI does not initialize agent-core's absolute log directory.
Without this bootstrap its lazy logger writes ``./logs`` after native workspace
activation, which contaminates read-only S0 snapshots. No agent behavior or
machine protocol is reimplemented here.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys


def main() -> None:
    state_value = os.environ.get("JIUWENSWARM_DATA_DIR", "")
    state = Path(state_value)
    if not state_value or not state.is_absolute() or not state.is_dir():
        print("Native stage requires an existing absolute state directory.", file=sys.stderr)
        raise SystemExit(1)

    # Imports can initialize a default logger before configure_log_config runs.
    # Keep even those startup files in the owned state directory.
    os.chdir(state)
    from jiuwenswarm.channels.process_cli.machine_entry import protocol_stdout

    try:
        with protocol_stdout():
            from openjiuwen.core.common.logging.log_config import configure_log_config

            log_dir = state / "openjiuwen-logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            configure_log_config({
                "backend": "default", "level": "INFO", "log_path": str(log_dir) + os.sep,
                "output": ["console", "file"],
            })
            from jiuwenswarm.channels.process_cli.main import main as native_main
    except Exception:
        # Do not print third-party exceptions that can include provider config.
        print("Native stage logging bootstrap failed.", file=sys.stderr)
        raise SystemExit(1) from None
    native_main()


if __name__ == "__main__":
    main()
