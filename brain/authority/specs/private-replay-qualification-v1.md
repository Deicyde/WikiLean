# Private replay qualification, version 1

Status: separate outer qualification tooling at `brain/tools/private_replay_gate.py`.
The profile is `brain-private-replay-qualified-v1`. No actual policy approval or
full-corpus qualification has been issued by implementing these tools.

This profile separates permission for private acquisition, retention and replay
from public distribution and accepted assertion authority. Source manifest IDs,
original restrictions, all evidence ancestors, old preflight readiness fields,
release profiles and the existing reproducibility gate remain unchanged. A private
qualification cannot satisfy a public release policy review or a production gate.
The unsigned trusted-local operator boundary is the same as the standalone policy
reviews: expected IDs are decisions supplied externally, not author authentication
or an automated legal judgment.

## Exact input approval

`wikilean.private-replay-qualification-approval/v1` contains all fields of the old
reproducibility approval plus `profile` and `private_policy`. Its distinct
`approval_id` domain is `wikilean.private-replay-qualification-approval.v1`.
The private policy binding names the independently approved review ID, its complete
canonical byte digest, and the root/count of its exact attachment files. The root
uses domain `private-replay-policy-attachments/v1` over sorted unique records of
`path`, `sha256` and `bytes`. Two evidence assertions may refer to the same exact
attachment file; conflicting bytes at one path fail. The review digest preserves
both assertions and all original evidence metadata.

`run` requires separately supplied expected approval and private-review IDs. It
fully verifies the v3 pack and the standalone approved private review before
creating either replay workspace. It accepts restricted objects only within that
private review; it changes no restriction. The manifest must be at the supplied
pack root so both verifiers inspect the same bytes. Every private source and
object role is covered by the existing standalone review contract.

The gate retains the canonical review and every referenced attachment under its
fresh private session directory. Attachment paths reject symlinks and traversal;
all retained directories must remain current-user-owned mode 0700 and files must
remain current-user-owned, single-link, regular mode 0400. The individual and
aggregate attachment bounds are 32 MiB and 256 MiB. The retained tree must have
exactly its declared files and necessary directories. Later edits to the original
review location do not replace the retained generation.

## Two owned builds

`run` owns two actual trusted OCI launcher subprocesses. It uses the existing
preparation, sandbox, launch-record, complete output, SQLite and release verifiers;
it does not import successful build summaries or publish a projected old approval.
Each launch is preceded by verification of the copied policy evidence and the
whole gate/helper generation. The pack remains the reducer's sole readable data
input; policy attachments are not mounted into it. The two paths, randomized
mtimes, hostile environment values, distinct containers, complete output identity,
release identity and exact baseline/provenance comparison requirements are the
existing technical rules. Policy approval cannot waive a content/topology change
or expand the existing provenance-only exception.

The session schema/domain are
`wikilean.private-replay-qualification-session/v1` and
`wikilean.private-replay-qualification-session.v1`. The session binds its input
approval and private policy, complete implementation generation, two owned launch
records and output/release measurements. It remains `pending-attestation`. A
failure never writes a successful session.

The original `reproducibility_gate.py` implementation and v1 schemas are not
rewritten. That preserves the exact code closure needed by retained v1 sessions.
The new module reuses its technical helpers but owns its policy checkpoints and
orchestration. Its implementation closure includes both gates and the standalone
policy validator. It captures the loaded tuple at import and requires current
bytes to match it before any input verification and at each later checkpoint.
A changed helper invalidates an unfinished new session until it is deliberately
rebuilt under a reviewed implementation.

## Release-specific finalization

A provenance mapping can only be completed after the replay has produced an exact
release. It is therefore a finalization prerequisite, not a circular prerequisite
for producing the first release. `finalize` requires separately expected session,
private-review and coverage-mapping IDs. It rechecks the retained policy, complete
pack, both actual owned launches, output closures, releases, baseline comparison,
and gate implementation. It then calls the independent
`provenance_coverage.check` API against the exact first release and retained
private review/attachments. Both releases must already have equal verified IDs.
No public policy review is supplied for this private qualification.

Missing coverage support, a pending/unresolved mapping, or any failed occurrence
coverage remains a hard prerequisite failure. There is no placeholder success or
option to skip this check. The checker itself must be independently reviewed
before this integration can produce a real qualification. Finalization retains
its exact mapping and newly computed report, and binds their identities/digests
and the checker's complete loaded implementation file tuple/root, including its
family-dispatch helper and transitive dependencies. It rechecks that tuple,
retained policy, pack, baseline
and both output/release closures after coverage before publishing a result.

The final schema/domain are
`wikilean.private-replay-qualification-attestation/v1` and
`wikilean.private-replay-qualification-attestation.v1`. A full-corpus pass sets only
`private_replay_qualified: true`; a fixture pass cannot set it. Every result fixes
`accepted_authority`, `public_release_policy_ready`, `production_activation`,
`reviewer_authenticated` and `legal_determination` to false. A schema-valid result
alone is not proof of the launches: re-run finalization over the externally
expected retained session and coverage mapping to verify that evidence.

## Command line

Run on the existing native Linux trusted runtime with CPython 3.12 isolated
startup, supplying the existing OCI/engine/wheelhouse/timeout arguments together
with these new mandatory inputs:

```text
private_replay_gate.py run --approval /private/approval.json \
  --expected-approval-id sha256:… --private-review /private/review.json \
  --expected-private-review-id sha256:… \
  --private-attachments /private/review-attachments \
  --baseline /private/baseline/release.json --manifest /private/pack/pack.json \
  --root /private/pack --destination /private/session …runtime arguments…

private_replay_gate.py finalize --session /private/session/session.json \
  --expected-session-id sha256:… --expected-private-review-id sha256:… \
  --coverage-mapping /private/mapping.json --expected-coverage-mapping-id sha256:… \
  --destination /private/qualification
```

There is no approval-generation, imported-success, deployment or source-relabeling
command. The output directory must be fresh and separate from retained inputs.
