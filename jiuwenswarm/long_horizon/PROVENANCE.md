# Fixed long-horizon integration provenance

The native agent implementation starts from openJiuwen-ai/jiuwenswarm
`49c9f5b4c66d193e4168bfa9c82c2df025c773f0`. Added integration code is a separate,
versioned treatment; its measurements must not be attributed to the unmodified
upstream commit.

Requirement-clarification schemas, prompts, validation and snapshot semantics,
and the deterministic PACT adapter rules were initially adapted from Chrys
`6fcfb3321892735631b49f33236b1e2a988ded2d`. The current migration target includes
its stage-recovery update `0561cf0fa584fd56fdcd98c90cca6a5bc1890d95`: separate
public-verifier investigation and tool-free synthesis, bounded artifact repair,
reviewed-criterion checkpoints, public command receipts and explicit P0 recovery.
Original copyright notices and the original revision of unchanged adapted files
are retained.
This private integration does not grant a new license to upstream code. There is
no Chrys solver or model client in the new execution path: all inference stages
use the pinned JiuwenSwarm process interface.

The suite adds an explicit opt-in native Code tool policy for tool-free synthesis.
The original default tool policy is preserved. This extension belongs to the
versioned integration; the resulting agent is based on the requested upstream
commit, not byte-identical to it. Unit checks and real native HTTP probes must be
reported separately in the benchmark validation record.

PACT's independent control plane remains `pact-core==0.2.0.dev0`, from source
`aa9073bed4970481a035755990e1682e9de486d8`; its externally supplied wheel has SHA-256
`4f08b2b6ae258823e0e1c824380f58579a6e4174d02b3b1338a32ee9eb76ab2a`.
The wheel, runtime outputs and credentials are not committed here.

The snapshot adapter adds `pathspec==1.1.1` as an explicit runtime dependency in
`requirements.lock`, with the same wheel hash already present in upstream's
`uv.lock`. The upstream `--no-dev` install does not include this package; the
separate hash-checked installation is recorded as a suite-only dependency layer.

There is no route classifier: this entrypoint always selects the long-horizon
workflow. External memory and writeback are disabled for the initial treatment.
Graph construction, embeddings and release services remain outside this agent
integration. The benchmark repository owns dataset revisions, complete task
selection, independent grading and frozen run identities.

The original-task benchmark entry is migrated from Chrys
`3f78185e298800b3e3f735f932a7ee2abb7bc25f`, including its routing-off fix.
It creates canonical PACT Work from the verbatim original task, runs P0 once,
clarifies frozen S0, binds the acceptance brief and enters the public
BenchDriver with native Jiuwen Worker/Reviewer adapters. This separately selected
workflow does not synthesize Goal Contract, Initial Plan, Missions or a mandatory
public verifier before entering PACT. Core owns derived checks, review cadence,
acceptance and delivery. A sealed export may be independently graded even when
internal acceptance has not occurred. External grading never runs in this agent.
Every actual native clarification call enters the same immutable trial ledger.
Native reviewer transport captures its final JSON with read-only source tools;
this differs from Chrys's writable transport file. The native proposer remains
the separately documented combined investigation/synthesis adapter.

This workflow uses the updated external `pact-core==0.2.0.dev0` wheel, SHA-256
`06c8c2ccba2112ce7614fc3ba0866539bd53f8314c15bce9049153468b8b5d2a`.
Its dependency version is unchanged but its bytes and public WorkRuntime/
BenchDriver interfaces differ from the legacy wheel. Both are explicitly pinned
by their selecting benchmark configuration; they cannot be silently substituted.
The original JiuwenSwarm uv.lock and SDK revisions remain unchanged.


## Caller-owned repeat-cycle stop (2026-09-29)

The original-task benchmark caller stops after three identical reviewed inputs
in consecutive completed Worker rounds. It reads the pinned Core budget ledger;
bookkeeping revisions and polling do not count. Native Core controls verify
that the latest candidate still exports without acceptance. Core/SDK internals
and independent grading are unchanged. The control fixture has its own budget
and does not represent a paid-model evaluation.

Sync basis: Chrys 0e99732ba6b71ddd7ec1b0f6c3df552a90416ecd (exact-candidate verification). PACT Git-link export is a separately pinned host wheel; Jiuwen SDK uv.lock remains unchanged.

Also synchronized Chrys `650483a113e7235befa8693a898a1bdf0b34b7f9`:
registered public checks must have a verification before consuming a reviewed
round. An unverified Worker save cannot manufacture a stop threshold.

The next benchmark treatment can select the external Core wheel SHA-256
`592efcacf132b9b79f8be87d64bd8a49e47c2f90286db60bf5c5fd48131d77a9`.
It comes from PACT `a2e50f6709caaa29e3bbd87b114d981a543ae00c` plus the
Git-link export patch published with Chrys `0e99732ba6b71ddd7ec1b0f6c3df552a90416ecd`.
Opaque submodule commit identities are exported and hash-verified without
fetching submodule contents. Old running images retain their selected Core.


Synchronized Chrys `e5bd40ba483970c7a04fcb55d8ec5679361274fc`: Worker and
Reviewer role context requires candidate-local rebuild/install when imports
resolve into the original source checkout. Original Git and submodule metadata
must not be copied or modified. This is a role instruction change; Core source
ownership/export guards and the `592efcacf...` Git-link wheel remain unchanged.
It does not repair Core cleanup of read-only Go module caches.

The original-task benchmark's `NativeBenchmarkAdapter` also appends this
boundary to both semantic roles. A real native transport control caught the
initial migration into only the legacy role adapter; the corrected benchmark
path is tested independently before new task admission.


Synchronized Chrys `12e0a2a8cf880d0db1c8e1ecf27966c27a0d7b89`: sealed
exports keep unchanged original external fixture links as v3 opaque metadata.
The native stage bridge reconstructs only their literal link bytes in a private
capture tree, where snapshot capture seals them as hidden blobs. No external
target enters a model view. Rewritten/new escapes and aliases fail closed.
This requires the selecting host to pin the matching updated Core wheel; an
old running image is not updated by this source commit.

The separately maintained public Core repair also prepares read-only ignored
Go cache directories for Git clean inside controller-owned Worker worktrees,
without chmod of files, hard links or external symlink targets. These repairs
change the explicit treatment dependency and apply only to newly admitted tasks.

The combined fixture/cache boundary wheel for this treatment has SHA-256
`2d939bc863f5f1a4195183a32c6ebeaedc7341b9e6b0dce7e8fbe4395f525948`.
It retains the source base `a2e50f6709caaa29e3bbd87b114d981a543ae00c`,
Chrys12e0a2a8's public export patch and the owning-Core directory cleanup
repair. Its public source revision and control receipts belong to the host's
release provenance; the binary remains outside this repository.


## Current PR22 + work-session treatment (2026-10-08)

This section supersedes the earlier Core wheel pins for this source revision.
Jiuwen integration base: `6cfe9025af9cca10d6eab2d6ff470949745dd9b0`.
Core base: merged PR #22, `e21464732b03a1492b25f5c3e1134860810bd667`.
Core feature source: `3c320312f871dfd8920282d3a5811be7bc61542b` (local, not yet pushed).
Wheel SHA-256: `c26c2c605454037e1b4b77030e0864561fa9debd79703d2c503b3608ea583649`.
The machine-readable selection is `PACT_CORE_PIN.json`; the wheel remains external.
A clean git-archive wheel equals the tested wheel byte-for-byte, and all 66 Python
source files match both the source commit and the installed validation package.

Campaign uses WorkRuntime + DefaultDriver; original-task benchmarks use BenchDriver.
Acceptance remains fixed_command. Jiuwen host execution-evidence export is not part
of this treatment, so reviewer_evidence is not claimed.

Opt-in Worker resume changes native process cleanup: it drains and fsyncs root
session history, emits a checkpoint receipt, and requires its integrity on reuse.
Only cleanly finalized calls can be resumed after timeout/cancellation. A registry,
per-work caller lock and retained native state allow a new caller invocation to
continue the same session without resetting the trial deadline. Reviewer stays fresh.
The native agent and integration are therefore modified; results must not be
attributed to the unmodified upstream implementation.

Local validation: macOS arm64, Python 3.11.15, 1,130 tests and 76 subtests passed,
1 skipped. Tests cover the complete long_horizon suite, process_cli suite and atomic
session-history tests, using controlled native processes and the real Core package.
The production Python 3.12 image, full native SDK and paid-model benchmark were not
run here. See `WORKER_RESUME_VALIDATION.md` for commands, boundaries and log hashes.


## Current 0.3.0 development-series pin (2026-10-08)

This supersedes the earlier wheel selections for this revision; historical records
above remain unchanged. The base version is `0.3.0.dev1`; this is not
the final 0.3.0 release. Core source: `f47bd8d4cf22c5b3bcc3ec108938d062be3665cb`.
Installed private version: `0.3.0.dev1+gf47bd8d4cf22c5b3bcc3ec108938d062be3665cb`.
Wheel SHA-256: `495b4ab4fe3d51280dfbb5476691ee19fc5efa6169e0a4084cfd94417b32e543`.

Core's clean-archive builder automatically appends the full source commit to the
single-source version and generates `pact_core/_build_info.json`. All 66 Python
files match the source commit after that declared version-only transformation,
and the installed files match the wheel. Against the preceding fully tested
Worker-resume wheel, only `pact_core/__init__.py` differs among Python files.
Package metadata, Python API and CLI report the same full version.
The exact current selection is `PACT_CORE_PIN.json`.

Version-only checks: Core build/CLI 28 passed; Jiuwen integration 29 passed and
14 subtests passed. See `VERSION_030_VALIDATION.md`; no new full-suite or model claim.
