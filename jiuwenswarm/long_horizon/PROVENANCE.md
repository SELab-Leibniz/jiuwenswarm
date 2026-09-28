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
