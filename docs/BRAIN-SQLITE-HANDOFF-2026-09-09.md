# Brain SQLite handoff — 2026-09-09 00:22 UTC

> **Historical checkpoint only.** Use
> [the 2026-09-19 handoff](BRAIN-SQLITE-HANDOFF-2026-09-19.md) for current state.
> The PID, private paths, quota date, and live-capture instructions below describe the old
> laptop and must not be treated as current operator instructions.

At this checkpoint, Jack requested an immediate handoff and push because the session
was running out of tokens. Further engineering, probes and new captures were stopped.
`ROADMAP.md` remains the completion contract; the detailed acquisition history is in
[`BRAIN-SQLITE-CONTINUATION-2026-09-08.md`](BRAIN-SQLITE-CONTINUATION-2026-09-08.md).

## Checkouts and authorization

- Worktree: `/Users/jack/Desktop/LEAN/WikiLean-migration`, branch
  `codex/brain-architecture-phase1`, remote `Deicyde/WikiLean`.
- Jack explicitly authorized pushing this work. No deployment, production D1 write,
  accepted-authority cutover, or production activation was authorized or performed.
- Original checkout `/Users/jack/Desktop/LEAN/WikiLean` includes fetched main
  `ebac34dc`; another task added `f3e44926` (FC research documents). Its 670 existing
  data/cache changes remain untouched. Backup branch:
  `codex/local-data-backup-20260908-132955`, commit `ebcb58c7`.
  Do not reset, clean, or reseed from that tree.
- Private data/evidence root: `/Users/jack/.local/share/wikilean-migration`.
  These captures are intentionally not in Git and remain restricted.

## What is committed

- `1bd15ac3`: sealed proposal folds, D1 export compatibility, standalone policy
  reviews, exact old halo preparation; complete 81-command Python suite passed.
- `945f9c81`: byte-preserving old SQLite-to-schema-2 comparison projector; nine tests.
- `69cfb173`: exact folded inventory transition, legacy input preparation/release
  assembly, provenance coverage and separate private replay qualification gate.
  Complete **86-command Python suite passed** at this commit.
- `3ee4bb54`: reviewed OpenAlex/arXiv v2 capture policy; **23 raw + 11 normalization
  tests passed independently**. Old completed exports still reproduce unchanged.
- `952eac23`: **WIP native legacy execution driver** and optional assembler runtime
  support-file retention. Driver 12 tests and assembler 14 tests passed. Two actual
  native fixture probes passed. **Independent driver review is still outstanding.**
  At this historical checkpoint the new driver test was not yet registered in the required
  CI script; commit `8928d7c7` registered it afterward.

The complete suite was not rerun after the final two commits; their relevant
focused suites passed. The Worker is unchanged and retains its earlier passing
typecheck and **872 tests across 37 files**. No full-corpus pack, legacy graph run,
two-build result, baseline approval, full attestation or production activation exists.

## First: preserve the live Kerodon capture

At handoff it was at **5,730 / 7,511 requests**, all successful on attempt one.

- Process **PID 11762**, parent PID 911 (Codex host), tool session **97232**, no TTY.
- Log: `kerodon-plan-20260908/acquisition.log` under the private root.
- Leave Codex running. This process survives the agent turn, but **is not durable
  across app/host shutdown and has no checkpoint/resume API**. Approximately 45
  minutes remained at handoff; inspect the process/log before doing anything.
- If shutdown is necessary, SIGINT reaches the reviewed exception-retention path
  and writes an incomplete capture. SIGKILL/app shutdown can lose the in-memory
  transcript. No signal was sent.
- On completion, verify/export raw evidence, normalize, and independently compare
  with the old normalizer on the same captured responses. The actual parity harness
  is `/tmp/wikilean-kerodon-actual-parity.py`. Then assemble its paired external inputs.

Keep the live capture's complete program closure frozen through export/verification:
`authority_contracts.py` SHA `225d2eb930e1b21e3a2681a7f93a1d69e628566c4ff4b84fd78cd72ae829d160`,
`execution_environment.py` SHA `7b89aafb4f7c9e88b54cc591a55c93e16ebdbf6a57712e95ded388322873ccac`,
`stage_io.py` SHA `9b659899ce6c62709ac75b8bec2b9d83cd8550281e5d0ca2122ea6a8a805e4cf`,
plus Mathlib/source-plan helpers and the Kerodon core/CLI/legacy ingester.

## Immediate source blockers

1. **OpenAlex replacement is incomplete, and quota is insufficient today.**
   Its first refreshed-parent run stopped on arXiv HTTP 429 at request 485, retaining
   484 earlier requests in
   `openalex/captures-incomplete/3ab91cc14709a1533475fa286f18666ce504cf889b56fab21d272e59fe5f8ff0`.
   The actual last charged OpenAlex header says **$0.0521 remains**. Direct DOI
   requests also cost money; `filtered_attempts` is not a complete cost counter.
   A whole same-scope run previously needed about $0.09. Wait for the next reset
   (**2026-09-10 00:00 UTC**) and verify adequate free quota before a fresh run.
   No paid/key/account path is authorized. The planned arXiv recovery probe was
   **not run**, and no replacement capture is currently running.
2. The reviewed v2 request plan is
   `openalex-d1-refresh-20260908/request-plan-v2.json`, SHA
   `dbec92d3015f643963898a1f7d4ffbbb9d353ca8db98471ca99021e55428bf0e`.
   It retains 2,052 arXiv IDs, nine exclusions and theoremgraph parent `085a42e3…`.
   Raw profile: `sha256:5ad2557528e7e979939b894d3849010607204416ff5804028a3e2f94217afb28`.
   Pure profile: `sha256:4ad20c2163256d1e0719cf22e5b7ff26aeed736cc8b6fdfe6f766ffaad763d`.
   V2 uses 10-second arXiv spacing and up to five arXiv-only 429 attempts with
   60/120/240/480-second backoff and bounded Retry-After. Old v1 remains unchanged.
   Every failed response is retained; do not splice or relabel the incomplete run.
3. **New proposal/fold empty-object MIME conflict discovered during wrap-up.**
   The read-only preview at `stable-closure-preview-20260909/report.json` checks
   110 stable sources, excluding both pending OpenAlex entries and Kerodon. It found
   **260 zero-byte octet-stream aliases versus 32 zero-byte NDJSON aliases** in the
   proposed proposal-curation/fold closure. Parent identities resolve, but the final
   `assert not conflicts` deliberately fails. This is a declaration/parent audit,
   not full pack verification. Fix the proposal/fold exporter in a separately
   versioned generation, preserve old exports, and consistently declare truly empty
   objects. Do not weaken the compiler's global alias rule. No fix was attempted.

## Draft and replacement closure

The original private draft remains **unchanged**:
`full-corpus-plan-20260908/control/source-plan-draft.json`, 71,123,618 bytes,
SHA `4a0e4fb7fa9cbe3c9df97bf7c419d181d64f3dbfe3cf2c97526452cd593ce9ce`.
It contains 106 sources and 41/43 complete input groups. Never print/grep the whole
single-line document; parse selected fields. `roots.json` SHA is
`22ef30315e8c547e335da12ce83d6694984caaa7393f8f1c5c55affda4d5f73f`.

Verified replacements are retained, not installed:

- D1 export: `d1-sources/315ef420a28828a7ed827b6208a89c530d76daf4f5837524e8ae34d28df19e4e`.
  Original read-only capture and all normalized data bytes are unchanged.
- Derived catalog: `derived-catalog-source-exports/dda362e2c5e647c9d9d5df49ef0d3ce523a9d2beecbe7c73ee1a14a6f2312fc3`.
  Six normalized files remain byte-identical.
- Fold: `proposal-fold-source-exports/5cd6b73e2c4448e53bfa20f89571974657309218b16a3b41e8fdfa181299bc89`.
  This verifies independently but needs the new combined MIME issue fixed before
  integration. Preserve 104 containers including five manual contributions, all
  326 discovery identities, and explicitly review 15 FC retractions and two module
  metadata corrections. See `BRAIN-SQLITE-CURATION-DECISION.md`; it is still proposed.

Read `source-replacement-design-20260908/README.md` and `inspection.json` before
writing any controls. They specify exact replacements, six reachable fold additions,
diagnostic-source exclusions, unchanged curated Git pins and transaction checks.
Expected count before Kerodon is 112 with the successful new OpenAlex pair.
The newly discovered fold generation change must update that design's pinned refs.
Keep existing `repo` and `wikilean_git` roots; give the final reducer its separate
`reducer_git` root. Do not use the old append-only materializer for replacements.

The new inventory is `brain/authority/reducer-inputs-v3-folded.json`, ID
`sha256:675e97745422fdd1ae7930eead2fb83f1d012236b452c08d84a368256ca2672c`.
Only that exact ID selects the three immutable fold input classes; historical
inventories stay unchanged. A new committed reducer and matching measured environment
are required; changing only the inventory would fail.

## Runtime and comparison preparation

Native Linux runtime is in `linux-runtime/`: Lima instance `wl`, native ARM64
Ubuntu 24.04, no host mounts. `LIMA_HOME=…/linux-runtime/lima-state`; executable
`…/linux-runtime/lima-2.2.0/bin/limactl`.
Existing descriptor `execution-environment-c64438d2.json` identifies image
`sha256:4813bc6a62e0e5e3537e749a24fd194e18506ec68104b70c269cfe17d6b7bafd`.
Its strict native kernel/runtime probes passed. The compiler requires environment
runner Git commit == reducer Git commit. At `69cfb173`, all eleven runner files
equal c644, so unchanged-image reuse would require a new exact-commit descriptor
plus fresh probe. The planned contract optimization changes runner bytes and
therefore requires rebuilding the image instead. Preserve the original evidence.

The unapplied performance patch and synthetic probe are in
`lineage-index-preparation-20260909/`. It indexes each validated parent's objects
once: original traversals grow from 10 to 130 for 8 versus 128 inputs; candidate
stays at three, with equal identities and rejection behavior. **Repo helper bytes
were not changed.** Apply only after live captures and exports finish. Add a proper
regression, preserve all 25 source profile registries/30 historical generations and
literal D1/entity whole tuples, and audit historical reproduction before adding new
current profiles. The per-exporter compatibility audit remains unfinished.

Old baseline checkout and ten exact old programs are in `legacy-baseline-20260908/`
at `ebac34dc`, tree `753f8e002466ec27d75cd6c41360e1a930dc178b`.
Do not use its provisional input map as a full verified pack.

Driver WIP at `952eac23` provides `run_legacy_baseline.py execute` and a separate
fixture-only `probe`. It requires exact pack/preparation identities and produces
`execution.json`, `legacy/`, and `launch/evidence/`. Review it independently first.
Actual native fixture evidence is in
`linux-runtime/legacy-driver-diagnostic-20260908/`; guest evidence is
`/home/jack.guest/runtime/legacy-probe-2/launch/evidence/`.
Both native probes passed 12 kernel checks. Probe 2 also passed the exact old-stage
bootstrap, sibling imports, NumPy 2.3.3 and explicit input configuration. It did
**not** run the seven full old graph stages.

Final order: verified pack → exact old input preparation → seven old stages with
fresh halo inserted before frontier → SQLite compatibility projector → legacy
release assembler → explicit comparison review → two real isolated new builds.
Use the original schema-1 writer's application ID zero; the projected new database
must pass full schema-2 checks. Preserve all seven semantic files exactly.

## Remaining gates and validation pointers

Private policy tools and the 25-family provenance checker are implemented; no
full-pack policy approval or release-specific reviewed mapping exists. The separate
private replay gate cannot grant public rights or accepted authority. P1B still
needs the real public baseline/shadow/dry-run evidence bundle; P1C needs Jack's exact
release/rollback/window approvals. P2A is complete only as an experimental shadow.
P2B–D authority/genesis, P3 D1 overlay and generated-artifact retirement remain later.

Logs: `/tmp/wikilean-migration-python-ci-private-legacy.log` (86 commands),
`/tmp/wikilean-openalex-v2-final.log` and `…-independent.log` (34 tests),
`/tmp/wikilean-legacy-driver-tests.log` (12),
`/tmp/wikilean-legacy-assembler-support-tests.log` (14).
Use the venv Python 3.12 and put `/usr/bin/git` before Homebrew's symlink in PATH;
the continuation checkpoint has exact commands. Host free disk was about 94 GiB.

Resume with capture/log inspection and the blockers above. Do not treat this push,
fixture success or the private source draft as migration completion.
