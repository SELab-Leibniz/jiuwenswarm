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
Upstream components keep their original licensing terms. All inference stages
use the pinned JiuwenSwarm process interface.

The suite adds an explicit opt-in native Code tool policy for tool-free synthesis.
The original default tool policy is preserved. This extension belongs to the
versioned integration; the resulting agent is based on the requested upstream
commit, not byte-identical to it. Unit checks and real native HTTP probes must be
reported separately in the benchmark validation record.

At JiuwenSwarm revision `6a6e643e2`, standalone Campaign used the Chrys `040a1156` distribution of `pact-core==0.2.0.dev0` (upstream source `aa9073bed4970481a035755990e1682e9de486d8`, artifact SHA-256 `4f08b2b6ae258823e0e1c824380f58579a6e4174d02b3b1338a32ee9eb76ab2a`). That revision identifies historical Campaign runs.

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

## Current embedded suite sync (2026-11-25)

The `jiuwenswarm.lh` entry now selects the shared suite from
`SELab-Leibniz/long-horizon-controller@732f3640546334a3730b5c6ae408680a17dd325f`.
The controller and role ports include the shared logic for saved external
drafts and public review retry fingerprints. The native bridge was imported
at `ccb4adc6ab3bf1e592db978603a0a165ae3c2c47` from the
`vendor/jiuwen-lh-native/jiuwenswarm/long_horizon` subtree, including public-check
validation, planning checkpoints, bounded event retention, reviewer access
preflight and stage-local write isolation. The integration retains its MemGraph
names and disabled default, native transport retry rail, and opt-in public-test
write guard. The native SDK and both ordinary CLI entrypoints remain selected
by the JiuwenSwarm checkout.

This suite embeds the Python sources and runtime resources from
`pact-core==0.2.0.dev0+bytes1.deadline1.turns1.checklifecycle1` (upstream artifact SHA-256
`190ea348e1897c76159a65dd6e604cfb1e63a9fca5dd0776c092420b5675611f`).
The suite and original-task benchmark both import `jiuwenswarm.pact_core`. Core package imports resolve within the source tree shipped by WorkSwarm. The original schema identifiers and resource bytes are preserved.
The other PACT digests in this history identify earlier benchmark treatments.

The bundled pathspec wheel is the original PyPI artifact pinned by the snapshot
adapter. The Core source paths, individual file hashes and artifact origins are listed in
[`SOURCE.json`](../lh/SOURCE.json).

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
