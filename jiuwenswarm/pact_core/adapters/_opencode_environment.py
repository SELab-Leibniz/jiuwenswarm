"""OpenCode profile installation and host-environment isolation."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


_WORKER_AGENT_MD = """---
description: PACT worker: headless implementation agent for one checkpoint round.
mode: primary
permission:
  edit: allow
  bash: allow
  task:
    "*": deny
---

Operate autonomously to completion within this one round. The prompt you
receive is the complete, self-contained task; do not wait for interactive
approval or additional instructions.
"""

_REVIEWER_AGENT_MD = """---
description: PACT reviewer: headless review agent for one checkpoint round.
mode: primary
permission:
  edit: allow
  bash: allow
  task:
    "*": deny
---

Operate autonomously to completion within this one round. The prompt you
receive is the complete, self-contained task; do not wait for interactive
approval or additional instructions.
"""


class OpenCodeEnvironment:
    """Own one adapter instance's disposable OpenCode home and profiles."""

    def __init__(self, home_root: Path | None) -> None:
        self._home_root = home_root
        self._temporary_home: tempfile.TemporaryDirectory[str] | None = None

    def ensure_home(self) -> Path:
        if self._home_root is None:
            self._temporary_home = tempfile.TemporaryDirectory(
                prefix="pact-opencode-home-"
            )
            self._home_root = Path(self._temporary_home.name)
        for sub in ("config", "data", "state", "cache", "tmp"):
            (self._home_root / sub).mkdir(parents=True, exist_ok=True)
        return self._home_root

    @staticmethod
    def isolation_env(
        home: Path, *, data_home: Path | None = None
    ) -> dict[str, str]:
        effective_data_home = data_home or home / "data"
        return {
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / "config"),
            "XDG_DATA_HOME": str(effective_data_home),
            "XDG_STATE_HOME": str(home / "state"),
            "XDG_CACHE_HOME": str(home / "cache"),
            "TMPDIR": str(home / "tmp"),
            "OPENCODE_DB": str(effective_data_home / "opencode" / "pact.db"),
            "OPENCODE_CONFIG_DIR": str(home / "config" / "opencode"),
            "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
        }

    @staticmethod
    def host_auth_content() -> str | None:
        """Read host login for one-way forwarding to the isolated child."""
        if os.environ.get("OPENCODE_AUTH_CONTENT"):
            return None
        data_home = os.environ.get("XDG_DATA_HOME")
        base = (
            Path(data_home).expanduser()
            if data_home
            else Path.home() / ".local" / "share"
        )
        try:
            content = (base / "opencode" / "auth.json").read_text(encoding="utf-8")
        except OSError:
            return None
        return content if content.strip() else None

    def build_env(
        self, home: Path, *, data_home: Path | None = None
    ) -> dict[str, str]:
        env = os.environ.copy()
        auth_content = self.host_auth_content()
        env.pop("OPENCODE_CONFIG", None)
        env.pop("OPENCODE_CONFIG_CONTENT", None)
        env.update(self.isolation_env(home, data_home=data_home))
        if auth_content is not None:
            env["OPENCODE_AUTH_CONTENT"] = auth_content
        return env

    def preflight_env(self, home: Path) -> dict[str, str]:
        """Build an isolated, credential-free environment for debug probes."""
        env = os.environ.copy()
        for key in (
            "OPENCODE_AUTH_CONTENT",
            "OPENCODE_CONFIG",
            "OPENCODE_CONFIG_CONTENT",
        ):
            env.pop(key, None)
        env.update(self.isolation_env(home))
        return env

    @staticmethod
    def install_agent_profiles(home: Path) -> None:
        agent_dir = home / "config" / "opencode" / "agent"
        agent_dir.mkdir(parents=True, exist_ok=True)
        (agent_dir / "pact-worker.md").write_text(
            _WORKER_AGENT_MD, encoding="utf-8"
        )
        (agent_dir / "pact-reviewer.md").write_text(
            _REVIEWER_AGENT_MD, encoding="utf-8"
        )
