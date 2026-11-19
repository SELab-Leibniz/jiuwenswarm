# Copyright (c) 2026 Chrys. All rights reserved.

"""Read explicitly configured public runtime capabilities without executing them.

Migrated from Chrys 0561cf0fa584fd56fdcd98c90cca6a5bc1890d95,
``src/chrys/service/requirement_clarification/runtime_capabilities.py``.
These launcher-declared facts describe existing public commands outside the
source snapshot. They do not add acceptance criteria or reveal a grading oracle.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class PublicRuntimeCommand(BaseModel):
    """A launcher-declared command installed outside the source snapshot."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    executable: str = Field(min_length=1, max_length=4096)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    invocation: str = Field(min_length=1, max_length=4096)
    description: str = Field(min_length=1, max_length=8000)


class PublicRuntimeCapabilities(BaseModel):
    """Closed public interface; arbitrary environment variables are never read."""

    model_config = ConfigDict(extra="forbid")

    schema_id: Literal["jiuwenswarm/public-runtime-capabilities/v1"] = Field(alias="schema")
    commands: list[PublicRuntimeCommand] = Field(max_length=20)
    execution_identity: dict[str, str] = Field(default_factory=dict, max_length=10)


def read_public_runtime_capabilities(path: str) -> str:
    """Verify launcher receipts and return public facts, never a grading oracle."""
    if not path:
        return ""
    raw = Path(path).read_bytes()
    if len(raw) > 128 * 1024:
        raise ValueError("public runtime capability manifest is too large")
    capabilities = PublicRuntimeCapabilities.model_validate_json(raw)
    for command in capabilities.commands:
        executable = Path(command.executable)
        if not executable.is_absolute() or not executable.is_file():
            raise ValueError(f"public runtime executable is unavailable: {command.name}")
        with executable.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != command.sha256:
            raise ValueError(f"public runtime executable changed: {command.name}")
    return json.dumps(capabilities.model_dump(mode="json", by_alias=True), ensure_ascii=True)
