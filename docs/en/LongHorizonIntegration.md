# General long-horizon runtime fixes

The main-line integration includes the native long-horizon chain through `672dc95a568ca261696e835b0c186092936c8d44`: typed native completion/evidence parsing, tool-free synthesis, frozen MemGraph reads, original-task preservation, PACT progress and public-test guards, per-stage bytecode caches, and finite transport recovery. Native transport recovery remains opt-in and does not reset a whole task.

Two further changes are promoted from the reviewed granular-resume runtime: deterministic ripgrep ordering with external configuration disabled, and reuse of the exact frozen evidence packet across clarification and PACT input generation. Concurrent proposal slots are processed as they finish, remain identified and ordered by their original sample index, and are all cancelled and joined if an integrity failure or external cancellation escapes. An incomplete or failed proposal is not converted into a valid one.

The newer native write guards, cache isolation and retry policy in this branch are retained. They are not overwritten by older versions of those three files in the frozen runtime snapshot. New lifecycle regressions exercise cancellation, joining, evidence reuse, and deterministic ordering without provider calls.

The companion controller is https://github.com/SELab-Leibniz/long-horizon-controller/pull/2. Its main tree includes the pinned PACT wheel and reviewed `vendor/jiuwen-nonstream-guard` companion: absolute read-file line numbers, nonstream task-loop handling, strict complete-JSON validation, response closure and metadata-only diagnostics. These patches target an exact external SDK baseline; they remain an explicitly selected overlay for a new frozen deployment, rather than silently replacing an installed SDK or changing existing attempts.

The historical `pact/campaign.py` adapter and `test_pact.py` / `test_controller.py` use the CampaignControlPlane API from the Chrys `040a1156` companion wheel. The original-task benchmark uses the newer WorkRuntime API and the controller's `+bytes1.deadline1.turns1` wheel. These are separate dependency contracts: do not co-install them or treat the modern wheel as a drop-in dependency for the legacy campaign.

Run the offline matrix in separate Python 3.12 environments. Both need pytest, pytest-asyncio, pydantic, PyYAML, pathspec, httpx, loguru, portalocker and MCP. Run `test_pact.py` and `test_controller.py` with the legacy wheel. Run the remainder of `tests/long_horizon` with the modern wheel, using `--confcutdir=tests/long_horizon -o addopts=''`. Linux DAC tests require the pinned Linux image; the portable file-audit controls stub privilege setup and use one sentinel spelling per namespace for case-insensitive filesystems. They do not establish kernel isolation or replace the Linux case-variant checks. Production setup still uses the normal repository installation contract. A unit-test pass or main merge is not an independent benchmark grade.

## `--lh` entry and MemGraph

`jiuwenswarm --lh ...`, `jiuwenswarm-process --lh ...` and `python -m jiuwenswarm.lh`
run the shared controller and native Jiuwen plugin embedded under `jiuwenswarm/lh/`.
The controller revision and the required PACT Core wheel are pinned in
[`SOURCE.json`](../../jiuwenswarm/lh/SOURCE.json). See the
[suite usage and installation guide](../../jiuwenswarm/lh/README.en.md).

The former ContextGraph integration is renamed MemGraph and stays disabled by default. The native read-only MCP (`jiuwenswarm.long_horizon.memgraph_mcp`) is registered only with `JIUWENSWARM_MEMGRAPH_ENABLED=1` and the `MEMGRAPH_HTTP_URL`, `MEMGRAPH_EXPECTED_RECEIPT_SHA256`, `MEMGRAPH_EXPECTED_CORPUS` and `MEMGRAPH_CALL_AUDIT_PATH` variables; the controller-side corpus query is used only when both `--memgraph-command` and `--memgraph-receipt` are given. Deployments that previously set `JIUWENSWARM_BENCH_MEMORY_ENABLED` / `CONTEXTGRAPH_*` must switch to the new names.
