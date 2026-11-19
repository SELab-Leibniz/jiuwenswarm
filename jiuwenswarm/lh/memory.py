# Copyright (c) 2026 Chrys. All rights reserved.

"""Portable trusted-host memory command; corpus identity is part of trial admission."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path


class CommandMemory:
    """Read-only host command consumes a JSON request and returns {text: str}."""

    def __init__(self, argv: list[str], receipt: Path) -> None:
        if not argv:
            raise ValueError("memory command must be nonempty")
        self.argv, self.receipt = argv, receipt.resolve(strict=True)
        self.receipt_sha256 = hashlib.sha256(self.receipt.read_bytes()).hexdigest()
        self.identity = hashlib.sha256(json.dumps([argv, self.receipt_sha256]).encode()).hexdigest()

    async def __call__(self, context, task: str) -> str:
        if hashlib.sha256(self.receipt.read_bytes()).hexdigest() != self.receipt_sha256:
            raise ValueError("frozen memory corpus receipt changed")
        process = await asyncio.create_subprocess_exec(
            *self.argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _stderr = await process.communicate(
                json.dumps(
                    {
                        "schema": "long-horizon/memory-query/v1",
                        "requirement": task,
                        "snapshot_root": context.snapshot.roots[0].view_root,
                        "receipt_sha256": self.receipt_sha256,
                    }
                ).encode()
            )
            if process.returncode != 0 or len(stdout) > 64_000:
                raise ValueError("memory provider failed or exceeded its output limit")
            value = json.loads(stdout)
            if not isinstance(value, dict) or not isinstance(value.get("text"), str):
                raise ValueError("memory provider must return a JSON object with text")
            return value["text"][:12_000]
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except TimeoutError:
                    process.kill()
                    await process.wait()
