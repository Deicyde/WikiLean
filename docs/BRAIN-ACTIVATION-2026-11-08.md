# R1 production activation — 2026-11-08 UTC

R1, “Tag this passage,” is live. The exact promoter completed at
2026-11-08T03:38:01Z with journal outcome `deployed` and a passing live canary.
Jack delegated continuation of the release and Literature Links work; Codex
selected and reviewed the concrete technical identities below. Existing
source-policy and licensing decisions remain unchanged.

| Identity | Value |
|---|---|
| Authority and reducer commit | `596aa7bb92e89c1f7a44f768392c3b895b049c47` |
| Release | `sha256:13c7517ba6eb9c2ddd698af17629442e41c6af84619f00b95dfc1a6afeea3aa9` |
| Release manifest SHA-256 | `8f607c425a12641b0ab348c0781bbda5d52347c171d7782da54cf54da08cee9f` |
| Worker version | `7b73c447-0285-48d5-8153-9bb00f729eff` |
| Deployment | `bf151fe2-5d99-4d00-a7e7-8760e9a18cc4` |
| Activation bundle | `sha256:5f41231ceeb2d8d720475eab5e66eb762a98f6dd5c9bb92e1cfc58b63f8ceac5` |
| Public baseline | `sha256:be1aea31812c4e8d03a209483bd04d454ea2d231a513427b2f56cae9e826bc86` |
| Retained promoter artifacts | `sha256:d1d7b99533e032f5502eb5beb6a8935938385853dc1dce4d0cedf98bbd24cc3f` |
| Attempt | `20261108T024111Z-13c7517ba6eb-c992bf80ec` |
| Final journal event / chain tip | `sha256:b1826c7334a0504983d9af04804586d13f259fe52e4ce3e204849460739d3d2b` |

The attempt ID was frozen in the retained dry run. The deployment intent and
invocation were recorded at 03:31:59Z; the final predeployment check passed at
03:37:34Z. The ID's earlier timestamp does not date the production mutation.
Full evidence, the canonical append-only journal and recovery copies are kept
in the operator's private stores. Live identity is available at
[/api/version](https://wikilean.jackmccarthy.org/api/version).

## What shipped and what was checked

- Human and agent community tags on the existing formalization reader, with
  attribution, CC0 at first edit, newcomer limits, Turnstile and the site-wide
  write brake. Editing on the old `/:slug` plane is off; its pages remain readable.
- Agent identity and token-management routes. No operator token or contributor
  identity was minted for release verification.
- Literature Links 1.0.2 support and the preview-first, admin-session migration
  endpoint. The live structural conversion finished on 2026-11-08: the
  08:15:42Z D1 observation found all 778 articles at schema 4, zero legacy
  articles, 778 exact migration archives and 5,972 revisions. The full
  preservation audit passed at 08:17Z. The write brake remains locked while
  final browser-journal inspection and writer restoration are completed;
  `slug_editing` remains off.
- Migrations 0017, 0018 and 0019 applied after a fresh encrypted backup and an
  in-memory schema rehearsal. The existing 0014 schema was verified and left
  in place. All 19 original table row counts stayed unchanged through the DDL.
- Fresh sealed CI: 1,527 Worker tests, typecheck, and 131 offline Python commands.
  The bundle passed an independent verification before the one-shot promotion.
- Thirteen live feature checks passed, including the tag entry point, D1 overlay,
  anonymous write/admin refusals and old reader URL redirects. A names-only
  check confirmed `TURNSTILE_SECRET` on the deployed Worker; no secret values
  were requested. No synthetic contributions or legal agreements were submitted.

The seven-artifact semantic comparison against the actual prior production
release is complete and reports no differences. Of 2,414 public Brain paths,
2,409 are byte-identical; the remaining five differ only in generation metadata.
No new raw source corpus or full u1 declaration index was published. The existing
source-distribution question remains open. No new paid model calls were made.

## Retained prior state and remaining work

The retained prior release is
`sha256:7132f5283d4e720c149296e2e0f11eb381f05ba8e72d239350e9ee04d77b4d35`,
manifest `58b7121389214f23408650e5929d9e23af93afaa4350bb8d8895fa320b18049d`,
Worker `fa58d14d-520d-4358-ad80-d37abcc23af6`. Its frozen files and the older
protected release remain retained. The rollback drill remains unrun.
For a rollback after native data migration, follow
[the guarded structural rollback procedure](COMPANION-NATIVE.md#history-and-rollback)
and check reader compatibility first.

The live conversion ran inside the recorded exclusive maintenance window.
The local moderation job was paused while idle, the exact Cloudflare drift
schedule was removed, and propagation and in-flight completion were verified
before the final encrypted backup and copied-database rehearsal. Each article
was converted by the deployed admin-session endpoint with its previewed version,
source revision and snapshot digest. There was no whole-database restore or
replacement from the disk cache.

A fresh post-conversion encrypted export passed the full 23-table in-memory
restore, integrity and foreign-key checks. The application-data comparison
against the final pre-conversion backup verified all 38,259 work items,
17,125 links, 23,374 unlinked records, 15,963 distinct per-item formal targets,
35 human work items, 4,416 original revision rows, 778 exact archives and
1,556 new checkpoint/native history rows. Source context, statuses, proposal
strings and prior attribution were preserved. No special-case direction,
Mathlib commit pin or noMatch was invented. This is structural preservation,
not a claim of mathematical correctness. Authentication data was restored
only in memory; the private application projection excludes it. The new
ciphertext has not had an independent age-decryption test.

The dedicated native-aware operator runtime now mirrors all 778 articles from
a fresh sealed D1 snapshot. Its 15,954 reverse citations and 5,432 autocomplete
entries were checked against the audited database. The `declcites:v1` KV cache
was updated and its exact bytes read back successfully. The private graph source
rebuild passed independent verification under the current native-aware profile;
its six outputs are installed only in the dedicated operator runtime. The
control plane was refreshed using those outputs and the retained structural
edge inputs. The autocomplete asset is also local to that runtime. Neither a
new Worker nor new public static assets have been deployed.

The final browser-journal inspection and deliberate restoration of the recorded
writer state remain pending because the native browser controls cannot read the
live page reliably. The operator has retained the previous moderation schedule
and the exact drift
schedule (`17 6 * * *`). Only the previously active writers may be resumed after
all remaining checks. The browser's final client journal still needs inspection.

The post-conversion encrypted backup and application projection were copied to
ffmain and their hashes verified. Full evidence remains private. The source
export follow-up ([PR #154](https://github.com/Deicyde/WikiLean/pull/154))
preserves the existing community node and attribution, keeps the export
restricted, and does not add that node to the static Brain. The original
curated Git commit remains `d4ed6e42…`; the current normalizer uses fresh D1
source identities without relabeling historical proof bundles.

Follow [the live migration procedure](COMPANION-NATIVE.md#live-migration-procedure)
for the remaining restoration and window closure. R2–R6, source-policy approvals
and the external launch announcement remain outside this activation.
