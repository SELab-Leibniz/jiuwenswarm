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
