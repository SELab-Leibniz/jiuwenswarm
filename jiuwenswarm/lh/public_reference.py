# Copyright (c) 2026 Chrys. All rights reserved.

"""Probe a declared public tool or compare two executions in the caller's task sandbox."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import selectors
import signal
import subprocess
import sys
import tempfile
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

MAX_BYTES = 1024 * 1024
SCHEMA = "public-reference-check/v1"


class CheckError(ValueError):
    def __init__(self, reason, *, exit_code=None):
        super().__init__(reason)
        self.exit_code = exit_code


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CheckError("duplicate_key")
        result[key] = value
    return result


def _nonfinite(_):
    raise CheckError("nonfinite_number")


def _decode(raw):
    try:
        return json.loads(
            raw.decode("utf-8"), object_pairs_hook=_object, parse_float=Decimal, parse_constant=_nonfinite
        )
    except (UnicodeError, ValueError, RecursionError, InvalidOperation) as exc:
        raise CheckError("invalid_json") from exc


def _request(path, minimum):
    with path.open("rb") as source:
        raw = source.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise CheckError("request_too_large")
    request = _decode(raw)
    if not isinstance(request, dict) or set(request) != {"input", "checkpoints"}:
        raise CheckError("invalid_request")
    steps = request["checkpoints"]
    if (
        not isinstance(steps, list)
        or len(steps) < minimum
        or any(type(step) is not int or step < 0 for step in steps)
        or any(left >= right for left, right in zip(steps, steps[1:]))
    ):
        raise CheckError("invalid_checkpoints")
    return raw, steps


def _guard_adapter(owner_fd, status_fd, argv):
    """Own a private process group until either the adapter exits or its caller disappears."""
    if os.getsid(0) != os.getpid():
        return 2
    try:
        try:
            child = subprocess.Popen(argv, close_fds=True)
        except (OSError, ValueError):
            os.write(status_fd, b"unavailable")
            return 2
        with selectors.DefaultSelector() as poll:
            poll.register(owner_fd, selectors.EVENT_READ)
            while child.poll() is None:
                if poll.select(0.05):
                    return 2
        os.write(status_fd, str(child.returncode).encode("ascii"))
    finally:
        # This process is the session leader. Its PID cannot be reused here.
        # Closing the owner's pipe on TERM, KILL or timeout reaches this path.
        os.killpg(os.getpgrp(), signal.SIGKILL)


def _run_adapter(argv, request, deadline):
    """Keep stdout bounded. A separate group owner watches a pipe even if the helper is killed."""
    with tempfile.TemporaryFile() as source, tempfile.TemporaryFile() as status:
        source.write(request)
        source.seek(0)
        owner_read, owner_write = os.pipe()
        try:
            guard = subprocess.Popen(
                [sys.executable, "-I", __file__, "--guard-adapter", str(owner_read), str(status.fileno()), *argv],
                stdin=source,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                pass_fds=(owner_read, status.fileno()),
            )
        except BaseException:
            os.close(owner_write)
            raise
        finally:
            os.close(owner_read)
        with guard:
            try:
                output = bytearray()
                with selectors.DefaultSelector() as poll:
                    poll.register(guard.stdout, selectors.EVENT_READ)
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0 or not poll.select(remaining):
                            raise CheckError("timeout")
                        block = os.read(guard.stdout.fileno(), 65536)
                        if not block:
                            break
                        output.extend(block)
                        if len(output) > MAX_BYTES:
                            raise CheckError("output_too_large")
                status.seek(0)
                code = status.read(16)
                if code == b"unavailable" or not code.lstrip(b"-").isdigit():
                    raise CheckError("execution_error")
                if int(code) != 0:
                    raise CheckError("nonzero_exit", exit_code=int(code))
                return _decode(output)
            finally:
                os.close(owner_write)


def _observations(argv, raw, steps, deadline):
    if deadline <= time.monotonic():
        raise CheckError("timeout")
    rows = _run_adapter(argv, raw, deadline)
    if (
        isinstance(rows, dict)
        and set(rows) == {"error"}
        and rows["error"] in ("access_denied", "timeout", "service_unavailable")
    ):
        raise CheckError(rows["error"])
    if not isinstance(rows, list) or len(rows) != len(steps):
        raise CheckError("incomplete_trace")
    for row, step in zip(rows, steps):
        if (
            not isinstance(row, dict)
            or set(row) != {"step", "state"}
            or type(row["step"]) is not int
            or row["step"] != step
        ):
            raise CheckError("invalid_observation")
    return rows


def _difference(left, right, path=""):
    """First unequal public field as a JSON Pointer, including missing fields and array entries."""
    if type(left) is not type(right):
        if type(left) in (int, Decimal) and type(right) in (int, Decimal) and left == right:
            return None
        return path
    if isinstance(left, dict):
        for key in sorted(left.keys() | right.keys()):
            child = path + "/" + key.replace("~", "~0").replace("/", "~1")
            if key not in left or key not in right:
                return child
            mismatch = _difference(left[key], right[key], child)
            if mismatch is not None:
                return mismatch
    elif isinstance(left, list):
        for index, (a, b) in enumerate(zip(left, right)):
            mismatch = _difference(a, b, path + "/" + str(index))
            if mismatch is not None:
                return mismatch
        if len(left) != len(right):
            return path + "/" + str(min(len(left), len(right)))
    elif left != right:
        return path
    return None


def check(request_path, reference, candidate=None, *, timeout=15):
    receipt = {"schema": SCHEMA}
    try:
        raw, steps = _request(request_path, 1 if candidate is None else 2)
    except (CheckError, OSError) as exc:
        return {
            **receipt,
            "status": "invalid_request",
            "reason": str(exc) if isinstance(exc, CheckError) else "read_error",
        }
    receipt["request_sha256"] = hashlib.sha256(raw).hexdigest()

    def argv_hash(argv):
        return hashlib.sha256(json.dumps(argv, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()

    receipt["reference_argv_sha256"] = argv_hash(reference)
    if candidate is not None:
        receipt["candidate_argv_sha256"] = argv_hash(candidate)
        if receipt["reference_argv_sha256"] == receipt["candidate_argv_sha256"]:
            return {**receipt, "status": "invalid_request", "reason": "identical_adapters"}
    traces = []
    for side, argv in (("reference", reference), ("candidate", candidate)):
        if argv is None:
            break
        try:
            traces.append(_observations(argv, raw, steps, time.monotonic() + timeout))
        except (CheckError, OSError) as exc:
            return {
                **receipt,
                "status": side + "_unavailable",
                **({"exit_code": exc.exit_code} if isinstance(exc, CheckError) and exc.exit_code is not None else {}),
                "reason": str(exc) if isinstance(exc, CheckError) else "execution_error",
            }
    if candidate is None:
        return {**receipt, "status": "available", "checkpoints": len(steps)}
    try:
        for expected, actual in zip(*traces):
            field = _difference(expected["state"], actual["state"])
            if field is not None:
                return {**receipt, "status": "mismatch", "step": expected["step"], "field": field}
    except RecursionError:
        return {**receipt, "status": "comparison_unavailable", "reason": "nesting_limit"}
    return {**receipt, "status": "match", "checkpoints": len(steps)}


def _argv(value):
    try:
        items = json.loads(value)
    except ValueError:
        items = None
    if (
        not isinstance(items, list)
        or not items
        or not all(isinstance(s, str) and "\0" not in s for s in items)
        or not items[0]
    ):
        raise argparse.ArgumentTypeError("expected a nonempty JSON command array")
    return items


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for action in ("probe", "compare"):
        command = commands.add_parser(action)
        command.add_argument("request", type=Path)
        command.add_argument("--reference", required=True, type=_argv)
        if action == "compare":
            command.add_argument("--candidate", required=True, type=_argv)
        command.add_argument("--timeout", type=int, choices=range(1, 301), metavar="1..300", default=15)
    args = parser.parse_args(argv)
    try:
        receipt = check(args.request, args.reference, getattr(args, "candidate", None), timeout=args.timeout)
    except Exception:
        receipt = {"schema": SCHEMA, "status": "comparison_unavailable", "reason": "helper_error"}
    print(json.dumps(receipt, sort_keys=True))
    if receipt["status"] == "mismatch":
        return 1
    return 0 if receipt["status"] in ("available", "match") else 2


if __name__ == "__main__":
    if sys.argv[1:2] == ["--guard-adapter"]:
        sys.exit(_guard_adapter(int(sys.argv[2]), int(sys.argv[3]), sys.argv[4:]))
    sys.exit(main())
