"""Migration notice for the retired standalone Campaign command."""

import sys


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    help_requested = args in (["--help"], ["-h"])
    print(
        "The standalone Jiuwen Campaign command has been retired. Start a new Work with:\n"
        "  jiuwenswarm-process --lh --workflow suite --task TASK_FILE --cwd GIT_REPO\n"
        "For the original-task workflow, use --workflow benchmark. "
        "Use jiuwenswarm-process --lh --help for all options.",
        file=sys.stdout if help_requested else sys.stderr,
    )
    return 0 if help_requested else 2


if __name__ == "__main__":
    raise SystemExit(main())
