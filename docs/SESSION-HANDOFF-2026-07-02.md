# Session handoff — 2026-07-01 → 07-02

> Recovery doc written before a context compaction. Captures a very large two-day
> session. Durable facts also live in the memory system (`~/.claude/projects/-Users-jack-Desktop-LEAN-WikiLean/memory/`) — esp. `mathdb_unification.md`,
> `project_wikidata_proposal.md`, `project_propose_then_approve.md`,
> `wikilean_management_workflow.md`. The deep-research artifact is
> `docs/research/mathdb-unification-research.json` (12 reports + synthesis). When
> this doc and the code disagree, trust the code.

## The two arcs of this session

1. **Management workflow** (start): built the `manage/` control plane, wired a
   session-start hook + nightly automation, drained the Agent-2 backlog, shipped
   the propose-then-approve loop end-to-end, and hardened the concept graph.
2. **"Combine the internet's math databases"** (main mission, Jack's directive):
   deep research → a join fabric keyed on the Wikidata QID → shipped Phase 1
   (crossref chips, multi-library `/decl`, FC frontier) + Phase A (the bubble
   atlas + agent API) + the P14534 Wikidata property (submitted!) + an auto-push
   pipeline (gated, inert).

---

## SHIPPED & LIVE (all at https://wikilean.jackmccarthy.org)

### Management control plane (`manage/`)
- `manage/status.py` — session-start ground-truth snapshot (a SessionStart hook runs it). `manage/centrality.py` (PageRank), `coverage.py`, `worklists.py`, `refresh.py`. Commits `baf12c2`, `cceae86`, `f29e144`.
- **Formalize backlog wired** (`3e44cec`): `moderate.py review --slugs` + `manage/formalize_backlog.py` Agent-2 the extracted (Agent-1-only) articles the `/api/work` ladder can't see. Proven live (Finite_group 0%→60%). Rate tunable in **`site/ops/nightly.env`** (`8fd0acc`): `WIKILEAN_FORMALIZE_LIMIT=12`, plus `WIKILEAN_GRAPH_REFRESH/DEPLOY`, `WIKILEAN_DECLCITES_REFRESH`, `WIKILEAN_LIBDECLS_REFRESH`, `WIKILEAN_PROPOSALS=1` (generation ON).

### CLAUDE.md + HANDOFF
- `40af665`: created auto-loaded **`CLAUDE.md`** (durable conventions/invariants); slimmed `HANDOFF.md` to a pointer (live state → status.py, facts → memory, conventions → CLAUDE.md).

### Propose-then-approve (the human+AI loop) — LIVE
- Backend (`f425593`), inline banner UI (`a097da5`), pipeline harvest step 2 (`dbeea42`, gated `WIKILEAN_PROPOSALS=1` — now ON), tombstone guard (`1981995`).
- **Arc 2** (`5ef029f`, `5e3734d`): `proposals` lifecycle table (migration 0009), **`/proposals`** cross-article review queue (patroller/admin only), **`/stats` v3** (acceptance rate, canary). Reject-reason enum; dual-write self-heal; stale-sweep.
- Decisions are **patroller/admin-gated**; Jack is `admin` live. A demo proposal was seeded on `/Prime_ideal`.

### Concept graph `/graph` — heavily upgraded
- Human-reviewed filter (`6add98b`); coverage coloring (`5be5788`); served **KV-first** `graph:data:v1`, nightly refresh via `wrangler kv put` NOT deploy (`c176262`, `2f58a5c`).
- **Crossref chips** (`ffe1d60`, `3fc3f45`): 1,355/1,376 nodes joined to 12 external databases (MathWorld/nLab/ProofWiki/Metamath/LMFDB-knowl/OEIS/EoM/PlanetMath/DLMF/MSC/**P14534 Mathlib**/**Google kgmid**). Fetcher: `catalog/mathlib_deps/fetch_crossrefs.py`.
- **FC frontier layer** (`665904d`): 342 FormalConjectures statements → 114 QIDs (101 NEW frontier nodes, dashed amber rings). Ingest: `catalog/ingest_formal_conjectures.py`. `research solved` ≠ proved.

### Multi-library `/decl` resolver (Phase 1.4) — LIVE
- `6e8b182` + **`bd75bd5`** (the source `catalog/data/libraries.json` + `site/build_library_decls.py` were untracked; now committed). Resolves CSLib / Physlib / Formal Conjectures decls beyond Mathlib via KV `libdecls:v1` (16,874 own decls). `wiki/src/decl.ts`.

### The bubble atlas `/atlas` + agent API (Phase A) — LIVE
- `1781f6c`, review fix `5fb8c9a`. **Jack's design**: granularity as containment — continents → subfields → concepts → decl super-nodes (the 114 multi-QID decls; Module opens to its 6 concepts). `catalog/data/atlas_taxonomy.json` (9 curated/frozen continents), `site/build_atlas.py`, `site/build_atlas_page.py` (D3 zoomable circle-packing).
- **Agent API** (`wiki/src/atlas.ts`): `GET /api/atlas` (continents + routing edges), `GET /api/atlas/:key` (one bounded bubble: concepts≤500, supernodes, top-20 edges). Progressive disclosure so agents traverse coarse→fine without loading the whole graph. Verified live.

### Wikidata property **P14534 "Mathlib Declaration ID" — SUBMITTED**
- P14534 already **existed** (created 2026-06-17 by Jack's own account **"Mynus grey"**). Seed = the 126 merged `@[wikidata]` tags (NOT the 815 AI mappings). `bot/export_property_seed.py` (`d8b2d3b`, `6984c6a`, `b729d8a`), 1000.yaml ingest (`3a2d23e`), `/decl` formatter (`ee532c8`), finalized batch (`0a2d4d8`).
- **Jack submitted the 114-statement batch this session** (QuickStatements v2, as Mynus grey, EditGroups batch **QSv2T**). Result: **114/114 landed, P14534 4→115 uses**, single-value constraint clean. 1 distinct-value flag: decl `Real` on both `Q12916` (real number) + `Q2584477` (construction of the real numbers) — both are merged mathlib tags; **steward's call, not an error.**

### CC0 join fabric (plan item 11)
- `catalog/build_join_fabric.py` → `join_fabric.tsv/.jsonl`: 1,025 (QID→decl) mappings, provenance-tiered (merged 108 / mathlib-maintainer 150 / ai-verified 61 / ai 706). Commit `539df34`.

---

## GATED / INERT (built, reviewed, OFF until Jack enables)

### Auto-push P14534 on merge (`033f3c9`, review fixes `d4fd723`)
- **What:** when `@[wikidata]` tags merge into mathlib master, the poller auto-adds the P14534 statements to Wikidata (the **merge IS the review gate**).
- `bot/push_property.py` reads `property_seed_autopush.csv` (the PURE `bot`/`external` subset, 109 rows — catalog-settled tiebreaks EXCLUDED), diffs live vs Wikidata, POSTs net-new to the QuickStatements batch API as Mynus grey.
- **Safety envelope** (all adversarially reviewed): gated by `WIKILEAN_PUSH_PROPERTY` · dry-run unless token+`--submit` · **once per merged PR** (`pushed_pr` guard, `bot/poll.py`) · never overwrites (different value = logged conflict) · WDQS **COUNT cross-check** (partial result → abort) · `MAX_AUTO=50` cap · token via stdin not argv · EditGroups-undoable.
- **STATUS: INERT.** `.github/workflows/wikidata-poll.yml` sets `WIKILEAN_PUSH_PROPERTY: "0"`. Secret `QUICKSTATEMENTS_TOKEN` is set on `Deicyde/WikiLean`. Verified idempotent (109 rows → 0 net-new).
- **To enable:** flip `"0"`→`"1"` in the workflow. (Offered but not done: wire it to a repo variable `gh variable set WIKILEAN_PUSH_PROPERTY --body 1` so it toggles from the UI without a commit.)

### `docs/outreach.md` — 3–4 collaboration drafts (Jack untracked it, `0e0e974`, to keep private)
Drafts for Jack to send (never auto-sent), each opening with a concrete gift:
- **Vasily Ilyin** (Theorem Graph, vilin@uw.edu) — the QID concept layer they lack (decl→QID table + FC→QID mapping); clarify their CC-BY-SA-vs-NC license.
- **Chris Birkbeck** (LMFDB/LeanBridge) — independent cross-check of their ~62 `mathlib=` knowls + co-propose the missing "LMFDB object label" property.
- **Pieter Belmans** (Stacks) — the tag→decl dict his stalled Nov-2024 backlink plan needed (323 `@[stacks]` tags) + a "Stacks Project tag" Wikidata property (field verified empty).
- **Mathlib maintainers** (draft #4, added by Jack) — a shared Mix'n'match catalog for the 815 AI mappings.

---

## OPEN DECISIONS / WAITING ON JACK
1. **Enable the auto-push?** Flip `WIKILEAN_PUSH_PROPERTY` (or ask me to wire the repo-variable toggle).
2. **Send the outreach drafts** (`docs/outreach.md`, currently untracked/local).
3. **P14534 `Real` distinct-value flag** — keep both QIDs, or drop `Q2584477`? (steward call).
4. **`/wikifunctions/verify` 404** — its source links point at `github.com/Deicyde/wikifunctions`; commit `8ce2d43` "migrate the verification project to Deicyde/wikifunctions" may have resolved this — **verify the links resolve now**; if not, the proofs live on branch `migrate/wikifunctions-standalone` (needs merge to `main`).
5. **Scheduled reminder** `p14534-quickstatements-reminder` (daily 9:05am) — Jack interrupted my attempt to disable it; it self-cancels on its next run (checks the live count, sees 115>100, disables itself). Harmless either way.
6. **Next build** — recommended item 6 (LMFDB triangle harvest; mirror reachable, needs `psycopg`), which produces the Chris cross-check artifact.

---

## THE MATH-DB UNIFICATION PLAN (12 items; see the research JSON for full detail)
Done: (1) tag ground-truth ✓ · (2) P14534 seed **SUBMITTED** · (3) crossref chips ✓ · (4) multi-library `/decl` ✓ · (5) FC frontier ✓ · (11) CC0 join fabric ✓ · **Phase A atlas ✓**.
Remaining: **(6) LMFDB triangle harvest** [next; Postgres mirror `devmirror.lmfdb.xyz:5432` `lmfdb/lmfdb` open, needs psycopg] · (7) schema v4 one-shot rewrite [formalizations[] + references[] + per-annotation qid — high-stakes, do once] · (8) TheoremGraph moderated ingestion [needs Vasily's license answer] · (9) Erdős–OEIS 4-way triangle · (10) Mix'n'match catalog + LMFDB object-label property · **(12) the tiled Paperscape atlas = Phase B** [LATER].

### The join fabric (spine) & granularity model
- **Spine = the Wikidata QID** (the only cross-database join mechanism in math — no federated SPARQL exists). Also `(library, fully-qualified decl)` pairs and `declaration-data.bmp` (universal CORS-open decl oracle on every doc-gen4 site).
- **4 layers**: L0 concept (QID; only dedup layer) / L1 formal statement ((library,decl)@pin) / L1' literature instance (TheoremGraph UUID = session key) / L2 object (LMFDB label, OEIS A-number). MSC = topic overlay, never identity. Every QID→QID edge is a LIFT of lower-layer edges with evidence payload attached.
- **Anti-slop doctrine** (9 rules in the JSON): evidence payload + version pin per edge · existence oracle before write · durable keys + tombstones · triangulate → disagreement to humans · ingest only precision-labeled slices · LLMs propose / humans publish · source-tiered trust · nightly drift watch · license provenance per record.

---

## KEY GOTCHAS & FACTS DISCOVERED (don't re-learn these)
- **P14534 = Jack's own property** (Mynus grey). QuickStatements API needs ≥1 manual batch first (done); token on the QS user page. API: `POST quickstatements.toolforge.org/api.php` `action=import&format=v1&submit=1&data=<v1>&username&token` → `{status,batch_id}`.
- **LMFDB LeanBridge is funded** (Birkbeck/Roe/Sutherland); 62 `mathlib=` knowls (status=awaiting-review); P12987 = knowl property (no object-label property — the gap). Whole DB open at `devmirror.lmfdb.xyz:5432` (`lmfdb/lmfdb`), incl. `kwl_knowls` (not in the API). API needs `Cookie: human=1`.
- **TheoremGraph** (arXiv 2606.25363) = a draft of the north star (11.7M arXiv theorems ↔ 388k decls, incl. FC+CSLib) but NO concept layer, NO dedup, judge precision 48–87%. License contradiction CC-BY-SA vs CC-BY-NC-SA (ask before republishing).
- **WDQS graph split**: `P818` arXiv joins return ~0 on the main endpoint; use `query-scholarly.wikidata.org`.
- **Mix'n'match TSV import treats `q` AND `autoq` as fully-matched** — the 815 AI mappings MUST use JSON import with `user:0` (preliminary). No Mathlib catalog exists yet (Metamath #6372 is the template).
- `@[stacks]` in mathlib: 323 unique tags / 467 decls; Wikidata Stacks field is empty (next property).
- **Infra**: system Python SSL is broken → all external HTTPS uses `curl`. Remote D1 migration-tracking is out of sync → apply via `wrangler d1 execute --remote --file=…`, NOT `migrations apply`. Max-auth gotcha: unset `ANTHROPIC_API_KEY` before SDK/moderate runs. `build-public.ts` must run FROM `wiki/`.

---

## WORKING TREE STATE (at handoff)
- Everything above is **committed** (latest: `bd75bd5`). Last Worker deploy included the atlas + `/api/atlas` + the atlas prototype-key fix.
- **Uncommitted (derived/support, left for Jack's judgment):** `manage/data/*.json` (regenerated worklists/coverage/digest), `site/cache/*.meta.json` (pinned Wikipedia revision caches — many new from formalize runs), ~182 `site/annotations/*.json` (npm-run-pull churn — the standing "unrelated diff" to stage-explicit around). None are code; all regenerable except the annotation/cache pins.
- Nothing has been **pushed** to origin (Jack pushes when ready; standing rule).

## PARALLEL/OTHER-SESSION COMMITS interleaved (not mine, noted for completeness)
`6c5b4b3` batch size 25→10 (maintainer request) · `f0d9f8c` /review OAuth Submit · `4ddd32a` outreach #4 · `0e0e974` untrack outreach · `8ce2d43` wikifunctions migrate.
