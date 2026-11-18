"""Run the long-horizon controller with ``python -m jiuwenswarm.lh``."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main(prog="python -m jiuwenswarm.lh"))
