# Trusted local OCI replay

`brain/tools/launch_replay_oci.py` is an offline launcher for an already prepared
v2/v3 replay workspace. Its first supported deployment target is a **native Linux
host**, a non-root operator account, and an explicitly selected local Docker Unix
socket. It never pulls an image, builds an image, contacts a registry, selects a
remote engine, or installs virtualization. macOS and emulated architectures fail
before any engine command.

This implementation has hermetic integrity/policy tests. No actual OCI image has
been built or replayed on this development Mac. Native Linux kernel evidence,
final image/dependency pins, and the full-corpus two-build attestation remain
required before an authoritative release claim.

## Inputs and preparation

The operator must retain:

- One OCI **platform manifest** and its exact config/layer blobs in an OCI layout.
  Image indexes, tags, foreign/URL layers, unsupported compression, digest/size
  mismatches, and uncompressed layer DiffID mismatches fail closed.
- The exact NumPy wheel and a reviewed canonical
  `wikilean.oci-runtime-policy/v1` document. The policy records the wheel's version,
  filename, byte count, and SHA-256; complete compiled NumPy baseline/dispatch
  feature lists; the explicit baseline OpenBLAS core; and its verified companion
  library/symbol. No example policy is pre-approved: derive these values from the
  actual pinned wheel and review them before image construction.
- A sealed execution descriptor whose image digest, architecture, dependency lock,
  installed NumPy tree, Python/SQLite facts, runner closure, and bubblewrap identity
  match the resulting runtime. `execution-environment/v1` remains unchanged for
  file-backed `_sqlite3`. New `execution-environment/v2` explicitly identifies a
  builtin `_sqlite3` by the digest of the loaded binary owning `PyInit__sqlite3`,
  together with the connected engine's version, source ID, and compile options.
  The live probe uses its corresponding v2 schema. Process-global `sqlite3_*`
  symbols are deliberately not used: on macOS they can refer to a different system
  SQLite. Development-host descriptors remain non-authoritative in both versions.
- A verified pack and freshly prepared workspace, in disjoint absolute paths,
  plus a private external directory for the launch receipt.

`package_oci_runtime.py` creates a fresh build context from exact committed runner
blobs and the verified wheel. It requires a full Git commit and an explicit
`repository@sha256:…` base image reference. The reviewed base must already contain
CPython 3.12, pip, and unprivileged `/usr/bin/bwrap`; package acquisition belongs
outside this tool. The generated recipe reinstalls the hash-locked wheel with
`--no-index --no-deps --require-hashes --no-compile` and runs all build steps with
network disabled. The image label binds the canonical numerical/artifact policy.
Retain the base-image and wheel acquisition evidence separately: a recipe alone
is not proof of acquisition or a successful image build.

The launch command requires explicit `--docker`, `--docker-sha256`, `--socket`,
`--engine-id`, and `--engine-version`, as well as `--manifest`, `--context`,
`--oci-layout`, `--policy`, `--wheelhouse`, and `--receipt`. Use `--root` when the
pack manifest's parent is not its root. Run it with CPython 3.12 `-I`.

## What execution verifies

The launcher binds the verified platform manifest to its config digest and ordered
uncompressed layer identities, then verifies that the existing engine image has
that exact config/rootfs. It creates a container without starting it, inspects the
actual command, user, mounts, network mode, and isolation flags, and only then
starts it through a fresh stdio channel. It rechecks image/container state after
exit and removes only its labelled container, including reconciliation after an
uncertain create outcome. A success receipt is written only after clean removal.

The outer container has no network, a read-only root, no capabilities, a non-root
user, no privilege escalation, bounded memory/processes, private namespaces, a
small ephemeral `/tmp`, read-only pack/workspace mounts, and writable output/scratch
mounts. The image-default environment is cleared before Python startup. The
existing inner bubblewrap boundary remains mandatory for every reducer and probe;
there is no option to disable it. Docker's outer seccomp filter is deliberately
unconfined to permit nested unprivileged user namespaces; this does **not** grant
capabilities or disable inner isolation. Hosts whose kernel/AppArmor policy blocks
the inner namespace fail closed and need a separately reviewed host policy, not a
privileged-container workaround.

NumPy's complete compiled dispatch list is disabled and checked inside the actual
probe process. The selected OpenBLAS core is measured through its pinned companion
library. The existing one-thread settings are preserved. These checks do not
replace the required cross-host/full-corpus numerical reproducibility experiment.

## Trust and evidence limits

Direct `run_replay_v2.py` CLI invocation still has no trusted-evidence or numerical-
policy flags. The private container bootstrap accepts only the live launch channel;
its result alone is **not** OCI launch authority. The outer execution record binds
the requested pack/environment, engine/image identities, complete create/exit
observations, and command-output digests. Raw reducer logs are not retained by this
v1 launcher; the receipt includes their digests, not a substitute for log retention.

The trusted host executable, local engine, reviewed image, and operator review are
the trust boundary. The record is unsigned and does not defeat malicious code
running as the same host user, arbitrary Python monkeypatching, a compromised
daemon, or forged operator documents. Cryptographic remote attestation would need
a separate signing/verification trust-anchor protocol; a private Python argument
does not provide one. Real clean-host sandbox evidence must still be retained
under the final pinned runtime identity.

Protocol references: [OCI platform manifests](https://github.com/opencontainers/image-spec/blob/main/manifest.md),
[OCI rootfs and DiffIDs](https://github.com/opencontainers/image-spec/blob/main/config.md),
[Docker create](https://docs.docker.com/reference/cli/docker/container/create/),
and [Docker container isolation flags](https://docs.docker.com/reference/cli/docker/container/run/).
