"""Drop-in constructor bindings for both old benchmark entry points.

These constructors call the SAME controller. They do not invoke or subclass
either native long_horizon_benchmark implementation.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from .controller import BenchmarkArguments, LongHorizonBenchmark


def arguments(value: Any) -> BenchmarkArguments:
    if isinstance(value, BenchmarkArguments):
        return value
    return BenchmarkArguments(**asdict(value))


def jiuwen_long_horizon(args: Any, **options: Any) -> LongHorizonBenchmark:
    from .plugins.jiuwen import JiuwenPlugin

    return LongHorizonBenchmark(arguments(args), plugin=JiuwenPlugin(**options))
