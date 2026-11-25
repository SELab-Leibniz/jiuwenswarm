# Suite synchronization validation

## 2026-11-25 · Controller `732f364`

The shared controller and role ports match long-horizon-controller
`732f3640546334a3730b5c6ae408680a17dd325f`. The previous JiuwenSwarm PR head
was `ddc88ec606785a29b2e9a549d4ea9ac095ba4f4d`.
All 108 embedded Python files match the hashes in `SOURCE.json`. All 58
installed PACT Core Python files match the wheel pinned by that manifest.

The embedded `tests/lh` suite finished with 507 passing cases and 44 skips
across parallel and serial runs. The new fingerprint test passed after
its fixture was given a valid saved Work progress record. The bounded
deadline-only case exhausted its 120-second window during the main run
and passed when rerun on its own.

The new embedded regression checks that changes to diagnostic wording
preserve the public review retry fingerprint. New evidence or user
decisions change the fingerprint, and the receipts retain the diagnostics.
The two native `--lh --help` entrypoints and the ordinary process CLI help
all exit zero on macOS with Python 3.13.3.

The upstream external-adapter checkout passed 25 selected protocol controls
for saved draft recovery, recovery refusals, interruption, and stable
public review retry decisions. Those controls use scripted roles on the
pinned upstream source. The JiuwenSwarm entry continues saved Work via
`--work-id` and `--expected-revision`. Persistent-session recovery and
automatic retry scheduling run through the upstream external adapter.

## 2026-11-23 · Controller `ccb4adc`

Source: `long-horizon-controller@ccb4adc6ab3bf1e592db978603a0a165ae3c2c47`.
PR base head before this update: `7f04d86a29236032c62d590855a078adc55e2894`.
Environment: macOS, Python 3.13.3, and the exact PACT Core wheel recorded in
`SOURCE.json`. These controls use scripted roles and synthetic native processes;
no provider inference or independently graded benchmark was run.

### LH and native bridge

| Scope | Passing cases | Skips |
| --- | ---: | ---: |
| Function-based pytest controls, 42 files | 750 | 44 |
| unittest controls, eight files | 129 | 13 |
| Combined unique cases | 879 | 57 |

The unittest files also passed 57 subtests. The scope is `tests/lh` plus
`tests/long_horizon`, excluding the three historical standalone Core/SDK files
listed below. Linux root, UID-drop and optional native-runtime controls retain
their platform/dependency skips.

Function-based cases ran with `-n 8 --dist worksteal`; unittest files ran in
separate serial pytest processes. Pytest 9 subtest report serialization is
incompatible with the installed xdist transport for several existing unittest
subtest values, so unittest cases were verified without xdist.

Two deadline-only controls exhausted their 120-second windows under parallel
load; both passed a serial rerun. One historical question-persistence control
was updated to explicitly select interactive mode, matching the upstream test
and preserving its original unresolved-question assertion; both parameter cases
then passed.

Specific checks include:

- Work-bound planning/checkpoint recovery, independent criterion review,
  cancellation, review admission, public checks and candidate delivery.
- Public reference probe/compare results, JSON checkpoint semantics and rejection
  of candidate-generated receipts as independent reference evidence.
- 49 native worker protocol cases, with 12 subtests.
- 17 controls that launch real synthetic native subprocesses through the stage
  bootstrap, checking frozen-view hashes, log/SQLite state isolation, cancellation
  receipts, continuation and tool-free configuration.
- Existing native transport retry and public-test write-guard controls. Fake
  protocol fixtures deliberately select their fake executable; bootstrap isolation
  is tested separately through the actual companion entry.

The underlying source was also compared after import relocation and MemGraph
renaming: 94 source files match exactly after those transformations. Local CLI,
native guard, retry and delivery-helper adaptations are described in `SOURCE.json`.
All 108 recorded Python file hashes were read back successfully.

### Ordinary process CLI and entrypoint smoke tests

The process CLI suite has 983 passing cases and one skip: the remaining files
pass 963 cases with one skip, and the shared-session exit/failure injection file
passes its 20 cases serially. Running that file in parallel
can produce `SESSION_BUSY` instead of the injected shutdown failure. It is kept
separate from the remaining process CLI files.

Both `jiuwenswarm --lh --help` and `jiuwenswarm-process --lh --help` exit zero and
show the new finishing options. `jiuwenswarm-process --help` exits zero on its
ordinary parser.

### Historical compatibility limits

`tests/long_horizon/test_controller.py`, `test_pact.py` and
`test_native_tool_free.py` belong to the older standalone Core/SDK integration.
Their compatibility limits were already recorded on PR #2 before this update;
they are excluded from the new suite passing count. Four failures from the
controller/SDK files were reproduced on the unchanged old head with the same
installed environment. The old standalone workflow's default public-test guard
is retained.

Linux DAC CI and real-model end-to-end acceptance are separate checks. Local
formatters and static-analysis tools were not run, following the repository's
collaboration constraints.

### Reproduction

After installing the exact PACT wheel and the repository's test dependencies:

```bash
# Serial invocation avoids unittest subtest serialization and tight-window load.
uv run --no-sync pytest tests/lh tests/long_horizon \
  --ignore=tests/long_horizon/test_controller.py \
  --ignore=tests/long_horizon/test_pact.py \
  --ignore=tests/long_horizon/test_native_tool_free.py -o addopts=''

uv run --no-sync pytest tests/unit_tests/process_cli \
  --ignore=tests/unit_tests/process_cli/test_exit_process.py \
  -W ignore::SyntaxWarning -o addopts=''
uv run --no-sync pytest tests/unit_tests/process_cli/test_exit_process.py \
  -W ignore::SyntaxWarning -o addopts=''
```
