# Native JiuwenSwarm long-horizon suite

This package embeds the shared controller from
[`long-horizon-controller@ccb4adc6ab3b`](https://github.com/SELab-Leibniz/long-horizon-controller/tree/ccb4adc6ab3bf1e592db978603a0a165ae3c2c47).
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

The current sync adds Work-bound planning checkpoints, parallel independent
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

## PACT Core dependency

Install the external wheel from the pinned controller revision's `vendor/`
directory after verifying its SHA256:

```text
pact_core-0.2.0.dev0+bytes1.deadline1.turns1.checklifecycle1-py3-none-any.whl
190ea348e1897c76159a65dd6e604cfb1e63a9fca5dd0776c092420b5675611f
```

```bash
uv pip install /path/to/pact_core-0.2.0.dev0+bytes1.deadline1.turns1.checklifecycle1-py3-none-any.whl
```

The wheel is supplied separately and is imported only on the LH path. The older
`turns1` wheel lacks the check-lifecycle interfaces required by this suite.
