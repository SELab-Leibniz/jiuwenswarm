# Copyright (c) 2026 Chrys. All rights reserved.

"""Portable trusted-host memory command; corpus identity is part of trial admission."""

from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
from pathlib import Path

from .capacity import capacity_scale, model_content_limit


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
        stdout_spool = tempfile.SpooledTemporaryFile(max_size=1024 * 1024)
        stderr_spool = tempfile.SpooledTemporaryFile(max_size=1024 * 1024)

        async def drain(stream, spool) -> None:
            assert stream is not None
            while True:
                chunk = await stream.read(1024 * 1024)
                if not chunk:
                    return
                spool.write(chunk)

        try:
            request = json.dumps(
                {
                    "schema": "long-horizon/memory-query/v1",
                    "requirement": task,
                    "snapshot_root": context.snapshot.roots[0].view_root,
                    "receipt_sha256": self.receipt_sha256,
                }
            ).encode()
            assert process.stdin is not None
            process.stdin.write(request)
            await process.stdin.drain()
            process.stdin.close()
            await asyncio.gather(
                drain(process.stdout, stdout_spool),
                drain(process.stderr, stderr_spool),
                process.wait(),
            )
            if process.returncode != 0:
                raise ValueError("memory provider failed")
            stdout_spool.seek(0)
            raw = stdout_spool.read()
            if capacity_scale() == 1 and len(raw) > 64_000:
                raise ValueError("memory provider exceeded its output limit")
            value = json.loads(raw)
            if not isinstance(value, dict) or not isinstance(value.get("text"), str):
                raise ValueError("memory provider must return a JSON object with text")
            # The provider output is spooled, never rejected for size.  The
            # prompt bound remains the opt-in content capacity.
            return value["text"][:model_content_limit(12_000)]
        finally:
            stdout_spool.close()
            stderr_spool.close()
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except TimeoutError:
                    process.kill()
                    await process.wait()
