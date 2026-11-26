# Suite synchronization validation

## 2026-11-26 · Default runtime dependencies and initialization smoke

The dependency audit against PR head `69e22ba6a` found one missing default LH
dependency: `pathspec==1.1.1`. It is now in `project.dependencies`; the empty `lh`
extra remains for compatibility. All 318 non-project package records in `uv.lock`
are unchanged, and uv 0.9.30 `lock --check` passes. See [DEPENDENCIES.md](DEPENDENCIES.md)
for direct, deferred and conditional imports and package resources. The final
change preserves the concurrent default-dependency fix and regression in
`cc54be6e6`; the lockfile is identical to that commit.

Production installation was verified in Linux/amd64 with the harness builder's
uv 0.9.30 image, CPython 3.11.14, and:

```sh
uv sync --frozen --no-dev --no-editable --python 3.11 \
  --python-platform x86_64-manylinux_2_28
```

The fresh environment contains 254 packages, including pathspec 1.1.1, and has
neither pytest nor mypy installed. `uv pip check` reports no incompatible packages.
The installed package retains the `lh` extra metadata. All 63 PACT source/resource
hashes match the manifest; the native config, process protocol schema, requirement
files and bundled pathspec wheel are present and readable.

A second container mounted only the installed runtime and an empty evidence
directory, with no checkout mount and `--network none`. From `/tmp` it ran:

```sh
/opt/jiuwenswarm-host/venv/bin/python -I \
  -m jiuwenswarm.lh.initialization_smoke \
  --check-native-imports --output /smoke-output
```

The installed controller reached `s0_frozen`, read and applied the nonempty
`.gitignore`, and verified the real Core candidate, S0 checkpoint and snapshot.
The role-call ledger and worker call count remained zero. The isolated native
bootstrap/import check also passed for `process_cli.machine` and `process_cli.main`
from site-packages. It did not execute either entrypoint or contact a provider.

Twenty-five targeted tests passed on macOS/Python 3.11, covering initialization,
Core packaging, snapshot resources and run-JSON routing. The new negative control
blocks only pathspec resolution: real S0 initialization then fails with
`ModuleNotFoundError` retained in `selection.json`, phase `created`, no S0 snapshot
and zero role/worker calls. Another control makes the optional native check throw
after successful S0 and verifies the smoke returns failure rather than a false pass.

```sh
python -m pytest -q -o addopts='' --confcutdir=tests/lh \
  tests/lh/test_initialization_smoke.py tests/lh/test_pact_core_sources.py \
  tests/lh/test_snapshot_resources.py tests/lh/test_lh_run_json.py
```

The `LH production initialization` workflow repeats the fresh default installation
and network-disabled installed-package smoke on relevant changes. It preserves
the smoke JSON, selection, snapshot and native import logs even on failure.
These checks establish installation and initialization coverage; no model task,
DeepSWE rerun, native role execution or external grader was run for this change.

## Default production installation

From a clean checkout, run the platform's production install and check the installed package metadata:

```sh
uv sync --frozen --no-dev --no-editable
uv run --no-sync python -I -c 'from importlib.metadata import requires, version; assert "pathspec==1.1.1" in requires("workswarm"); print(version("pathspec"))'
```

On macOS with Python 3.13.3 this installed `pathspec` 1.1.1. The installed
`capture_workspace` kept a tracked file and an untracked file re-included by
`.gitignore`, and excluded an ignored file and directory from the frozen view.

## Source-based WorkRuntime

Both workflows import `jiuwenswarm.pact_core`. Run the workflow regressions from a
fresh checkout with the project's base and test dependencies installed:

```sh
python -m pytest -q -o addopts='' --confcutdir=tests/lh tests/lh/test_pact_core_sources.py \
  tests/lh/test_suite_flow.py::test_shared_suite_restores_p1_planning_recall_and_localization_before_review \
  tests/lh/test_portable_flow.py::test_third_agent_runs_same_controller_without_native_packages
python -m pytest -q -o addopts='' --confcutdir=tests/long_horizon tests/long_horizon/test_campaign_migration.py \
  tests/long_horizon/test_original_task_benchmark.py::test_real_core_original_task_p0_clarification_and_sealed_latest_candidate
```

The two workflow cases run real WorkRuntime operations on temporary Git repositories
with scripted roles and check the exported candidate and delivery. The native bridge
case separately checks that the original-task adapter seals its latest candidate.

On macOS with Python 3.13.3, commit `610d87e1e` passed 68 targeted controller,
CLI and public-check tests and 17 native bridge, evidence and migration tests.
Through the process CLI, scripted `suite` and `benchmark` runs each delivered
the requested file from a clean checkout and again from an offline installation
of the WorkSwarm distribution. These fixtures did not call a model or run an
external grader.

Both the sdist and the wheel contained all 58 WorkRuntime Python files and
five runtime resources. The Python syntax trees matched the pinned upstream
artifact after normalizing package imports, and the resource bytes matched
exactly. The 101 controller and native bridge files matched the manifest.
The only nested dependency wheel was the public `pathspec==1.1.1` artifact.

## Historical wheel installation at `6a6e643e2` (2026-11-25)

The source distribution and the WorkSwarm wheel built from it contain the same
three pinned artifacts. Both PACT wheels match their respective source Git
blobs byte for byte. The pathspec wheel matches the public hash in the base
branch's lockfile. All 108 embedded Python files matched `SOURCE.json` in both
archives.

On macOS with Python 3.13.3, two isolated environments started with the existing
JiuwenSwarm base dependencies. Each installed WorkSwarm, pathspec, and its PACT
variant from the main wheel using pip with indexes and cache disabled. Both LH
console entrypoints and the historical Campaign module produced their help
output. Appending bytes to a copy of the pathspec wheel caused pip to reject
that install at the hash check.

The installed suite passed 53 targeted packaging, CLI, run-JSON, portable
workflow and suite tests with scripted roles. The separate Campaign environment
passed all 42 tests in `test_pact.py` and `test_controller.py`, including 16
subtests. The original dependency declarations and all 318 existing dependency
lock entries matched `develop`.

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
