# Brain SQLite continuation — 2026-09-08

This is an operational checkpoint. `ROADMAP.md` remains the completion contract.
The work is on `codex/brain-architecture-phase1`, continued from `509c6f65` in
`/Users/jack/Desktop/LEAN/WikiLean-migration`. The main checkout was synchronized
to `ebac34dc`; its 670 existing data/cache changes were preserved and backed up on
`codex/local-data-backup-20260908-132955` (`ebcb58c7`). Another task may continue
committing in that original checkout; do not reset or clean it.

## Authority status

The migration is **not complete**. No reviewed full-corpus v3 source plan, real
full-corpus offline pack, two-build attestation, authoritative release, or production
activation has been produced. Production still uses compatibility authority.
No deployment or D1 write was performed during this continuation.

The first reviewed implementation tranche is committed as `f51fc27c` (source evidence,
inventory coherence, OCI tools and sealed publication fixes), followed by `3f822f04`
(native ARM OpenBLAS identity). Further acquisition and pack-bound release work has
passed integration checks and is committed as `d4ed6e42`, followed by `c64438d2`
(dataset evidence and actual native replay isolation), then `a99d48fa` (derived
catalog fragments and retained rejected observations). Completion of a fixture suite is
not evidence of a full-corpus or native Linux OCI run.
The next identifier and public Git tranche is committed as `dded5b39`.
The subsequent Git harvest, external Git, ProofWiki, EOM raw and complete
Wikidata-derived input tranche is committed as `bb179a0c`.
The EOM normalization, DLMF, OEIS, Kerodon and LMFDB acquisition tranche is
committed as `c3265f80`.
The final catalog importers and experimental assertion shadow are committed as
`f670cbf3`. Commit `1bd15ac3` adds D1 export compatibility, sealed proposal
folds, standalone policy review records and same-input legacy halo preparation.
Commit `945f9c81` adds the byte-preserving legacy SQLite compatibility projector.

## Real source acquisitions

The sealed read-only D1 acquisition succeeded at `2026-09-08T13:41:25Z`. Independent
bundle verification confirmed 778 articles, zero community edges, and zero community
nodes, from one query. The private bundle is:

`/Users/jack/.local/share/wikilean-migration/d1-snapshots/16f5e9f905f707f9b5418d71e2d9a901262df62a2ed96b11d7d8d3e316ff5123`

- Acquisition receipt: `sha256:66cede70275acc9e9c8acee18c83c780ce1aa423729f0001504059c17a641d2a`.
- Normalization lineage: `sha256:bca17ac9b8ca6d1d6891ce7967fbcb94176c7437012f81315a32bde5acea9b5f`.
- The raw capture stays private. Existing disk annotation metadata is not authority.
- Historical acquisition tool generations must remain verifiable when current helper
  code changes. Accept whole reviewed wrapper/dependency tuples, never mixed hashes.

The independently verified private source export is
`/Users/jack/.local/share/wikilean-migration/d1-sources/b722ef778e3864aef1f5af52ae71fa145894e4515ced60b934b9e6853d3e57d3`.
Its `source-fragment.json` supplies two source entries and annotation/community
bindings for physical root `d1_export`. It retains 1,599 files/116,047,701 member
bytes, including the original capture and full seven-file implementation closure.
The parent source ID is
`sha256:784829681026a3789a337119b9d40af9ffde5fcdf45bb271264bbb228a4df96f`;
the derived source ID is
`sha256:23d534a9275b49dc04f38bcc9efe92eda350bd499ff1c43256843ecc411452bf`.
An earlier private export `ec2726ed...` is retained for audit and superseded by this one.

The combined 106-source preflight subsequently found one digest declared with
incompatible media types: the empty byte string appears in 255 Git-derived objects
as `application/octet-stream`, and in five D1-derived objects as NDJSON. The
compiler correctly rejected that combined declaration. The five D1 objects are
normalization outputs, so no new production query was needed. Exporter v2 reruns
the original verified raw rows and declares truly empty outputs as octet streams.
It retains the original acquisition receipt; its verification-only v1 dispatch
still reproduces the historical export from the complete pinned implementation.

The new independently verified export is
`d1-sources/315ef420a28828a7ed827b6208a89c530d76daf4f5837524e8ae34d28df19e4e`
under the private migration root. Its parent is
`sha256:a5b3003a1100f56158964b760fa5b3f8c94bf5dd79cadc017b08d841f1527fb9`
and sidecar source is
`sha256:e67d10faa96b9f2dc215a50bec17df24c83e1c561e05fd8536773661ae7391a3`.
All 11 acquisition files (39,772,274 bytes) and 779 normalized files
(17,217,882 bytes) are unchanged. The private
`preflight-20260908/d1-export-refresh.json` records the exact comparison and
transitive refresh: all four derived catalog children change identity because
their configuration binds every parent. Proposal folding has now been refreshed
against the new hierarchy parent. OpenAlex's unchanged acquisition contract binds
the exact theorem-link parent, requiring a new plan and fresh capture. Keep the
original draft and all old evidence until the replacement closure independently verifies.

The derived-catalog refresh independently verifies at
`derived-catalog-source-exports/dda362e2c5e647c9d9d5df49ef0d3ce523a9d2beecbe7c73ee1a14a6f2312fc3`.
All six normalized files (30,159,029 bytes) remain equal. The new hierarchy source
is `sha256:06a224b93915bd1d69ebabae9e9e1e8a8e92c79a08e841ba8798b4567d26b6b1`;
the theorem-link source is
`sha256:085a42e3845366a132242005e77588a63653ca57c13030c3d3c5b8ace11fb1cf`.
The new replacement OpenAlex plan changes only that exact parent identity; its
selector remains 2,052 arXiv IDs and nine exclusions. A fresh observation at
2026-09-09T00:02:22Z confirmed $0.10 free quota, with zero prepaid balance. The
credential-free replacement capture subsequently stopped at request 485 on arXiv
HTTP 429, after 484 retained requests. This was not OpenAlex quota exhaustion;
the reviewed retry policy does not retry that response. Its log and quota receipt
are in `openalex-d1-refresh-20260908/`, and its incomplete capture is
`openalex/captures-incomplete/3ab91cc14709a1533475fa286f18666ce504cf889b56fab21d272e59fe5f8ff0`.
A compliant new attempt is being prepared. The current full draft is still unchanged.

An exact declaration oracle and its Mathlib source revision were located through
the official [documentation build](https://github.com/leanprover-community/mathlib4_docs/actions/runs/34203087670).
GitHub artifact `10049778551` has ZIP digest
`2e1f4c6498da586d2da78da088198e79dd933b7df028bcad40f035d6efc8b17e` (117,468,491 bytes),
verified against the API artifact metadata. Its `declarations/declaration-data.bmp`
is JSON with digest `b68bb78132ed8de2f34f09fbd826ef3f5557fadc98cbf7bbb7858483f13325ac`
(67,374,686 bytes), exactly matching the existing `.claude` oracle cache.

The retained job log (`101986182527`) records Mathlib commit
`e861750b15eff0d5bc98279911ce457bb1a1382f` and doc-gen4 revision
`97d4ecdfc8e09e7f511724c25e303d448de6a3db`. The generated Topology/Basic page names
that same Mathlib commit in its source links. The source was fetched by exact commit
into a separate complete-content, shallow Git checkout; the bot checkout was untouched.
Its root tree is `3b2c28473acf9cfc3363551d91bfbc7d0ac1d2bb` and its Git archive is
110,888,960 bytes with SHA-256
`f03b69e44860cb5b91fb9166cdd54f3191a016bc467191e5345854ddf077e083`.

Private evidence locations:

- `/Users/jack/.local/share/wikilean-migration/mathlib-docs/34203087670/`:
  run/artifact API responses, job log, original ZIP, extracted oracle/page, and audit records.
- `/Users/jack/.local/share/wikilean-migration/mathlib-source/`:
  exact source checkout, Git archive, fetch log, and acquisition audit.

The original manually retained evidence above remains an audit record. A fresh,
instrumented seven-request acquisition subsequently succeeded, with real receipts,
at `/Users/jack/.local/share/wikilean-migration/mathlib-evidence-captures/0bc971fc1d1ce3f18bee753283aa77b19afc758742164545d8814f27a37fcb65`.
It reconstructs the exact 9,265-member Git tree, including 8,511 Mathlib Lean files,
and binds the documentation oracle's 424,321 declarations to that commit. The first
independently verified v3 source export is
`/Users/jack/.local/share/wikilean-migration/mathlib-source-exports/f5dfe62f5d67b6d532c1451b3064e599fe04b0e06a956c839e7e1aa793e94e1a`.
It is superseded for plan assembly by the independently verified v2 export
`/Users/jack/.local/share/wikilean-migration/mathlib-source-exports/0f599920ab77b1d4a62de7f09e68663ea4d95e6ebcffb80411943a760b62483e`.
The v2 normalizer binds a sealed complete reviewed tool-profile preimage. Its source
manifest is `sha256:720e1aac25d584afd848704e8c05f9ae99f473b2fd3eb2d6835e4e9eb77848d0`;
the docs manifest is `sha256:0912ea672c4f447c7717dfd4185c17e2319bd5a99170baa132fff6811bbd7f47`.
Keep both older evidence generations for audit; use the v2 export in the source plan.

Fresh Hugging Face acquisition and independent export verification also succeeded:

- Capture: `/Users/jack/.local/share/wikilean-migration/huggingface-evidence-captures/8356cd2318e29f5c4ac9bff91af076d17871437a08a86ee6523ef1629b5b1639`.
- Export: `/Users/jack/.local/share/wikilean-migration/huggingface-source-exports/bc180fa2ae4adc70215a0f1f90d2582c60078aaa9956ff6055f66568d01c741f`.
- Twelve actual public requests bind metadata, README files and six CSV files
  (3,102,401,461 CSV bytes) to the reviewed dataset revisions and LFS checksums.
  README Git blob hashes and publisher license declarations are retained.
- Source IDs: MathNetwork/MathlibGraph `sha256:81d936b07f3152e9fda275e031e1dca28fc95ea583770e9fbe3c6519e81a3ce3`;
  uw-math-ai/math-graph `sha256:98644aebd8499ca67335da9e3e6cab6672f11900bb2199ab067db91e9cac01d4`;
  uw-math-ai/theorem-matching `sha256:34cc31e950257b002ebb4ce205f417e948a0153973929e8b800eac015826dcfd`.
- All outputs remain private and restricted pending source-plan and redistribution
  review. The preserved acquisition profile is `sha256:98e923fea9daea19c85226b6da3df86fc5f3012a62cd7bc8ed7c359aeed82ac2`.

The first real Wikidata observation failed because WDQS appended a timeout stack trace
inside an HTTP 200 JSON response. Plan v2 uses smaller edge batches and applies the
complete pinned target scope before label lookup, preserving normalized semantics.
The optimized probe succeeded. A subsequent complete attempt failed at request 45
with an HTTP 502. Bounded private diagnostics are retained under
`wikidata-observations/failed-attempt-2770219067f94c8abbdf66c5b4629d16` beneath the same
private migration root. No failed attempt published an authority bundle or installed
partial data. Another complete attempt stopped at request 24 with an unclassified transport failure.
The transport now retains only safe numeric curl status and bounded Retry-After
diagnostics. The failed query succeeded when probed again; a larger batch was slower
and approached the upstream timeout. The next complete 194-request attempt failed
at request 69 with curl status 22/HTTP 502 and no Retry-After. Its diagnostics are
in `wikidata-observations/failed-attempt-ff538d414ceb4875a09eab8777487186`.
The explicitly versioned retry mode is implemented: every attempt and failed
response is retained, receipt v2 counts actual attempts, and each planned request
must end in exactly one success. The existing no-retry evidence stays verifiable.
The first explicit-retry capture ran under profile
`sha256:e69a4fdd13c937439ba781afdec6c204f99f55387b0d361a425e3287ca0158a4`.
Its log is `wikidata-plan-20260908/acquisition-explicit-retries.log`. It recovered
from two HTTP 502s and completed all 194 requests, then failed the reviewed class
floor for Q21550639. A fresh diagnostic query returned 25 distinct QIDs versus
the previous 26, with exactly Q44946 (point) removed. The independently verified
entity capture records Q44946 revision `2536250970`, modified August 25, whose
P31 statements no longer include Q21550639. The prospective floor was reviewed
from 26 to 25; all other floors, selectors and request parameters remain unchanged.
The review is `wikidata-plan-20260908/q21550639-floor-review.json`; it grants no
production semantic approval. A fresh complete acquisition is running with
`request-plan-v2-reviewed-class-drift.json` (SHA-256
`4f14434d1cdaefa0404cb859d47d78161ec09c90ac99e47d146a521a5762aab3`),
tool profile
`sha256:da17f29e23a2f883e28cedcb92f919ced455edc1637a8c58a0f8ffa3c8545939`.
The first attempt under this plan exhausted five DNS failures at request 129;
its single-request diagnostic is retained under
`wikidata-observations/failed-attempt-3d4e06e1d8e749548bb7b212df7f650c/`.
DNS resolution subsequently recovered. The fresh attempt under the same
plan/profile succeeded; its log is `acquisition-reviewed-class-drift-retry2.log`.
Independent complete bundle/source verification passed for
`wikidata-observations/b8f9a745812f3cc60c41680b9d5ec99a51a2c765c27a47c171eb4e204f1dbf35`.
It contains 194 successful requests and four explicitly retained failed attempts
(198 attempts total). Source ID:
`sha256:36be342172d6c1bf3c896aeba7ab19a6794b8d0190e382111ab4429b7a07f5e6`;
receipt `sha256:69e0ab6665c3eeeddae7e2af19c3434a8ab15e375b1830f383cbf2d2a44bae8f`;
lineage `sha256:f665b695284eb247ce84b69beaad596abcd7571213c5d1331ec92074c4d55434`.
The verified fragment, manifest and report are in `wikidata-plan-20260908/`.
The producer now preserves complete rejected transcripts as private diagnostics;
these have no receipt, normalized output, authority bundle or resume path.

A separate complete entity capture succeeded for the 3,069 QIDs in the reviewed
observation edge/description union (covering all 2,500 grounding QIDs):
`/Users/jack/.local/share/wikilean-migration/wikidata-entity-captures/4ba24db76b9aa37c49e35cbbdd160f01a3e108c2ab8a2e208d241550e87701d6`.
The crossref derivation and independent export verification succeeded:
`/Users/jack/.local/share/wikilean-migration/wikidata-crossref-source-exports/426b0e2025d9717a78b9e82701d51ce9efc1dd11c1096acf0c6fda0ae3f38d88`.
It covers all 3,069 requested QIDs, with 3,031 containing concrete truthy external
identifiers. It retains the original acquisition evidence and adds an explicit
claims normalization plus the committed source-registry parent. Source IDs are:

- Entity claims: `sha256:2e045d2ee23b90ee359efece1efa09887bd8030c700921a14da8a6113a183594`.
- Registry: `sha256:2e60086743c4f7e904cae1eb5a691a0478e585a31531a07efbda142d418f6550`.
- Crossrefs: `sha256:c7d301b5ab8783cd68b7ce4c87cecd572b9addd5022e29d6a17ed643d7db6f2d`.

Fresh public Git source captures and independently reconstructed exports also
succeeded for Formal Conjectures, ErdosProblems and TauCeti, using six actual
requests and exact complete Git trees. Their paths and identities are retained in
`/Users/jack/.local/share/wikilean-migration/public-git/verified-source-exports.json`.
Pure harvester normalization and second-process verification subsequently passed:
`public-git/harvest-normalization/exports/7ced5a02f6778d485b6c55193127e0e1060df24be8f274162597cddfa4bfb0ad`.
The 4,190 Formal Conjectures rows, 7,860 TauCeti rows and 1,217 Erdos joins/pages
match every existing data row. Erdos gains a deterministic pair envelope and an
explicit empty links companion. `public-git/harvest-normalization/verified-export.json`
records child IDs, parent roots, exact PyYAML 6.0.3 dependency evidence and row parity.
The source-only YAML loader rejects preloaded packages and bypasses unmeasured bytecode.

Fresh nLab and Stacks Git captures and second-process export verification also
succeeded. The summary is `public-git/verified-nlab-stacks-source-exports.json`:
nLab commit `155c084fedf98b24d14ff8883d692140a6f0a942` has 41,428 files;
Stacks commit `c4fe5c4a3db63dab0f8c7b65f828662ef952ab2a` has 154 files.
Their source IDs are respectively
`sha256:d8cfe64994d5325bd49a33f02fc295b329468ee80fa7b530c57ea982f806dd7d`
and `sha256:a1988cb44467ba2aa060e572cf6d9b18e49a3db1a08ea53a4be448ec74623cab`.
The Git acquisition profile now explicitly binds allowed repositories; historical
profiles remain restricted to their original three repositories.
Both pure catalog reductions also passed independent verification:
`external-git-source-exports/cd0b2c1d11af82264e136e83b9a34458119d7d92b04a177b4264b2c1f8235a4d`.
nLab produces 20,057 pages/438,492 links; Stacks produces 21,436 pages/47,356 links.
`verified-external-git-export-20260908.json` records complete source IDs and root bindings.
The shared `external_pair_normalization.py` retains the reviewed legacy writer's
pure row/metadata semantics without executing its filesystem publication block.

The independently verified identifier export is
`derived-identifier-source-exports/0035986f00ab36f6519a044dcb60f2c410b8f4171aa71284e4a341c5acc4d18f`.
It contains 710 Mathlib tag rows from the exact source/oracle pair and 1,738
MathWorld identifiers from verified P2812 claims. No MathWorld sitemap was acquired;
the empty link pair and explicit inventory metadata retain that limitation.
The source IDs are `sha256:3e679da797cb7f6f544623258a4ba34dd5b1f842c81cb01b324b51b8c67551e1`
and `sha256:eea3d8d9e5867143d9c125f4f922c8f957b582c353e663de2fa44b6cb8ce7a6b`.
These exports are private and carry no production semantic approval.

The derived catalog normalizer covers concept layer, concept graph/declaration
bindings, hierarchy and theorem links. Its 22 evidence/compiler tests and five
legacy semantic parity tests pass. The original draft was finalized with the
verified shared Wikidata observation: `derived-catalog-plan-20260908/plan.json`,
SHA-256 `ebb2b4c5daca2a25aa38343939901aef398970a267155397d545a32506832d91`.
The actual eleven-parent reduction and separate-process verification succeeded at
`derived-catalog-source-exports/98ab7bb4df33fc7ca30f13e55df3a85a15d7c57b2be3e6a07b99c91cd56a9dda`.
The exact six output bindings and four child manifests are in its source fragment;
the complete verification report is `derived-catalog-plan-20260908/verification.json`.
Its current profile is
`sha256:98623f9f769343f566693da9d4371111a9ae0b78fcb878f805c630b4e981e0b8`.
Curated source objects use native Git paths. Final logical input assembly belongs
in a separate Git-backed staging tree, never the original dirty checkout.

The private `full-corpus-plan-20260908/control/source-plan-draft.json` now has
106 manifests and 41 of 43 complete input groups after PlanetMath and OpenAlex
integration. Ten external pairs are staged; only the aggregate external page/link
groups remain incomplete pending Kerodon. Its current SHA-256 is
`4a0e4fb7fa9cbe3c9df97bf7c419d181d64f3dbfe3cf2c97526452cd593ce9ce`. Its detached
checkout is at `c64438d2`; verified source members are copied into logical input
locations with checksums and materialization records. Kerodon remains necessary
before either aggregate external input can be complete. Previously present source
families remain required by the completeness review. The draft is explicitly
non-authoritative and is not a full plan or compiled pack; inspect the latest
materialization records for subsequent assembly changes. Proposal-fold source
coherence, source-policy review and full evidence/compiler checks remain outstanding.

ProofWiki's fresh single-GET compressed dump is 36,266,006 bytes, SHA-256
`48c9ba98542597a32d2c4e7cddc670170a6975c404a25b9bb4f96a28bf4f7979`.
Its raw export is `public-files/exports/834530ee2b1359da2b154ceda45e7050b09a688b55699058c68d2cb41ef12e90`;
the complete pure XML reduction and independent replay are
`proofwiki-normalization/exports/a79f80855eaa72aada3dc746995ea98ce167695e4c5a70d88640c62b5dbdb8d8`.
It yields 49,331 pages and 342,161 links, with 540 QID joins. Its explicit four-parent
ancestry includes the verified dump, requested entity claims, crossrefs and Git registry.
The XML stream independently enforces the 4 GiB expansion cap. Fresh upstream
content differs from the old dump; source drift is not a migration parity approval.

EOM's complete fresh 95-request API walk and separate-process export verification
passed: `eom/exports/50d67ba95bd75644891c7608309317c575107a568c737a70e8e14010492f821b`.
Source ID `sha256:114b5813d750ca72e95a39f210f889ea8f19bebff2723c967d49abc8dfbd6a6f`;
4,639,593 original response bytes yield 9,985 page identities and 42,846 links.
Exact continuation replay, logical-title collision rejection, incremental resource
bounds, complete request/body evidence and private failure retention are enforced.
The pure normalization and independent replay also completed:
`eom-normalized-exports/4dfb6326e3447f0446e2b94e689f809eb9b7b17df0341a70a2bcf25682958da9`.
Its child source is `sha256:1d8213ee6f119e54a0d5d6bffc659180ab15bc0031884c9004a92fa7c9795185`.
All 9,985 existing page rows and 42,846 link rows match exactly; 473 pages have QIDs.
Both logical members are staged, with the original API, claims and Git registry
ancestry retained. No article text is requested.

OEIS acquisition, reduction and separate-process verification completed:
`oeis-source/exports/928606537a0d43a920e9d6c13926094fb5087035a7972cd08b7e5b0acf0c2bd2`.
All 99 actual GETs succeeded, retaining 9,394,637 response bytes and the full
399,443-name inventory. The exact 98 current P829 anchors produce 98 pages and
115 links. Every legacy page row (37) matches, and all 22 legacy links remain;
61 new anchors reflect source refresh. The raw source is
`sha256:8cf6f236ae0bacc5cef20d8823a59b6fd2db28ca0860b6de6729d05ee0d9d4cc`;
the normalized source is `sha256:10f7cef4d1e9023637229641efcf9e6d4878758fd593be5d50e12e1cb4bb77a1`.
The complete fragment and integration report are in `oeis-source/`; both members
are staged. This is source evidence, not full-graph semantic approval.

LMFDB's raw source capture and independent export verification completed:
`lmfdb-source/exports/40697ecf7a0799698f3ad13861d4c7b6d829c5ebd71f5599f9a2f68556a547ce`.
Its source ID is `sha256:2b8f90c21074abf4352210ece01041a4e6fa69e57cdf59740c5c5369cd6983d3`.
One read-only repeatable-read transaction on the documented public mirror retained
1,725 latest eligible knowls in 1,651,084 original response bytes and rolled back.
PostgreSQL reported snapshot `66149792:66149792:`. The mirror presents a self-signed
certificate: the retained DER SHA-256
`5547fae90ed24ba86dfce1c9c48bd5218a84e7789e8be9d0e177d79ac5bfad12`
is checked before authentication; no public-CA authentication is claimed and system
trust was not changed. Source-policy review must retain this explicit limitation.
The importer seals all 98 installed files of its five pinned pure-Python driver
packages, executes captured source bytes instead of bytecode caches, and binds the
actual CPython runtime in profile
`sha256:48c6b118dc336d1b77bc87d78aa4657065e83a52f7ddb44f051ff65e9e111cfe`.
Network message allocation, query size, latest-revision ambiguity and schema are
bounded; query/cleanup failures retain received data without publishing authority.
Pure normalization and independent legacy-code parity also passed: all 1,725
pages and 5,235 links match the legacy normalizer on the same fresh responses.
The export is `lmfdb-normalization/exports/4e21920329f9c742dac51b124fa49459464bd62eaa9428b848edff2e306738f9`,
child source `sha256:d40430d389d707043a454241afe0ab207455110a8ccd6a487a98dff8fbb92b51`.
There are 42 QID joins and 1,694 snippets. Historical-cache comparison separately
records eight added pages, 11 changed content rows, 12 changed QID joins, 21 added
links and two removed links. Both normalized members are staged. Publisher code
at LMFDB commit `fea1c3dffe05b3ac2df3d5953885db035d9096b4` explicitly states that
the data use CC-BY-SA-4.0; the verified template and Git blob identity are retained
under `source-policy-review/lmfdb/`. The old registry's unverified-license note
is therefore stale, but its pinned bytes remain intact pending source-policy review.

DLMF completed 909 requests with 103,921,199 response bytes. The independent raw
and normalized replays produce 872 pages and 31,540 links, including 15 QID joins;
all rows equal the legacy normalizer on the same responses. Normalized export:
`dlmf-normalization/exports/45891255636701f95e75d170fe2aad1b4d38f3be502032bd56e70dfbd34e7e4f`,
child source `sha256:7ddc2bf7bf04d108a02d6d8d4e0db8418d09c5d4734657ddfc74281a06d6ef2a`.
The raw source is `sha256:9ddb2900d0eb25ab86ef9a7dd1fce5e86eb4f0a1da820e78398d091ea74f872a`.
Both normalized members are staged.

PlanetMath completed all 63 repositories and 127 requests. The independently
verified export is
`planetmath-source/exports/be139e543da0f4f78cb8030aae7dbcc5d8fda1bc19bd06dc9fd6838cdb52b2e7`,
child source `sha256:f82666ce0cc45b9acfe31545748c5171a39ff505cceddaf52349acc9e424fef8`.
It retains 26,565 files and 5,604,844,007 bytes of evidence. All 9,451 page content
rows and 12,805 links match the historical cache; 40 added Wikidata joins raise
coverage to 297. The initial archive-bound failure and its measured limit review
are retained. The successful generation keeps its exact larger resource policy;
the failed generation is never relabeled as successful.

OpenAlex and arXiv completed 914 requests, including 452 filtered calls, with no
retries and 37,731,295 response bytes. All 2,052 selected arXiv IDs returned identified
Atom entries. The normalized export is
`openalex-normalized-exports/82146a6bd9cb1e3bf642353b67fbc5d0e49f97efbeb7be71d96ecd1412911a7c`,
child source `sha256:550466341eb80fc696187bf023157af59fae6724f0f407a6b5bfcee18e748c39`.
All 25 normalized citation rows match the legacy main on the same captured responses.
Historical drift is five added links and one removed link; the removed link's
source paper is absent from the reviewed current theoremgraph selector. The exact
selector and full seven-parent ancestry remain bound, rather than promoting a cache.
Private reports are in `openalex-plan-20260908/`.

Kerodon is the remaining live source capture. Its reviewed raw and pure profiles
remain frozen while it reads both roots and all 7,509 tags at a polite request
rate. The old cache contained only 6,490 successful contents, so its incomplete
legacy coverage must be called out in the later semantic review. The private log
is `kerodon-plan-20260908/acquisition.log`; incomplete captures cannot supply bindings.

## Curated contribution and assertion checks

The proposal diagnostic replays all 452 pinned proposal files with an exactly
empty unknown-entity request plan. Replaying from an empty FC seed produces the
same 2,100 rows as the old seeded run, removing an opaque prior-output dependency.
The separate [curation decision](BRAIN-SQLITE-CURATION-DECISION.md) explains five
direct Git container contributions that must be preserved separately, and 15 FC
links whose completed declaration names match recorded rejections. No diagnostic
fold output has replaced the currently bound runtime input. This is an explicit
graph-change review, not a provenance-only parity claim.

The sealed proposal adapter and exporter now reproduce the complete old fold from
452 native Git proposal shards, exact source/oracle inputs and five separately
retained direct Git container contributions. The first reviewed export is retained
at `proposal-fold-source-exports/7033aafe895da26eb27051cb93e97e6726a1d0702f15794f7596e5d5a231e99c`.
The D1-derived hierarchy refresh independently verifies at
`proposal-fold-source-exports/5cd6b73e2c4448e53bfa20f89571974657309218b16a3b41e8fdfa181299bc89`;
only parent identities and their contribution audit change. Graph, decision and
comparison bytes remain equal across those two exports. The 104 composed container rows remain
equal; all 326 discovery identities remain, with two module fields corrected
from `Mathlib.Data.Finite.Defs` to `Mathlib.Basic.Finite.Defs`. The 15 FC retractions remain an
explicit content-change decision. This export is retained, not installed.

The separately versioned `brain/authority/reducer-inputs-v3-folded.json` changes
only the three container/discovery/FC input classes and their purpose text to
describe sealed fold outputs. Its identity is
`sha256:675e97745422fdd1ae7930eead2fb83f1d012236b452c08d84a368256ca2672c`.
The reducer accepts those classes only for that exact inventory ID. Historical
inventories and contexts retain their original checks. All 23 focused inventory
and base-context tests pass independently. Installing the new inventory still
requires a new committed reducer generation and the complete coherent source
replacement; no source plan, graph-change approval or runtime binding was changed.

Standalone private-replay and public-release policy review tooling is implemented
in [`standalone-policy-reviews-v1.md`](../brain/authority/specs/standalone-policy-reviews-v1.md).
It binds exact pack/source/object closure separately from exact public artifact
coverage, keeps original source restrictions, and drafts pending records only.
No policy approval has been issued. The separate private replay gate now binds
an exact private policy review while leaving the existing authoritative replay
and promoter profiles unchanged. Retained publisher evidence and field-level
decisions still need review, including mixed-license theorem-matching data and
external text snippets. Provenance coverage needs source/method/context mappings;
a source label alone does not identify an acquisition family.

The standalone provenance checker and new private replay gate pass 42 combined
focused checks independently (18 checker, 24 gate). The checker verifies the
complete frozen release, exact source families, required joins, source-member
witnesses, policy references and provenance indices. Unknown kinds and unresolved
mappings fail. The gate seals the complete loaded implementation closure and
rechecks the exact mapping and private policy before final qualification. Neither
tool grants publication rights, accepted authority or production activation. No
full release mapping or private approval has been completed; these are verified
tools and fixture integration, not a full-corpus qualification result.

## Same-input legacy baseline preparation

The private `legacy-baseline-20260908/` holds an unchanged detached `ebac34dc`
checkout, all ten old reducer/helper files and a proposed seven-stage execution
recipe. Its current input map is provisional and must be rebuilt from the final
verified pack. No baseline graph run or approval has occurred. The new bounded
`legacy_halo_projection.py` executes five exact hash-pinned old pure functions on
the baseline's own cells/synapses. It emits full original rows with empty
centrality; the old frontier consumes only cell IDs and `all_frac`, which do not
depend on centrality. Historical halo data is retained for a separate diagnostic.
The new `legacy_sqlite_projection.py` preserves the original schema-1 SQLite and
all seven exact expected semantic files in a separate private directory. It builds
a schema-2 index from the five indexed files and runs the independent complete
SQLite row/column/owner verifier; frontier files remain exact pass-through bytes.
Its canonical expected-ID plan pins inputs and the four-file loaded implementation
closure. The completion record explicitly grants no legacy execution, static
release or baseline approval. All nine focused checks pass independently, including
the real retained `ebac34dc` schema-1 writer, changed index columns, a modified
retained database whose historical snapshot ID still matches, and the isolated CLI.
The original writer predates SQLite's current WLBN application marker: its exact
application ID is zero, while the new index must pass the current schema-2 checks.
The new `prepare_legacy_baseline.py` copies only fully verified pack inputs and
the exact ten old programs from native Git objects into a fresh private layout.
It checks declared absences against program and input paths, retains mixed
`brain/data` inputs for read-only overlays, and excludes old caches. Preparation
records explicitly say execution has not occurred.

The new `assemble_legacy_release.py` verifies the producer-owned seven-stage
execution record, all ten old programs, exact inputs, fresh halo, SQLite projection,
and sealed provenance. It preserves the old database and comparison evidence
outside the frozen release. All 12 tests pass independently. A separate synthetic
probe using the exact old static builders passed the unchanged full release verifier
with 20 artifacts; this is format compatibility evidence, not the full baseline.
The composite release identifies its current assembler/freezer generation and
retains the exact old graph code identity in its configuration evidence.
The native runtime can be reused, but the old-program launch needs its own honest
execution evidence; it is not the new authoritative replay profile.

P2A's experimental shadow kernel implements four assertion operations, independent
contribution identity, exact revision/retraction history, predecessor checks,
derived conflicts and full/incremental replay. Its fixture envelope and kind policy
are separate from accepted changesets; see
[`experimental-assertions-v1.md`](../brain/authority/specs/experimental-assertions-v1.md).
The actual Git pilot at `c64438d2` preserves all 104 container and 326 discovery
contributions with identical canonical per-file roots. Evidence is retained in
`assertion-shadow-20260908/`; source files, runtime routes and
`authority.through_changeset` are unchanged. Full graph parity, accepted genesis
and production authority remain later milestones.

## Engineering changes in progress

- Inventory v3 explicitly binds Wikidata universe, edges, and descriptions to one
  acquired source manifest. Preflight, compilation, and independent pack verification
  enforce this. Community-edge input becomes an immutable D1-derived source object.
  V2 inventories retain their existing acceptance and identities.
- One sealed Wikidata observation replaces separate live publication jobs. Its
  observation policy is `independent-live-requests/no-snapshot`; it makes no upstream
  transaction claim. The reviewed plan binds five selector inputs and exact count floors.
  Nightly requires an explicit plan and verifies the complete installed generation.
- The immutable D1 source exporter derives annotations solely from verified rows,
  preserving the original capture evidence. Nonempty community graduation requires
  additional explicit source dependencies; the zero-row capture must not justify a
  general empty-output fallback.
- The Mac pack publisher temporarily uses a non-searchable mode `0600` root for
  exclusive rename, reseals via its open descriptor, and rechecks full closure.
  Caught failures clean only the known candidate inode. A killed publisher may leave
  an inaccessible candidate that must be rejected rather than adopted.
- V3 source manifests can preserve distinct named outputs sharing identical CAS bytes;
  aliased digest, size, and media type must agree. The compiler's v3 compatibility
  shape validator now also accepts these aliases; legacy v1 plans/v2 manifests
  remain strict. A real compiled-pack regression verifies both boundaries.
- Mathlib tag harvesting reads one immutable Git tree, emits logical paths and exact
  source/oracle hashes, and explicitly refuses to imply revision coherence from an
  unbound cache. Rename metadata normalization preserves historical reviewed claims.
- The trusted OCI launcher, image/wheel checks, numerical policy, and packaging tools
  are implemented for native Linux. Runtime v2 describes builtin `_sqlite3` linkage
  explicitly while retaining v1's extension-file contract. A real native Linux
  container kernel probe now passes under an exact scoped AppArmor policy. This is
  final-image evidence under committed runner `c64438d2`.
- Pack-bound release production and independent verification now have a distinct
  `brain-offline-replay-v1` profile. The producer-owned two-build gate freezes all
  completed outputs plus exact sealed provenance inputs, compares bytes/identities,
  and requires an explicitly reviewed session before reproducibility attestation.
  Actual full-corpus execution is still outstanding.

## Verification environment

Use Node 22 and CPython 3.12. This Mac has an isolated development environment:

```bash
export PATH=/Users/jack/Desktop/LEAN/WikiLean-migration/.venv/bin:/Users/jack/.cache/wikilean-migration-runtime/node_modules/node/bin:/usr/bin:/bin:/opt/homebrew/bin
cd /Users/jack/Desktop/LEAN/WikiLean-migration
PYTHON=.venv/bin/python3 ./scripts/ci-python.sh
cd wiki && npm run test:ci
```

The `/usr/bin/git` ordering matters: the compiler deliberately rejects a symlinked
Git executable. The development venv includes the CI requirements and NumPy 2.3.3;
this install is not a pinned authoritative OCI dependency closure. This machine is
Darwin arm64. An isolated Linux guest has been prepared using Lima 2.2.0 and Apple's
Virtualization framework: instance `wl`, four CPUs, 8 GiB memory, 40 GiB sparse disk,
Ubuntu 24.04 arm64 in Lima plain mode, no host directory mounts or dynamic port forwarding. Its
private configuration, verified Lima archive and logs are in
`/Users/jack/.local/share/wikilean-migration/linux-runtime/`. `LIMA_HOME` for this
instance is that directory's `lima-state/`. The authoritative launcher must execute
inside the Linux guest and still prove actual container and kernel isolation.

Worker typechecking and all 872 tests passed after the new release-profile and
attestation-reference checks. Pack/compiler/preflight/publication
focused checks passed 66 tests before subsequent integration; authority contracts
passed 75 tests after the content-alias correction. Strict Darwin kernel sandbox
probing passed without skipping. The complete 55-command Python suite passed after
the new source evidence, release profile, explicit retry, and Linux policy integration;
its log is `/tmp/wikilean-migration-python-ci-native-policy.log`. The latest focused
Wikidata observation suite passes 48 cases; authority contracts pass 76. Native Linux
kernel and runtime diagnostic logs are retained in the guest's `runtime/evidence/`.
Two fresh sandboxed runtime probes under the final committed image agree on
CPython 3.12.14, NumPy 2.3.3, SQLite 3.40.1 and bubblewrap 0.8.0. The actual final
image passed the strict Linux kernel isolation test and exact AppArmor policy
verification. The sealed descriptor is
`linux-runtime/execution-environment-c64438d2.json`, environment ID
`sha256:30cf669d261f3fcccf5ba1200c2a27f6727dc342c6d442b137d6da3a589da8b0`,
OCI manifest `sha256:4813bc6a62e0e5e3537e749a24fd194e18506ec68104b70c269cfe17d6b7bafd`.
The OCI archive and preparation diagnostics are retained alongside it. These
prove runtime preparation, not a full-corpus replay or its launch receipt.
The expanded 59-command Python suite passed, log
`/tmp/wikilean-migration-python-ci-source-fragments.log`; focused counts include
Wikidata 49, compiler 31, crossref 28 and public Git 10 tests. The unchanged Worker
remains at the previously passing typecheck and 872 tests.
The subsequent 60-command Python suite passed in
`/tmp/wikilean-migration-python-ci-identifiers.log`, including all 13 identifier
tests and the expanded 12 public Git tests. CI now pins PyYAML 6.0.3 for the
upcoming pure Erdos importer; source normalization separately retains its complete
installed dependency preimages and interpreter identity.
The expanded 65-command suite subsequently passed in
`/tmp/wikilean-migration-python-ci-external-sources.log`: Git harvest 16,
external Git 19, public-file 13, ProofWiki 11 and EOM 13 focused checks, including
actual pack compiler integration and independent legacy-parity coverage.
The preceding complete suite passes **72 commands**, log
`/tmp/wikilean-migration-python-ci-remaining-catalogs.log`. New focused checks:
EOM normalization 12, DLMF raw 12 and normalization 12, OEIS 14, Kerodon raw 18
and normalization 12, LMFDB raw 16. All new source slices received independent
review; the LMFDB cleanup finding was fixed and regressed before live acquisition.
The unchanged Worker still has its previously passing typecheck and 872 tests.
The latest complete suite passes **77 commands**, log
`/tmp/wikilean-migration-python-ci-shadow-sources.log`. New focused suites pass
LMFDB normalization 12, PlanetMath 16, OpenAlex raw 18 and normalization 10,
and experimental assertion shadow 17 tests. Independent review confirmed actual
430-row Git pilot parity and exposed two repaired shadow issues: discovery rows
use `src`/`dst`/`kind`, and checkpoint comparisons must distinguish booleans from
integers using exact canonical bytes. Golden v1 identities and full/incremental
replay remain equal. This completes the bounded P2A shadow criteria only.
The next complete suite passes **81 commands**, log
`/tmp/wikilean-migration-python-ci-fold-policy-d1.log`. It adds 17 sealed fold,
17 standalone policy and three legacy halo checks, plus the expanded 22 D1
export checks. The real v3 compiler regression rejects the historical empty MIME
conflict and accepts all five refreshed D1 aliases beside the unchanged 255 Git
aliases. Both old and new real D1 exports independently reproduce. The unchanged
Worker retains its preceding passing typecheck and 872 tests.
The expanded complete suite passes **86 commands**, log
`/tmp/wikilean-migration-python-ci-private-legacy.log`. It includes SQLite compatibility
projection, verified old input preparation, legacy release assembly, provenance
coverage and private qualification. Focused cases pass: projector nine, input
preparer six, assembler 12, coverage 18 and private gate 24. Independent review
also verified the 23 folded-inventory/base-context cases and found the repaired
special-file omission in the preparer's final closure checks. The unchanged Worker
retains its preceding passing typecheck and 872 tests.
Current focused release tests pass 37 cases. Strict
Darwin evidence is retained in the private migration root's
`darwin-kernel-probe-20260908.log` and is diagnostic, not OCI evidence.

## Remaining completion order

1. Finish Kerodon and independently verify its normalized export. Complete the
   replacement OpenAlex capture after resolving arXiv throttling; D1's catalog/fold
   refresh is verified.
   Preserve every earlier evidence generation. The final
   committed native Linux runner image is verified; preserve its preparation evidence.
2. Seal D1, the revision-bound oracle/Mathlib source, Hugging Face objects, shared
   Wikidata observation, proposal-fold inputs, and other source families into the
   reviewed current-corpus v3 plan. Close policy and cross-object provenance gaps.
3. Assemble the logical inputs in the private detached staging checkout at
   `full-corpus-plan-20260908/repo` (currently `c64438d2`), update only its reducer
   generation for the exact folded inventory, preserve original curated Git pins,
   and bind the sealed native Linux runtime identity. No valid full source plan
   has been emitted yet.
4. Compile the real pack, run two isolated builds in different paths with adversarial
   mtimes/environment, verify complete byte/identity equality, and compare the approved
   semantic baseline with explicit provenance migration review. No approved complete
   pre-refactor baseline has been identified yet. Prepare a legacy-code comparison
   on the same verified inputs so fresh-source drift is not confused with migration
   behavior; retain an explicit review boundary before asserting full parity.
5. Emit a separate reproducibility attestation and bind the verified pack/source-set
   identities into the full-offline-replay release attestation.
6. Finish the P1B public baseline/shadow/dry-run/activation evidence bundle. P1C still
   requires Jack's exact-release and window approvals for A, B, rollback, and final state.
7. After the experimental P2A shadow, complete the later accepted Git assertion
   authority, release-pinned D1 overlay,
   and generated-artifact retirement stages. No schema or authority cutover is implied
   by completing the source-acquisition tools.
