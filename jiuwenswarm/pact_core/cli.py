"""Unified work-centric CLI; `pact run` defaults to continuous execution."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from . import __version__
from .adapters import UnknownAdapterError
from .artifacts import EventLogCorruptionError
from .checkpoint import PactGitError
from .controller import PactLoopError
from .kernel import KernelContractError
from .runtime import RuntimeContractError, RuntimeStateError
from ._campaign_cli import add_campaign_commands, add_run_arguments, run_campaign_command


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pact",
        description=(
            "Continue durable, plan-anchored work (PACT) "
            "against any coding agent CLI."
        ),
    )
    parser.add_argument("--version", action="version", version=f"pact-core {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    run = subparsers.add_parser("run", help="run or continue work; continuous execution is the default")
    add_run_arguments(run)

    add_campaign_commands(subparsers)
    return parser


def _run_command(args: argparse.Namespace) -> int:
    return run_campaign_command(args)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "run":
        try:
            return _run_command(args)
        except (
            OSError,
            ValueError,
            EventLogCorruptionError,
            KernelContractError,
            PactGitError,
            PactLoopError,
            RuntimeContractError,
            RuntimeStateError,
            UnknownAdapterError,
        ) as exc:
            print(f"pact: error: {exc}", file=sys.stderr)
            return 2
    if args.command == "campaign":
        if args.campaign_command is None:
            parser.parse_args(["campaign", "--help"])
            raise AssertionError("campaign help should exit")
        if args.campaign_command is not None:
            try:
                return run_campaign_command(args)
            except (
                OSError,
                ValueError,
                EventLogCorruptionError,
                KernelContractError,
                PactGitError,
                PactLoopError,
                RuntimeContractError,
                RuntimeStateError,
                UnknownAdapterError,
            ) as exc:
                print(f"pact: error: {exc}", file=sys.stderr)
                return 2
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
