# Native JiuwenSwarm long-horizon suite

This package embeds the shared controller from
[`long-horizon-controller@732f36405463`](https://github.com/SELab-Leibniz/long-horizon-controller/tree/732f3640546334a3730b5c6ae408680a17dd325f).
`SOURCE.json` records the full revision, file hashes and integration adaptations.
The native bridge remains in `jiuwenswarm.long_horizon`.

```bash
jiuwenswarm --lh "Fix issue #123" --cwd /path/to/repo
jiuwenswarm-process --lh --task task.md --cwd /path/to/repo --verify "pytest -q"
jiuwenswarm-process --run-json /path/to/run.json --lh
python -m jiuwenswarm.lh --task task.md --workdir /path/to/repo --output /tmp/lh-output
```

Both ordinary CLI entrypoints retain their existing behavior without `--lh`.
The run-JSON adapter works in a private workspace and applies the published
candidate patch back to the caller's workspace. Its stdout remains JSONL,
including `lh.enabled`, `lh.finished` and the terminal `result` record.

The default `suite` workflow freezes S0, runs P0, investigates the original
requirements on S0, constructs a Goal Contract and Initial Plan, runs P1, and
enters PACT's review, repair, acceptance and delivery loop. The `benchmark`
workflow remains available as an explicit alternative.

The suite supports Work-bound planning checkpoints, parallel independent
criterion review, isolated executable reviewers, check-method diagnosis,
time-based review admission, candidate archives and finishing reserves. Early
acceptance can trigger a bounded full-requirement audit. Public reference tools
can be compared at several checkpoints, with candidate differences and reference
availability recorded separately. Native transport keeps bounded in-memory
events and complete safe event records, and isolates read-only stage state,
logs and caches from the frozen source view.

Chrys/ACP backends, external SDK drivers, benchmark launchers, C7 and public-CA
overlays remain in the controller repository. The embedded package includes the
archive, check-receipt and delivery helpers used by the shared controller.

This revision syncs the shared logic for saved failed drafts and public review
retry fingerprints. The upstream external adapter resumes drafts in a persistent
model session and uses structured evidence to schedule retries. The native
Jiuwen entry resumes saved Work via `--work-id` and `--expected-revision`.

## Configuration

- Suite defaults: 48-hour budget and 64 implementation turns. Benchmark defaults:
  one hour and three turns. Explicit deadline-only mode remains supported.
- `--finish-reserve-seconds` and `--cutoff-at-unix` configure the finishing boundary.
- `--review-diagnosis-limit` and `--review-acceptance-limit` set review fuses.
- `--clarification-max-rounds` and `--clarification-budget-ratio` bound clarification.
- `--allow-user-interaction` preserves waiting for human answers; unattended runs
  use the suite's unresolved-question policy.
- `--reviewer-uid` and `--reviewer-gid` select a dedicated reviewer identity for
  Linux root execution.

MemGraph remains disabled by default. Controller retrieval requires both
`--memgraph-command` and `--memgraph-receipt`. Native MCP retrieval requires
`JIUWENSWARM_MEMGRAPH_ENABLED=1`, `MEMGRAPH_HTTP_URL`,
`MEMGRAPH_EXPECTED_RECEIPT_SHA256`, `MEMGRAPH_EXPECTED_CORPUS`, and
`MEMGRAPH_CALL_AUDIT_PATH`. The native transport retry rail and opt-in public-test
write guard are retained.

## Install from the PR

Use Python 3.11–3.13 in this checkout. Both the default `suite` and the original-task `benchmark` use the [PACT Core WorkRuntime source](../pact_core/) included in WorkSwarm. The `lh` extra installs `pathspec` for snapshot filtering.

```bash
uv sync --frozen --extra lh
uv run --no-sync jiuwenswarm-process --lh --help
```

If this branch is already installed through the usual JiuwenSwarm setup, activate that environment and install `pathspec` offline. pip verifies the bundled file's SHA-256 digest.

```bash
python -m pip install -r jiuwenswarm/lh/requirements.txt
```

## Install from a single WorkSwarm wheel

The WorkSwarm wheel and source distribution produced by `uv build` include the Python modules, role profiles and dashboard assets for WorkRuntime. To install offline in an environment with the JiuwenSwarm base dependencies, run these commands in the wheel's directory. The extraction step gives pip access to the bundled `pathspec` dependency.

```bash
python -m zipfile -e workswarm-0.2.5b1-py3-none-any.whl unpacked
python -m pip install -r unpacked/jiuwenswarm/lh/requirements.txt
python -m pip install --no-index --no-deps --force-reinstall ./workswarm-0.2.5b1-py3-none-any.whl
jiuwenswarm-process --lh --help
```

To migrate from `python -m jiuwenswarm.long_horizon`, start a new Work with `jiuwenswarm-process --lh --workflow suite --task task.md --cwd /path/to/repo`. Select `--workflow benchmark` for the original-task workflow. [Core version, origin and per-file hashes](SOURCE.json)
