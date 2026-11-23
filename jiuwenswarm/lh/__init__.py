"""One long-horizon controller, pluggable coding-agent execution."""

from .checkpoint_guard import install_checkpoint_guard
from .controller import BenchmarkArguments, BenchmarkOutcome, LongHorizonBenchmark
from .ports import Capabilities, ExecutionBinding, Plugin, PluginSpec

install_checkpoint_guard()

__all__ = [
    "BenchmarkArguments",
    "BenchmarkOutcome",
    "Capabilities",
    "ExecutionBinding",
    "LongHorizonBenchmark",
    "Plugin",
    "PluginSpec",
]
