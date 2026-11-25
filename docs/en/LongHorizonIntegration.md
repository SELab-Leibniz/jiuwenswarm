# General long-horizon runtime fixes

The main-line integration includes the native long-horizon chain through `672dc95a568ca261696e835b0c186092936c8d44`: typed native completion/evidence parsing, tool-free synthesis, frozen MemGraph reads, original-task preservation, PACT progress and public-test guards, per-stage bytecode caches, and finite transport recovery. Native transport recovery remains opt-in and does not reset a whole task.

Two further changes are promoted from the reviewed granular-resume runtime: deterministic ripgrep ordering with external configuration disabled, and reuse of the exact frozen evidence packet across clarification and PACT input generation. Concurrent proposal slots are processed as they finish, remain identified and ordered by their original sample index, and are all cancelled and joined if an integrity failure or external cancellation escapes. An incomplete or failed proposal is not converted into a valid one.

The newer native write guards, cache isolation and retry policy in this branch are retained. They are not overwritten by older versions of those three files in the frozen runtime snapshot. New lifecycle regressions exercise cancellation, joining, evidence reuse, and deterministic ordering without provider calls.

The companion controller is https://github.com/SELab-Leibniz/long-horizon-controller/pull/2. Its main tree includes the pinned PACT wheel and reviewed `vendor/jiuwen-nonstream-guard` companion: absolute read-file line numbers, nonstream task-loop handling, strict complete-JSON validation, response closure and metadata-only diagnostics. These patches target an exact external SDK baseline; they remain an explicitly selected overlay for a new frozen deployment, rather than silently replacing an installed SDK or changing existing attempts.

The historical `pact/campaign.py` adapter uses the CampaignControlPlane API from
the Chrys `040a1156` Core wheel. The current suite and original-task benchmark
use the WorkRuntime API from the `+bytes1.deadline1.turns1.checklifecycle1` wheel.
Both wheels ship in this checkout. Each environment selects one Core.

For `test_pact.py` and `test_controller.py`, activate a separate Python 3.12
virtual environment and run the following from the checkout.

```bash
python -m pip install ".[test]"
python -m pip install --no-index jiuwenswarm/lh/wheels/pathspec-1.1.1-py3-none-any.whl jiuwenswarm/lh/wheels/legacy/pact_core-0.2.0.dev0-py3-none-any.whl
python -m pytest --confcutdir=tests/long_horizon -o addopts='' tests/long_horizon/test_pact.py tests/long_horizon/test_controller.py
```

For the modern suite, `uv sync --frozen --extra lh` in a separate checkout or
environment installs the current Core and the default development tools.
Linux DAC controls exercise kernel isolation on the pinned Linux image.

## `--lh` entry and MemGraph

`jiuwenswarm --lh ...`, `jiuwenswarm-process --lh ...` and `python -m jiuwenswarm.lh`
run the shared controller and native Jiuwen plugin embedded under `jiuwenswarm/lh/`.
The controller revision and the required PACT Core wheel are pinned in
[`SOURCE.json`](../../jiuwenswarm/lh/SOURCE.json). See the
[suite usage and installation guide](../../jiuwenswarm/lh/README.en.md).

The former ContextGraph integration is renamed MemGraph and stays disabled by default. The native read-only MCP (`jiuwenswarm.long_horizon.memgraph_mcp`) is registered only with `JIUWENSWARM_MEMGRAPH_ENABLED=1` and the `MEMGRAPH_HTTP_URL`, `MEMGRAPH_EXPECTED_RECEIPT_SHA256`, `MEMGRAPH_EXPECTED_CORPUS` and `MEMGRAPH_CALL_AUDIT_PATH` variables; the controller-side corpus query is used only when both `--memgraph-command` and `--memgraph-receipt` are given. Deployments that previously set `JIUWENSWARM_BENCH_MEMORY_ENABLED` / `CONTEXTGRAPH_*` must switch to the new names.
