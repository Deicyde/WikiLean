# The Brain on D1: the read layer

Design, 2026-11-07, revised 2026-11-08. Part of [`docs/BRAIN-TAUCETI.md`](BRAIN-TAUCETI.md);
tracking issue #152. Step 1 (the serving database builder) is PR #153; everything after it is
design, not code.

**References.** `path:line` is at main `596aa7bb92e89c1f7a44f768392c3b895b049c47`. A bare
number after a reference is a line in the same file. Bare file names:

| Files | Directory |
|---|---|
| `*.ts` | `wiki/src/`, except `brain-release-public.ts` (`wiki/scripts/`) and `d1shim.ts`, `brain-fixture.ts` (`wiki/test/helpers/`) |
| `authority_contracts.py`, `build_release.py`, `build_replay_release.py`, `verify_release.py`, `provenance_coverage.py`, `check_provenance_coverage.py`, `private_replay_gate.py`, `reproducibility_gate.py`, `compile_offline_pack_v2.py`, `legacy_sqlite_projection.py`, `assemble_legacy_release.py` | `brain/tools/` |
| `brain_promote_release.py`, `brain_activation_bundle.py`, `brain_deploy_journal.py`, `brain-canary.py`, `brain_release_authority.py`, `brain-nightly.sh` | `site/ops/` |
| `build_brain_page.py` | `site/` |
| `build_cell_shards.py`, `build_serving_db.py`, `store.py`, `query.py`, `layout.py` | `brain/` |
| `cli.js` | wrangler 4.151.3's bundled CLI (`wrangler-dist/cli.js`), the version `wiki/package.json:46` pins |

`build_serving_db.py` exists only in PR #153 and is cited by name. Limits marked
"(unchecked)" are recalled from Cloudflare's documentation and have not been checked against
it. Issue #150 (trigram support, row sizes, reads during an import) has no results yet.

## Summary

- **Rows belong to serving datasets, not to releases.** A dataset is named by the digest of
  its serving tables, and a `releases` table maps each release to one dataset. A release whose
  tables didn't change costs no import.
- **One module reads the database.** `brain-db.ts` is the only code that reads
  `wikilean-brain`. Every statement is limited to the dataset the request's release binds.
  Every round trip starts with a readiness check: a dataset that is missing, half-imported or
  on an unknown schema answers 503, never an empty "not found".
- **Every statement has a bound**, either a LIMIT or a ceiling the builder guarantees. No
  prefix range, scan or ranking runs over an open-ended set.
- **The selector stays static.** `current.json` and `release.json` stay files and roll back
  together with the Worker. `release.json` binds the serving tables through a small JSON
  sidecar file.
- **The importer can resume and repair.** It writes each table in sequence ranges as DELETE
  then INSERT, reads every range back, and records each verified range in a journal. It never
  uses a file import, which wrangler says makes the database "unavailable to serve queries"
  (`cli.js:238241`).
- **Pruning keeps everything that can still be served:** the live current and previous
  releases, the releases named in the latest promotion attempt until that attempt reaches a
  terminal outcome, and one candidate that hasn't been promoted yet. At most three datasets
  are protected at once. Import and prune both take the promoter's lock, and an import
  registers its release before it prunes.
- **The /brain page stops loading whole files.** It calls eight Worker endpoints that carry
  the release in the URL, so the edge can cache the current release's responses for good. The
  only static files left are the selector, the release manifest, the cell manifest, the
  frontier data and the sources.
- **Replay first.** The replay tools learn the D1 layout before the cutover, so the first D1
  release can be a full offline replay release.

## Decisions

Jack's:

1. **Storage** (2026-11-06): "Sure take them out of git. The data should really live on our
   CloudFlare SQLite database."
2. **One shared database on Workers Paid** (2026-11-06): "I think one shared database is
   best". Workers Paid is confirmed in issue #135. The database is `wikilean-brain`, separate
   from `wikilean`.
3. **Search** (2026-11-06): "Whatever you think would be most useful for humans and agents."
   The design's answer is decision 9 below.
4. **Explorer** (2026-11-06): "I like the current explorer, but I'm open to improving it if
   you have ideas." The design's answer is decision 12 below.
5. **Layout** (2026-11-06): "Yes this bubble idea sounds very cool to me I love it."
6. **Content-addressed serving datasets** (2026-11-08): rows are keyed by serving dataset,
   not by release.
   - Each row carries a small integer `ds`. The `datasets` table maps it one-to-one to the
     dataset id, which is the `logical_root` of `brain/data/serving-tables.json`. That root
     hashes only the sidecar: the schema version, each table's row count, digest and delete
     bounds, and two name constants. Every FTS input is a digested column, so the id also
     binds what the search indexes are built from.
   - `releases` maps each `release_id` to a dataset. The importer imports a dataset only if
     the database doesn't hold it yet. Pruning deletes a dataset only when no protected
     release references it.
   - A release whose serving tables are unchanged (a Worker-only or articles-plane release)
     costs no import and no prune, and releases with matching tables share one copy under the
     10 GB cap. The cost is an extra table plus reference counting, so an operator can't
     simply drop one release's rows.
   - Rows keyed by release would re-import about 1 GB for every release, however small,
     because the release id hashes `authority.git_commit` (`brain.ts:170-177`) and there is no
     Worker-only release path (`CLAUDE.md:202-206`).
7. **Replay** (2026-11-08). Asked whether D1 releases should be compatibility-only until the
   replay tools learn the D1 layout, Jack answered "Whatever is better long-term." So the
   replay tools move onto the D1 layout before the cutover (section 6).

The design's own choices:

8. **Keep the selector static.** A selector row in D1 would cost one D1 read per request that
   can't be cached (`brain.ts:3-5`), and switching releases would no longer roll back with the
   Worker.
9. **Search behaves differently** (details in section 1):
   - exact matches come first, then prefix matches, then fragment matches, with cells before
     supercells within each tier;
   - 2-character queries get exact and prefix matches only;
   - text hits come after name hits, with their own limit, and every hit says which index
     matched;
   - text queries drop stopwords and fall back from "all words" to "any word".
10. **Bind the tables through a sidecar.** `release.json` gains two artifacts:
    `brain/data/serving.sqlite3` (opaque, `authority_contracts.py:187-188`) and
    `brain/data/serving-tables.json` (JSON, so it gets a logical root under the existing
    rules, `authority_contracts.py:183-184`). A release that lists the sidecar is a D1
    release, so no new profile name is needed. New profiles would touch `brain.ts:111-114`,
    `authority_contracts.py:3508-3509`, `brain-release-public.ts:369` and
    `brain-canary.py:219-220`.
11. **Page endpoints** live at `/api/brain/r/<release hex>/...`.
    - Responses for the selector's current release (the Worker's own) are immutable, so the
      edge caches them for good. Responses for the previous release get `max-age=300`,
      because a newer Worker computes them. The Cache API key includes the Worker version id.
    - The endpoint response format and each dataset's `schema_version` are a cross-release
      contract: the next Worker must serve the previous release in the format its frozen page
      expects (section 1, Caching).
    - Rate limiters apply only on cache misses and are sized by D1 cost. Per IP: 300 a minute
      for the light endpoints, 60 for search, 20 for explorer. A per-isolate cap limits
      concurrent D1 requests from page misses, the agent API and MCP together.
    - They stay off the agents' 120-a-minute budget (`brain-api.ts:2500-2509`). The agent API
      and MCP get their own D1-cost budget (section 1, Limits and costs).
12. **Explorer.**
    - Nothing changes for today's data.
    - In every scope, a neighbor that the ripple pulls in and that sits in a module bubble is
      drawn as its module's dot, so Mathlib scopes look as they do now.
    - Small scopes are computed live. Larger scopes and `all` are precomputed, and a
      precomputed scope over 21k cells also collapses its own bubbled cells.
    - Each precomputed scope is at most 8 MB. The build collapses more bubbles until a scope
      fits, and fails if it still doesn't. The page fetches a scope's parts as separate
      cacheable requests.
    - Summed edges to a module dot are drawn but can't be clicked.
13. **`brain_filter` pages can come back short.** A call examines at most 10,000 rows, so it
    can return fewer than `limit` hits along with a non-null cursor. The cursor format doesn't
    change.
14. **Supercell evidence gets filled in** from a trace table. On the agent API it is trimmed
    to 6 traces per row, the same cap cell rows have. Those rows also gain `traces_total`.
15. **Integrity checking moves.** Today every Brain byte is hashed against the signed
    inventory when it loads (`brain.ts:533-540`, `build_brain_page.py:596-597`). With D1,
    integrity rests on three things: the importer's per-range digests, the readiness check on
    every request, and a read-only sampled re-check after every canary and every night.

## 1. The Worker module

### Datasets and scoping

- **Binding.** `wiki/wrangler.jsonc` gets a second `d1_databases` entry: `BRAIN_DB`, pointing
  at `wikilean-brain`. Today only `DB` exists (`wiki/wrangler.jsonc:120-126`, `env.ts:2`). If
  `BRAIN_DB` isn't bound, every D1 release is unavailable.
- **Release context.** `BrainReleaseContext` (`brain.ts:127-136`) gains two fields:
  - `backend: "static" | "d1"`. A release is `d1` when its `release.json` lists the sidecar.
  - `datasetId`, the sidecar's `logical_root`.

  `parseReleaseManifest` already checks the root's form (`brain.ts:365`; for the sidecar it
  must also be non-null) and binds every artifact into the release id (`brain.ts:345`). So
  this costs no extra fetch.
- **Scope.** Every data statement selects rows with `ds = $DS`, where

  `$DS = (SELECT ds FROM datasets WHERE dataset_id = ?1 AND state = 'ready')`

  so a dataset that is importing, being deleted or absent matches nothing. The Worker never
  reads `releases`. The dataset id comes from the release's own signed manifest, and rows that
  match its digests are correct for any release that binds them.
- **Readiness check.** `brainBatch(c, release, stmts)` sends `[probe, ...stmts]` as one
  `BRAIN_DB.batch`. A batch runs as one transaction (unchecked), so the probe and the reads
  see the same state. The probe is:

  `SELECT schema_version, names_mode, longest_name, sup_names_from FROM datasets WHERE dataset_id = ?1 AND state = 'ready'`

  It throws `BrainReleaseUnavailableError(release_id, "BRAIN_DB", reason)`
  (`brain.ts:558-568`) in three cases: no row comes back, `schema_version` isn't one this
  Worker can read, or D1 raises an exception. The last three columns never change for a given
  dataset, so the Worker memoizes them by dataset id.
- **Error handling stays as it is.** `withRelease` and MCP already turn that error into a 503
  carrying `release_id` (`brain-api.ts:2485-2491`, `mcp.ts:476-486`). Any other exception
  would become a 500, or "internal error" in MCP (`mcp.ts:487-497`).
- **An empty result after a passing probe is a real miss.** It becomes null and then a 404,
  as a missing shard entry does today (`brain-api.ts:404-405`). This matters most for the
  community-edit check: "not a node" plus a QID creates a community node
  (`brain-edits.ts:269-273`).
- **FTS tables** are named `names_<hex>` and `texts_<hex>`, using the dataset id's 64 hex
  digits. Nothing else is pasted into SQL.

### Functions (`wiki/src/brain-db.ts`)

Each function returns the statements for one `brainBatch`. A route sends all of its
independent reads in one batch. The bracket after each function gives that statement's worst
case.

```text
-- $DS = (SELECT ds FROM datasets WHERE dataset_id = ?1 AND state = 'ready')
cellPage(id)                                                        [1 row]
  SELECT json FROM cell_pages WHERE ds = $DS AND id = ?2
cellPages(ids, at most 200)                                         [200 rows]
  SELECT id, json FROM cell_pages WHERE ds = $DS AND id IN (SELECT value FROM json_each(?2))
supercell(path) -> {row, breadcrumb}                                [1 + 64 rows]
  SELECT json FROM supercells WHERE ds = $DS AND path = ?2;
  WITH RECURSIVE up(path, d) AS (
      SELECT parent, 1 FROM supercells WHERE ds = $DS AND path = ?2
      UNION ALL SELECT s.parent, d + 1 FROM up JOIN supercells s ON s.ds = $DS AND s.path = up.path
      WHERE d < 64)
    SELECT up.path, s.label FROM up LEFT JOIN supercells s ON s.ds = $DS AND s.path = up.path
    WHERE up.path IS NOT NULL ORDER BY d DESC
aliasHits(key, bare, allowSlug), JS keeps organ > decl > slug       [3 rows]
  SELECT kind, owner FROM aliases WHERE ds = $DS AND ((kind = 'organ' AND key = ?2)
    OR (kind = 'decl' AND key = ?3) OR (?4 = 1 AND kind = 'slug' AND key = ?2))
exactName(keyLc, slugLc), cells first, then supercell organ labels  [1 row]
  SELECT owner FROM name_keys WHERE ds = $DS AND ((kind IN ('label', 'aka') AND name_lc = ?2)
    OR (kind = 'sorgan' AND name_lc IN (?2, ?3))) ORDER BY seq LIMIT 1
organPages(ids, at most 100); organOwners is the same without joins [100 rows]
  SELECT a.key, a.owner, json_extract(cp.json, '$.organs') AS cell_organs,
         json_extract(s.json, '$.organs') AS sup_organs FROM aliases a
    LEFT JOIN cell_pages cp ON cp.ds = a.ds AND cp.id = a.owner
    LEFT JOIN supercells s ON s.ds = a.ds AND s.path = a.owner
  WHERE a.ds = $DS AND a.kind = 'organ' AND a.key IN (SELECT value FROM json_each(?2))
partnerRows(id), label rows of one page's synapse partners          [200 rows, SYN_CAP]
  SELECT l.id, l.label, l.f, l.p, l.libs FROM cell_pages cp, json_each(cp.json, '$.syn') j
    JOIN labels l ON l.ds = cp.ds AND l.id = json_extract(j.value, '$.id')
  WHERE cp.ds = $DS AND cp.id = ?2   (and the same join to supercells for path: partners)
atomsExist(ids, at most 200)                                        [200 rows]
  SELECT id FROM cell_pages WHERE ds = $DS AND id IN (SELECT value FROM json_each(?2));
  SELECT path FROM supercells WHERE ds = $DS AND path IN (SELECT value FROM json_each(?2))
labelRows(ids, at most 500)                                         [500 rows]
  SELECT id, label, aka, f, p, libs FROM labels WHERE ds = $DS AND id IN (SELECT value FROM json_each(?2));
  SELECT path, label, fa, parent, n_cells, n_sub FROM supercells WHERE ds = $DS
    AND path IN (SELECT value FROM json_each(?2))
synapseTraces(pairs, at most 200)                                   [200 rows]
  SELECT pair, json FROM synapse_traces WHERE ds = $DS AND pair IN (SELECT value FROM json_each(?2))
xrefPages(node) / xrefNodes(pages, at most 40)                      [40 / 4,040 rows]
  SELECT page FROM xrefs WHERE ds = $DS AND node = ?2 ORDER BY page_ord LIMIT 40;
  SELECT page, node FROM (SELECT page, node, node_ord, row_number() OVER
      (PARTITION BY page ORDER BY node_ord) AS k FROM xrefs
      WHERE ds = $DS AND page IN (SELECT value FROM json_each(?2)))
  WHERE k <= 101 ORDER BY page, node_ord   -- the route keeps 40 pages, 100 partners (brain-edits.ts:1461-1464)
search, bridge, filter, level, explorer: below
```

Round trips per route:
- a card by atom id: 1; by organ key: 2;
- search: 2; filter: 1; neighborhood: 2;
- bridge: 2 or 3, and at most 6 when the statement pass needs all of its chunks;
- quickstatements: 1 before its first write;
- every page endpoint: 1 or 2.

### Search

The names index is `name_keys`. It holds one row per distinct (lowercased name, kind, owner):
- for cells: labels (`label`) and other names (`aka`);
- for supercells: labels (`slabel`), the whole path after the `path:` prefix (`spath`; for
  frontier areas, the name after `frontier:`) and organ labels (`sorgan`). The page's area
  search matches that whole path, by prefix or substring (`build_brain_page.py:4524`,
  `4539-4540`), so a query such as `algebra/group` or `mathlib/alg` still finds the area.

It also keeps each name in its original case (`name`), the input the parts tokenizer needs.
That column is digested in the local file but not copied to D1 (section 4).

Its `seq` column follows the result order within a tier: cells in labels order, then
supercells in file order. Supercell names start at `datasets.sup_names_from`.

For a query q of 2 to 200 characters, lowercased with `toLowerCase`, one batch runs these
statements:

1. **Exact:** `name_lc = q`, `ORDER BY seq LIMIT 200`.
2. **Prefix.** The builder lists every hot prefix, meaning one of 2 to 200 UTF-16 units that
   starts more than 1,000 names, in `hot_prefixes`, with three owner lists of up to 100 each:
   cells, supercells, and supercells whose subtree holds a cell (`live_sups`).
   - If q is a hot prefix, the Worker reads that one row. The page's search uses `live_sups`,
     because the page hides areas whose subtree is empty (`build_brain_page.py:4518`); the
     agent search uses `sups`.
   - Otherwise it reads `name_lc > q AND name_lc < q || char(0x10FFFF)` in index order with
     `LIMIT 1001`. The builder guarantees that is the whole set, and the Worker sorts it by
     seq.
   - Without the hot list, a prefix range would be unbounded. 80% of a sample of TauCeti
     declarations start with `TauCeti.` (33,957 of 42,461), and decl labels are full names
     (`brain/build_common.py:1039`). So every `tau` keystroke would read and sort about 250k
     index rows.
3. **Fragment**, for q of 3 or more characters:

   `SELECT rowid FROM names_<hex> WHERE names_<hex> MATCH ?q ORDER BY rowid LIMIT ?k`

   Here k = 20 × limit, at most 2,000; rowid = seq; and q is passed as one quoted phrase. A
   type filter adds `rowid < sup_names_from` or `rowid >= sup_names_from`.
4. **Text.** The query keeps up to 8 words after dropping a fixed stopword list. It first
   requires all words (AND), then fills the remaining slots with any word (OR). Each statement
   is:

   `SELECT * FROM (SELECT rowid, bm25(texts_<hex>) AS s FROM texts_<hex> WHERE texts_<hex> MATCH ?q LIMIT 2000) ORDER BY s LIMIT ?text_limit`

   So bm25 ranks at most 2,000 matches, taken in rowid order. `text_ids.seq` follows the
   owners' order, so the cap favors the richer atoms.

A MATCH reads its terms' doclists, never the table. A second round trip reads the rows of the
chosen owners, at most limit + text_limit of them.

Order of the hits:
1. the exact-key promotion, as today (agents only, `brain-api.ts:1554-1565`), tagged
   `matched`;
2. name hits: exact, then prefix, then fragment, each tier with cells before supercells,
   deduplicated by owner, tagged `index: "names"` plus `tier`;
3. text hits, tagged `index: "texts"` plus `node` and `score`, up to `text_limit`.

Page hits also carry the matched name, plus `f` and `libs` for cells, or `type` (`area` or
`frontier`), `n_sub` and `fhist` for supercells.

### Bridge candidates

Today `atomsInStatement` tests every label against q (`brain-api.ts:2288-2300`). q has no
length cap (`brain-api.ts:2309-2311`), and an MCP body can be up to 64 KiB (`mcp.ts:368`). The
D1 version works like this:

1. **Candidates.** A candidate is a substring of the lowercased q that:
   - starts at position 0 or after a character outside [a-z0-9];
   - ends at the end of q or before such a character (the same boundary rule as
     `containsWord`, `brain-api.ts:2278-2287`);
   - is at least 4 and at most `longest_name` UTF-16 units long.

   A name passes today's test exactly when it equals one of these candidates.
2. **Queries.** Candidates are sent longest first, one length class after another, in chunks
   of at most 2,000, each chunk as one JSON parameter:

   `SELECT owner, max(len16) AS len FROM name_keys WHERE ds = $DS AND kind IN ('label', 'aka') AND name_lc IN (SELECT value FROM json_each(?2)) GROUP BY owner ORDER BY len DESC, owner LIMIT 5`

3. **Stopping.** The Worker keeps each atom's longest match. It stops once it holds 5 hits and
   the next unqueried candidate is shorter than the 5th hit, so a tie class is always
   finished. Taking only the top 5 per chunk loses nothing: an atom that belongs in the final
   top 5 can have at most four atoms ranked above it in any chunk.
4. **Bounds.** Only the first 2,000 characters of q are scanned, and at most 4 chunks are
   sent. A call that stops at either bound reports `statement_scan: "truncated"`.

`len16` is the lowercased name's length in UTF-16 units, stored by the builder, so lengths
match what JS computes for the lowercased candidates. Within these bounds the results equal
today's, with one exception: ties are ordered by id, and SQLite orders ids by code point where
JS uses UTF-16 units. The two orders differ only between astral characters and characters in
U+E000-U+FFFF.

### Filter

```text
filterCells(mask, under, fromOrd, limit)                          [10,000 rows examined]
  under = '': SELECT ord, id, label, aka, f, p FROM labels
              WHERE ds = $DS AND ord >= ?2 AND ord < ?2 + 10000 AND (coalesce(f, 0) & ?3) = ?3
              ORDER BY ord LIMIT ?5 + 1
  under = P:  SELECT l.ord, l.id, l.label, l.aka, l.f, l.p
              FROM (SELECT ord FROM cell_under WHERE ds = $DS AND anc = ?4 AND ord >= ?2
                    ORDER BY ord LIMIT 10000) u
              JOIN labels l ON l.ds = $DS AND l.ord = u.ord
              WHERE (coalesce(l.f, 0) & ?3) = ?3 ORDER BY l.ord LIMIT ?5 + 1
filterSupercells(mask, under, fromOrd, limit)                     [10,000 rows examined]
  SELECT ord, path, label, fa, parent, n_cells FROM supercells
  WHERE ds = $DS AND ord >= ?2 AND ord < ?2 + 10000 AND (coalesce(fa, 0) & ?3) = ?3
    AND (?4 = '' OR path = ?4 OR (path >= ?4 || '/' AND path < ?4 || '0'))
  ORDER BY ord LIMIT ?5 + 1
```

- `labels.ord` is the index in `labels.json`, so cursors keep their meaning. Rows are rebuilt
  as today's label rows: `aka` is split on U+001F and absent fields are omitted.
- `cell_under` has one row per cell for every `/`-prefix of every folder whose cells list
  holds that cell, frontier areas included, and of the cell's `p`. That is exactly today's
  `under` test (`p` under the prefix, or membership in a folder in the subtree,
  `brain-api.ts:1443-1476`). `p` is always one of the cell's folders today
  (`build_cell_shards.py:1036-1038`); the builder takes it as well so the table stays equal to
  the Worker's test if that drifts. It uses string ranges, not `LIKE`: `LIKE` would fold case
  and treat `_` as a wildcard.
- Supercells page in file order, which is the sorted `path:` rows followed by the `frontier:`
  rows (`build_cell_shards.py:1073`, `1117-1147`). Ordering by path would move the frontier
  rows first.
- If a call reaches its examination cap before filling `limit`, `next_cursor` points just past
  the last row it examined.

### Level and explorer

```text
level(path) -> {row, chain, children, cells}; an empty path is the roots level
  SELECT json FROM supercells WHERE ds = $DS AND path = ?2;                               [1 row]
  SELECT path, label, aka, n_sub, n_cells, fa, frontier, fhist, kids, syn FROM supercells
    WHERE ds = $DS AND parent IS ?2 ORDER BY ord LIMIT 2001;         -- ?2 NULL for roots [2,001 rows]
  SELECT l.id, l.label, l.aka, l.f, l.p, l.libs FROM supercells s, json_each(s.json, '$.cells') j
    JOIN labels l ON l.ds = s.ds AND l.id = j.value
  WHERE s.ds = $DS AND s.path = ?2 LIMIT 5001;                                           [5,001 rows]
  plus the breadcrumb statement from supercell()
explorerScope(scope, part) -> {nodes, edges, bubbles, universe} or one precomputed part
  SELECT json FROM explorer_scopes WHERE ds = $DS AND scope = ?2 AND part = ?3;  [1 row, at most 1 MB]
  -- part 0 carries the scope's part count (at most 8, since a scope is at most 8 MB);
  -- the page fetches parts 1.. as separate explorer?scope=&part= requests
  -- no row for part 0: the builder marked this scope live (core plus ripple at most 2,000 cells, at
  -- most 20,000 edges touching it). Three statements share these CTEs:
  WITH core AS MATERIALIZED (SELECT id FROM labels
         WHERE ds = $DS AND (p = ?2 OR (p >= ?2 || '/' AND p < ?2 || '0')) LIMIT 2001),
       touch AS MATERIALIZED (SELECT id FROM core
         UNION SELECT b FROM explorer_edges WHERE ds = $DS AND a IN core
         UNION SELECT a FROM explorer_edges WHERE ds = $DS AND b IN core)
  SELECT id, label, xy, f, p, libs, bub FROM labels WHERE ds = $DS AND id IN touch LIMIT 2001;
  ... SELECT a, b, w FROM explorer_edges WHERE ds = $DS AND a IN touch AND b IN touch
        ORDER BY w DESC, a, b LIMIT 20001;
  ... SELECT path, disc, n, fa, libs FROM bubbles WHERE ds = $DS AND path IN
        (SELECT bub FROM labels WHERE ds = $DS AND id IN touch AND bub IS NOT NULL) LIMIT 2001
```

- A folder's child heads carry the child's `syn` (its synapse rows, without traces). The
  Worker keeps only rows whose partner is another child or a cell on the same level, which is
  exactly what the level's synapse web draws (`build_brain_page.py:1558-1561`).
- The roots level supplies `n_sub` and `fhist` for the library list, the top level and the
  Libraries panel (`build_brain_page.py:903-905`, `1136-1149`, `2676`, `4021-4022`).
- A response cut short by a LIMIT says `truncated`. A live explorer result that goes over its
  LIMIT means the builder and the data disagree, and it answers 503.
- Ripple cells that have `bub` set become their module's dot, in live and precomputed scopes
  alike (decision 12).

### Caching

- **Edge.** A page endpoint's URL names its release, so the data behind a 200 or 404 there
  never changes. The response itself is Worker output, not a signed file, so how long it may
  be cached depends on which Worker computed it.
  - Responses for the selector's current release, the release this Worker was deployed with,
    send `Cache-Control: public, max-age=31536000, immutable`.
  - Responses for the previous release send `Cache-Control: public, max-age=300`. A newer
    Worker computes them, so a rollback must not leave that Worker's output cached for a year
    in browsers.
  - Both are stored with the Cache API under a key made of the Worker version id (from the
    existing `CF_VERSION_METADATA` binding, `wiki/wrangler.jsonc:20-22`) plus the normalized
    URL (sorted parameters, canonical id lists). A rolled-back Worker therefore never reads a
    newer Worker's entries. Without the binding (as in tests, `env.ts:83-85`) the Worker skips
    the Cache API. The Cache API needs a custom domain (unchecked), which the site has
    (`wiki/wrangler.jsonc:28-30`).
  - 410 and 503 responses are `no-store`.
  - The selector is checked before the cache lookup, so a retired release answers 410 even
    while its responses are still cached.
- **Cross-release contract.** Release N's frozen `brain.html` calls Worker N+1 while N is the
  previous release. So the endpoint response format (every endpoint, parameter and field in
  section 3) and each dataset's `schema_version` are a contract the next Worker must honor:
  - a Worker reads the `schema_version` of the previous release's dataset as well as its own;
  - format changes are additive (new fields, new endpoints). Removing or changing a field
    waits until no current or previous release's page reads it.
- **Agent API.** `/api/brain/*` and MCP keep `max-age=0, must-revalidate`
  (`brain-api.ts:2460-2470`) and query D1 on every call, behind the same concurrency gate and
  a D1-cost budget (Limits and costs).
- **Isolate memory.**
  - Release contexts stay an LRU of two (`brain.ts:430`), and the per-dataset constants are
    memoized.
  - The first version doesn't cache D1 rows across requests. If measured latency calls for
    it, add a byte-capped row LRU keyed `ds:table:key`, evicted through
    `registerBrainReleaseCacheEvictor` (`brain.ts:445-447`).
  - Today every whole file and every shard a request touches stays parsed until eviction
    (`brain.ts:516-556`). That is what breaks the 128 MB isolate at TauCeti scale.

### Limits and costs

- **Page limiters.** New rate-limit bindings next to `MCP_LIMITER`
  (`wiki/wrangler.jsonc:61-72`), applied only on cache misses and keyed by IP: 300 a minute
  for `cell`, `cells`, `labels`, `organs`, `level` and `synapse`; 60 for `search`; 20 for
  `explorer`. Step 0's per-endpoint D1 timings set the final numbers.
- **Concurrency gate.** In each isolate, at most 4 requests query D1 at the same time,
  counting page misses, agent API calls and MCP tool calls together (at most 2 explorer
  misses). Anything beyond that gets a 503 with `Retry-After: 1`, which the page retries once;
  MCP returns the same refusal as a tool error. Workers rate limits count per location
  (unchecked), so the limiters bound a single client and the gate bounds how much one isolate
  can queue on D1. Neither one bounds a client spread over many IPs.
- **Agent D1-cost budget.** The agent API and MCP keep today's 120 a minute per IP
  (`brain-api.ts:2500-2509`, `MCP_LIMITER`). The routes that cost the most D1 work (filter,
  which examines up to 10,000 rows; bridge, up to 4 chunks of 2,000 lookups; search;
  neighborhood) and their MCP tools also draw from a second per-IP limiter. Step 0's per-route
  D1 timings set its number.
- **Request bounds.** q is at most 200 characters. Id lists are at most the endpoint's cap and
  at most 8 KB of URL. Anything larger gets a 400 or 414.
- **`/api/version` stays off D1**, as `version.ts:2` promises.

### D1 limits that matter here

- **One query at a time per database** (unchecked). One long statement stalls reads for every
  release. So:
  - no file imports (`cli.js:238241`) and no exports (`cli.js:90322-90326`);
  - no statement over a whole table while serving: no `INSERT ... SELECT` over a table, no big
    `DELETE`, no FTS `optimize`, no `CREATE INDEX` on a populated table.
- **30 s per statement** (unchecked).
- **100 bound parameters per statement** (unchecked). Lists go in as one JSON parameter
  through `json_each`.
- **100 KB of statement text** (unchecked). This sets the importer's batch size.
- **2 MB per row or value** (unchecked). Explorer parts are at most 1 MB each. The largest
  cell page and supercell rows at TauCeti scale haven't been measured; #150 tests rows from
  90 KB to 1.5 MB.
- **1,000 queries per Worker invocation on Paid**, counting each statement in a batch
  (unchecked). The route that came closest, quickstatements, now resolves everything in one
  batch.
- **10 GB per database** on Workers Paid.
- **Billing by rows read and rows written** (unchecked). Index and FTS writes multiply the
  number of rows an import writes.
- **Latency.** Every request makes round trips to the database's region, which hasn't been
  measured (step 0). Read replicas (unchecked) would fail closed on lag, because every batch
  starts with the probe.

## 2. Readers and their replacements

**Release and errors**

| Reader | Replacement | Change |
|---|---|---|
| `resolveBrainRelease`, `brain.ts:607-671` | Unchanged; the context gains `backend` and `datasetId`. A new `resolveBrainReleaseByHex` accepts only the selector's current or previous release, for the page endpoints, and says which one it is, which sets the response's cache lifetime. | none |
| `parseReleaseManifest`, `REQUIRED_RUNTIME_ARTIFACTS`, `brain.ts:324-389`, `42-52` | Required files depend on the layout. A D1 release needs both serving artifacts, `cells/manifest.json`, `cells/frontier.json`, `cells/frontier_graph.json`, `sources.json` and `brain.html`. It must not carry the four whole files, the shards, the trace buckets or `xref_index.json`. | none |
| `brainAssetJson`, `requiredBrainAssetJson`, `brain.ts:516-602` | Used only for the small static files. D1 reads go through `brainBatch` with the same error class. | decision 15 |
| memos and evictors, `brain.ts:423-458` | The context LRU is unchanged, per-dataset constants are memoized, and rows aren't cached. | memory is bounded by what one request reads |
| `send`, `withRelease`, MCP `tools/call`, `brain-api.ts:2466-2494`, `mcp.ts:464-497` | Unchanged. | none |
| `GET /api/version`, `version.ts:26` | Unchanged, with no D1 read (`version.ts:2`). The canary gets the dataset id from page responses instead (section 7). | none |

**Atom reads**

| Reader | Replacement | Change |
|---|---|---|
| `snapshotFor`, `cellsManifest`, `brain-api.ts:304-317`, `386-390` | The slim static manifest; for D1 releases only `prov` and `_meta` are validated. | none |
| `cellEntry`, `brain-api.ts:394-406` | `cellPage`, with the same bytes as the shard entry (the builder serializes each entry as `build_cell_shards.py` does). | none |
| `atomFor` supercell branch and breadcrumb, `brain-api.ts:485-517`, `467-481` | `supercell`; a depth cap of 64 replaces the seen-set. | none on a tree |
| `atomIdForOrgan`, `brainNodeExists`, `brain-api.ts:638-646`, `brain.ts:716-719` | `organOwners` and `atomsExist`, behind the probe. | fails closed when rows are missing |
| `resolveAtomKey` alias passes, `brain-api.ts:568-584` | `aliasHits`. | none |
| `resolveAtomKey` label and supercell organ-label passes, `586-594`, `611-622` | `exactName`. | names are lowercased at build time in Python and the key per request in JS: identical for ASCII, rare differences elsewhere |
| `cellEntries`, `hydrateSupercellTraces`, `411-433`, `1037-1054` | `synapseTraces` for supercell rows. | Rows that say `traces_unavailable` today get traces: 1,413 whose partner's list was capped and 292 that join two supercells (`1032-1036`). They are trimmed to 6 per row (`build_cell_shards.py:94`). The sidecar always carries `tt` (`build_cell_shards.py:1244-1247`), while cell rows carry it only when trimmed (`896-897`), so these rows also gain `traces_total` (`brain-api.ts:1217`). |
| `suggestRename`, `brain-api.ts:1786-1804` | `aliasHits` for decls plus `cellPages`, batched across a call's dead names. | none |
| `cellFor`, `transferFor`, `neighborhoodFor`, `snippetsFor`, `brain-api.ts:731-1346` | No reads of their own. | release-bound cursors keep working (`1081`, `1351`); field names stay, including `withheld_by_shard` |
| Reference text: `brain-api.ts:2769-2771` and `mcp.ts:201-203` (traces "hydrated from the partner cells' shards"), `mcp.ts:14` ("no tool touches D1"), `mcp.ts:129-145` (label search only) | Rewritten in step 3b. | text |

**Scans and search**

| Reader | Replacement | Change |
|---|---|---|
| `searchLabels`, `suggestionsFor`, the free-text fallback, `brain.ts:696-712`, `brain-api.ts:683-688`, `846-854` | The name tiers. | exact matches rank above prefix matches; 2-character queries lose substring matches (`brain.ts:708`) |
| `searchFor`, `brain-api.ts:1512-1586` | The single search endpoint. | Text hits and index tags are new. A supercell prefix hit now ranks above a cell fragment hit; today all supercell hits come after all cell hits (`1574-1584`). Supercells also match on their whole path after `path:`, as the page's area search does (`build_brain_page.py:4524`). |
| `filterFor` cells, `brain-api.ts:1438-1442`, `1478-1489` | `filterCells`, paging on `labels.ord`. | short pages (decision 13) |
| `filterFor` `under=`, `1443-1476` | `cell_under`, read in ord order. | none |
| `filterFor` supercells, `1424-1437` | `filterSupercells`, paging on `supercells.ord`. | short pages |
| `partnerLabels`, `2238-2245` | `labelRows` for at most 12 ids (`2233`). | none |
| `bridgeFor` candidates, `atomsInStatement`, `2321-2338`, `2288-2300` | The name tiers limited to 5, then the statement pass. | same results within its bounds; `statement_scan` reports when it stops at one |

**Community edits** (the overlay itself stays in `wikilean`)

| Reader | Replacement | Change |
|---|---|---|
| `resolveNodeEndpoint`, `POST /api/brain/node`, `brain-edits.ts:264-276`, `1375-1405` | `atomsExist` and `organOwners`, behind the probe. | fails closed |
| quickstatements, `brain-edits.ts:692-733`, row loop `1169-1178` | One batch resolves every endpoint id (`organPages`, `atomsExist`) before the first write. | Required: row by row, a 100-row submit (`brain-edits.ts:50`) would go over the per-invocation cap. Side effect: an outage no longer leaves earlier rows written. |
| `getXrefIndex`, `getXrefPagesByNode`, `brain-edits.ts:92-174` | `xrefPages` and `xrefNodes`. | the two isolate caches and their evictor go away |

Unchanged, because they aren't Brain release data: the decl, suffix and premise indexes
(`brain-api.ts:1644-1662`, `decl.ts:106-120`).

**Release, replay, tests and local tools**

| Reader | Replacement | Change |
|---|---|---|
| `_static_closure`, `build_release.py:249-283` | For a D1 release, no shard or trace maps are required, and the expected static set is the D1 public files. | none |
| `brain-release-public.ts:31-61`, `469-475`; `authority_contracts.py:147-167`, `3575-3579` | Required sets depend on the layout. The "at least one generated cell or trace shard" rule applies only to the static layout. | none |
| `provenance_coverage.py:183-246`, and its caller `private_replay_gate.py:197-260` | Also walks `cell_pages`, `supercells` and `synapse_traces` in the pinned `serving.sqlite3`, using the manifest's prov pool, instead of skipping the opaque file (`183-187`). | none |
| `reducer-inputs-v2.json`, `-v3.json`, `-v3-folded.json` (in `brain/authority/`), `compile_offline_pack_v2.py:2279`, `reproducibility_gate.py:209-224` | Learn the D1 layout in step 6 (section 6). | none for static releases |
| `legacy_sqlite_projection.py:185`, `assemble_legacy_release.py:297-300` | Keep the static required set under a renamed constant, because they assemble static legacy releases. The assembler also freezes through `build_release.build_release` (`:398`) and calls its `_static_closure` (`:297`), so `build_release.py` chooses the layout per workspace: static when the workspace has no `brain/data/serving.sqlite3`, D1 otherwise. The assembler pins the freezer's bytes in its plan (`:44-46`, `:147`), so existing assembly plans are re-pinned in every step that changes `build_release.py`. | none |
| `site/test_frontier_page.py:79`, `100-107` | Rewritten in the cutover's page PR. It asserts `releaseJson("cells/manifest.json")` and tampers with `cells/labels.json`. It is release-affecting by exception (`brain_release_authority.py:59`) and a nightly gate (`brain-nightly.sh:786`). | test |
| `d1shim.ts:107` | `_batchExec` decides whether a statement returns rows by `stmt.columns().length > 0` (the same call `raw()` uses at `94`), so a CTE inside a batch returns rows. | test |
| `query.py` (used by `site/search_tools.py`), `bench/analysis/brain_artifact.py` | Read `serving.sqlite3` from the cutover on. `query.py` exits non-zero when the file is missing, instead of printing no hits. | local tools |

## 3. Client changes

Page endpoints, all `GET /api/brain/r/<hex>/...`:

| Endpoint | Replaces (in `build_brain_page.py`) | Backed by |
|---|---|---|
| `cell?key=` | `getEntry` `670-689`, `resolveId` `816-849` | strict `aliasHits` (organ, decl, slug; never labels), `cellPage` or `supercell`, `partnerRows` |
| `cells?ids=` (at most 200) | the level web's per-cell fetches `1563-1571` (at most 60, `1527`); "Build on these" `3622-3625`, `3697-3720` | `cellPages` |
| `labels?ids=` (at most 500) | the remaining `labelById` lookups `3753-3764` | `labelRows` |
| `organs?ids=` (at most 100) | `organInfo`, two fetches per id, `3307-3367` | `organPages`, trimmed to label, url, snippet and licence |
| `level?path=` | `ensureTree` and its consumers `712-815`, `1131-1200`, `1529-1577`, `4353-4441`. With an empty path it is the roots level for `libRoots` `903-905`, the top level `1136-1149`, the Libraries panel `2676` and `rootsPanel` `4021-4022` | `level` |
| `search?q=&limit=&text_limit=` | `ensureSearchIndex` and the search box handler `4505-4566` | the search endpoint, with the page fields |
| `explorer?scope=&part=` | `fetchExplorerData` `4589-4596`, scope and ripple `5298-5322` | `explorerScope`; a precomputed scope's parts are separate cacheable requests |
| `synapse?a=&b=` | `synBetween` `3736-3764`, trace buckets `3775-3830` | `cellPages`, `synapseTraces`, `labelRows` |

- Every response carries `release_id` and `dataset`.
- The page splits id lists so each URL stays under 8 KB. 48% of TauCeti cell ids are longer
  than 64 characters, so 500 ids in one URL would go over Cloudflare's URL limit (about 16 KB,
  unchecked).
- The routes sit under the reserved `api` prefix, so the slug catch-all route can't claim them
  (`index.ts:93-95`). Deep links such as the queue card's `/brain#<organ id>`
  (`queue.ts:138-141`) resolve through `cell?key=`.

What stays static:
- `current.json` and `release.json`, read and checked by `selectRelease` as today
  (`516-589`).
- These files, hash-checked by `releaseJson` (`590-599`):
  - the slim `cells/manifest.json`;
  - the new `cells/frontier.json`, for the frontier views (`1884-2508`, `4085-4282`);
  - `cells/frontier_graph.json` (`1834-1846`);
  - `sources.json` (`4458-4503`).

The two existing Worker calls keep their shape:
- `/api/brain/edges` (`5623-5630`), now backed by `xrefs`;
- the "Connect to" autocomplete on `/api/brain/search` (`5773-5801`), now the single search
  endpoint, called with `text_limit=0`.

Behavior:
- **Release check.** The page rejects any response whose `release_id` differs from
  `RELEASE_ID` (set at `585`). A 410 means the tab's release is no longer current or previous;
  the page says a newer release is live and offers a reload.
- **Folder counts** come from each child's `fhist` (cells per facet bits and libraries),
  computed in the page for the current filters, so toggling a filter chip re-renders without a
  request. Counts follow the builder's rule that a cell in several modules counts once per
  ancestor (`build_cell_shards.py:1060-1070`). The page used to count it once per module, so
  the few such cells change the visible numbers slightly.
- **Level web.** Child heads carry `syn` already filtered to partners on the same level, as
  `enrich` draws it (`1558-1561`). Cell-to-cell edges come from one `cells?ids=` call.
- **Grandchild preview.** The page sorts a folder's children by the filtered count and draws
  the top 24 (`1541-1543`). Each child head's `kids` carries up to 64 children with `n_sub` and
  `fhist`, so the page computes the filtered counts and sorts as today. A folder with more than
  64 children keeps only its 64 largest by unfiltered `n_sub`, so with filters on its preview
  can differ from today's.
- **Search box.**
  - Hits are typed cell, area or frontier.
  - The "via" note shows the matched name. The "hidden" badge is computed from `f` and `libs`,
    and "n of N" from `fhist`.
  - Areas whose subtree is empty stay hidden (`4518`); hot prefixes carry `live_sups` for
    this.
  - Area prefix hits still rank above cell fragment hits (`4537-4543`).
  - New: an exact tier comes first. Lost: substring matches for 2-character queries.
- **Frontier views** work from `cells/frontier.json`. The near folders' labels (`2433`,
  `4207-4218`) come from its `near_labels` field.
- **Boot** fetches the selector, `release.json`, the manifest, then one `level` (the roots, or
  the hash target). The home page embeds /brain (`home.ts:215`), so every landing visit stops
  downloading supercells and labels whole, which would be 84.9 MB at TauCeti scale.
- **The explorer** keeps its client logic: the visibility predicate, build positions as they
  are, and relaxation only under 4,000 cells (`4640`, `5388`). What's new:
  - module dots, kept or hidden by their library and their `fa` facet bits;
  - summed edges to module dots, which can't be clicked.

## 4. Serving database

The builder (`build_serving_db.py`, PR #153) writes these tables, without `ds`, into
`serving.sqlite3`, and the importer adds `ds`. The D1 schema is one migration,
`wiki/migrations-brain/0001_brain.sql`, applied by hand. It is applied with `--command`
carrying the file's text, never `--file`: on a remote database, `--file` goes through the
import API (`cli.js:238241`).

```sql
CREATE TABLE datasets(ds INTEGER PRIMARY KEY AUTOINCREMENT,
  dataset_id TEXT NOT NULL UNIQUE,     -- logical_root of brain/data/serving-tables.json
  schema_version INTEGER NOT NULL,     -- from the sidecar
  tables_json TEXT NOT NULL,           -- the sidecar's {table: {rows, digest, delete_bounds}}
  longest_name INTEGER NOT NULL,       -- longest label or aka, UTF-16 units (bridge)
  sup_names_from INTEGER NOT NULL,     -- first name_keys.seq that names a supercell
  names_mode TEXT,                     -- 'trigram' or 'parts', chosen by the importer
  state TEXT NOT NULL CHECK (state IN ('importing', 'ready', 'deleting')),
  imported_at TEXT);
CREATE TABLE releases(release_id TEXT PRIMARY KEY,
  seq INTEGER NOT NULL UNIQUE,         -- registration order; set again on re-registration
  ds INTEGER NOT NULL REFERENCES datasets(ds),
  manifest_sha256 TEXT NOT NULL,       -- information only: two manifests can share an id (version.ts:38-39)
  state TEXT NOT NULL CHECK (state IN ('candidate', 'superseded')),
  registered_at TEXT NOT NULL);
CREATE TABLE import_ranges(ds INTEGER NOT NULL, tbl TEXT NOT NULL, lo INTEGER NOT NULL,
  hi INTEGER NOT NULL, digest TEXT NOT NULL, verified_at TEXT NOT NULL, UNIQUE(ds, tbl, lo));

-- Every data table: ds, then seq = 1..N in the table's key order, UNIQUE(ds, seq).
CREATE TABLE cell_pages(ds INTEGER NOT NULL, seq INTEGER NOT NULL, id TEXT NOT NULL,
  json TEXT NOT NULL, UNIQUE(ds, seq), UNIQUE(ds, id));
CREATE TABLE aliases(ds INTEGER NOT NULL, seq INTEGER NOT NULL, kind TEXT NOT NULL,
  key TEXT NOT NULL, owner TEXT NOT NULL, UNIQUE(ds, seq), UNIQUE(ds, kind, key));
CREATE TABLE labels(ds INTEGER NOT NULL, seq INTEGER NOT NULL, ord INTEGER NOT NULL,
  id TEXT NOT NULL, label TEXT, aka TEXT, f INTEGER, p TEXT,
  libs TEXT,   -- library roots of all its folders, U+001F-joined; NULL when homeless
  xy TEXT,     -- build position, JSON text
  bub TEXT,    -- step 1b: module path when the layout put the cell in a bubble
  UNIQUE(ds, seq), UNIQUE(ds, ord), UNIQUE(ds, id));
CREATE INDEX labels_p ON labels(ds, p);
CREATE TABLE supercells(ds INTEGER NOT NULL, seq INTEGER NOT NULL, ord INTEGER NOT NULL,
  path TEXT NOT NULL, label TEXT,
  aka TEXT,         -- organ labels, U+001F-joined
  parent TEXT, fa INTEGER, frontier INTEGER,
  n_cells INTEGER,  -- direct cells
  n_sub INTEGER NOT NULL,  -- subtree cells, once per ancestor (build_cell_shards.py:1060-1070)
  fhist TEXT NOT NULL,     -- {"<f>|<libs>": cells} over the subtree
  kids TEXT,        -- up to 64 children, the largest by n_sub, each with n_sub and fhist
  syn TEXT,         -- the folder's synapse rows, without traces
  json TEXT NOT NULL, UNIQUE(ds, seq), UNIQUE(ds, path), UNIQUE(ds, ord));
CREATE INDEX supercells_parent ON supercells(ds, parent, ord);
CREATE TABLE name_keys(ds INTEGER NOT NULL, seq INTEGER NOT NULL, name_lc TEXT NOT NULL,
  kind TEXT NOT NULL,  -- label, aka (cells); slabel, spath, sorgan (supercells)
  owner TEXT NOT NULL,
  len16 INTEGER NOT NULL,  -- length of name_lc in UTF-16 units
  -- serving.sqlite3 also has name TEXT NOT NULL (original case, for the FTS fill); D1 omits it
  UNIQUE(ds, seq), UNIQUE(ds, name_lc, kind, owner));
CREATE TABLE hot_prefixes(ds INTEGER NOT NULL, seq INTEGER NOT NULL, prefix TEXT NOT NULL,
  cells TEXT NOT NULL, sups TEXT NOT NULL,  -- JSON arrays of up to 100 name_keys.seq each
  live_sups TEXT NOT NULL,                  -- the same for supercells whose subtree holds a cell
  UNIQUE(ds, seq), UNIQUE(ds, prefix));
CREATE TABLE cell_under(ds INTEGER NOT NULL, seq INTEGER NOT NULL, anc TEXT NOT NULL,
  ord INTEGER NOT NULL, UNIQUE(ds, seq), UNIQUE(ds, anc, ord));
CREATE TABLE synapse_traces(ds INTEGER NOT NULL, seq INTEGER NOT NULL, pair TEXT NOT NULL,
  json TEXT NOT NULL, UNIQUE(ds, seq), UNIQUE(ds, pair));
CREATE TABLE xrefs(ds INTEGER NOT NULL, seq INTEGER NOT NULL, page_ord INTEGER NOT NULL,
  node_ord INTEGER NOT NULL, page TEXT NOT NULL, node TEXT NOT NULL,
  UNIQUE(ds, seq), UNIQUE(ds, page_ord, node_ord));
CREATE INDEX xrefs_node ON xrefs(ds, node, page_ord);   -- xrefPages reads in page_ord order, no sort
CREATE INDEX xrefs_page ON xrefs(ds, page, node_ord);
CREATE TABLE text_ids(ds INTEGER NOT NULL, seq INTEGER NOT NULL, node TEXT NOT NULL,
  owner TEXT NOT NULL,
  -- serving.sqlite3 also has body TEXT NOT NULL: label, docstring and description from
  -- nodes.jsonl, for the FTS fill; D1 omits it
  UNIQUE(ds, seq), UNIQUE(ds, node));
-- Step 1b, after PR #137:
CREATE TABLE explorer_edges(ds INTEGER NOT NULL, seq INTEGER NOT NULL, a TEXT NOT NULL,
  b TEXT NOT NULL, w INTEGER NOT NULL, UNIQUE(ds, seq), UNIQUE(ds, a, b));
CREATE INDEX explorer_edges_b ON explorer_edges(ds, b);
CREATE TABLE explorer_scopes(ds INTEGER NOT NULL, seq INTEGER NOT NULL, scope TEXT NOT NULL,
  part INTEGER NOT NULL, json TEXT NOT NULL, UNIQUE(ds, seq), UNIQUE(ds, scope, part));
CREATE TABLE bubbles(ds INTEGER NOT NULL, seq INTEGER NOT NULL, path TEXT NOT NULL,
  disc TEXT NOT NULL,  -- [x, y, r], JSON text
  n INTEGER NOT NULL, fa INTEGER, libs TEXT, UNIQUE(ds, seq), UNIQUE(ds, path));
-- Per dataset, created and filled by the importer, dropped by the pruner:
--   CREATE VIRTUAL TABLE names_<hex> USING fts5(name, content='', contentless_delete=1,
--       tokenize='trigram');                                 rowid = name_keys.seq
--   CREATE VIRTUAL TABLE texts_<hex> USING fts5(body, content='', contentless_delete=1);
--                                                            rowid = text_ids.seq
```

The local file also has two tables the importer doesn't copy: `tables` (each data table's row
count, digest and delete bounds, as the sidecar has them) and `meta` (the schema version, the
digests of the input files, and Python's Unicode version, below).

Notes on the tables:
- **`seq`.** Every data table numbers its rows 1..N in its key order, which is the order the
  digest reads them. The importer writes, reads back and deletes by `seq` ranges, and the
  natural keys stay UNIQUE, so a retried batch can't duplicate rows. `name_keys` is
  deduplicated at build time, keeping the first spelling: aka is deduplicated only by exact
  string (`build_cell_shards.py:1021-1029`), so "Vector Space" and "vector space" would
  otherwise produce two identical lowercased rows.
- **`hot_prefixes`** lists only prefixes of 2 to 200 UTF-16 units, the query lengths the
  Worker accepts.
- **`text_ids`** holds every node with a docstring or description whose organ id resolves to a
  cell, or that is a supercell itself. Its owners come in labels order, then supercell order.
- **Explorer per scope** (step 1b).
  - The scopes are each `path:` supercell plus `all` (`build_brain_page.py:5298`). The core is
    the cells whose `p` is in the subtree, and the ripple adds their neighbors (`5310-5316`),
    as today.
  - The builder records each scope's size. A scope of at most 2,000 cells (core plus ripple)
    and at most 20,000 edges is computed live. Larger scopes, and `all`, are precomputed into
    `explorer_scopes`, using today's payload shape plus `bubbles` and `libs`, in parts of at
    most 1 MB.
  - Each precomputed scope is at most 8 MB. Above that, the builder collapses more bubbled
    cells into module dots until the scope fits, and the build fails if it still doesn't.
    Nested scopes each store their own copy, so step 0 records the total `explorer_scopes`
    bytes per dataset.
  - In every scope, ripple cells that have `bub` set collapse into their module's dot. The
    dot's center and radius come from `bubbles`, and edges to its members are summed per dot.
  - A precomputed scope of more than 21,000 cells (what the explorer draws today,
    `build_brain_page.py:4597`) also collapses its own bubbled core cells.
  - Today's data has no bubbles, so today's views don't change.
  - Limiting the ripple to cells without `p` instead would drop the neighbors from other
    folders that today's views show.
  - `bub` and `bubbles` need the layout to record each bubbled cell's module and the discs
    that `place_bubbles` returns. The layout in PR #137 knows each cell's module but records
    neither, and it drops the discs.
- **Traces.**
  - `synapse_traces` holds exactly the sidecar's per-pair values
    (`build_cell_shards.py:1231-1248`), keyed `src|dst` with `src < dst`.
  - Cell-to-cell pairs that both cells' caps dropped stay unshipped, as today
    (`build_brain_page.py:3940-3943`).
  - The build fails when the synapse and cell prov tables differ. Today it only warns and
    ships `traces.prov` (`build_cell_shards.py:1279-1284`, `1305`); PR #153 changes that, and
    the serving build also refuses a manifest with `traces.prov`. So every trace's `prov`
    index points into the manifest's `prov`, which is what the agent API documents
    (`brain-api.ts:2807`) and what the page uses (`build_brain_page.py:3828-3829`).
- **xref.** Built from `xref_index.json` in file order. A node listed twice under one page
  keeps its first place, as `brain-edits.ts` deduplicates it today. Moving it removes the
  Worker's last whole-file Brain read and the inversion it builds in each isolate
  (`brain-edits.ts:151-174`).
- **Search.**
  - `names_<hex>` and `texts_<hex>` are contentless with `contentless_delete=1` (needs SQLite
    3.43 or newer; the builder refuses older versions, and #150 checks D1). So they store only
    the index and can be repaired by rowid range. The prototype's name and docstring indexes
    measured 48 MB and 116 MB.
  - If D1 has no trigram tokenizer, the importer instead creates `names_<hex>` as
    `fts5(parts, content='', contentless_delete=1, prefix='2 3')`, filled with each name split
    at capitals, dots, underscores and slashes, and records `names_mode = 'parts'`. Word-start
    fragments still match (`Semigroup` finds `StronglyContinuousSemigroup`); mid-word fragments
    don't.
  - The builder builds the same FTS tables locally (as `names` and `texts`, trigram only), for
    local tools and for the importer's checks. The FTS tables themselves aren't digested, so
    the trigram-or-parts choice never changes a dataset id. Their inputs are:
    `name_keys.name` (original case) and `text_ids.body` are columns of digested tables, so
    the dataset id binds every FTS input, and a change to a node's description changes the id.
  - D1 omits those two columns, which are used only to build its FTS tables. The importer
    fills the FTS tables from the local file. Range verification compares the copied columns
    of local and remote rows; the sidecar's digest covers every column of the local table.
- **Digest rule.** Numbers are stored only in INTEGER columns; floats stay inside JSON text
  (`xy`, `disc`, page JSON), and the builder fails on a REAL value. Otherwise a REAL such as
  12.0 can come back from D1's JSON as 12 and change the canonical JSON that
  `store.digest_rows` hashes (`store.py:41-56`).
- **Unicode.** `name_lc` is Python's `str.lower()`, so the digests hold only under one version
  of Python's Unicode database. `meta` records it, and step 4's verification checks it.
- **Sidecar.** Its shape is `{"schema", "schema_version", "tables": {name: {"rows", "digest",
  "delete_bounds"}}, "longest_name", "sup_names_from", "aliases_counts"}`.
  - Every table is digested over every column of the local table.
  - `delete_bounds` lists the `seq` values at which the table's cumulative row bytes cross
    each multiple of 4 MB. `datasets.tables_json` carries them, so the pruner sizes every
    delete by bytes, even after a dataset's `import_ranges` rows are gone. The bytes are
    measured on the local rows, which for `name_keys` and `text_ids` include the columns D1
    omits, so D1 deletes come out smaller, never larger.
  - `aliases_counts` carries the `_meta` counts of `aliases.json`, which the shard tests check
    and a D1 release no longer ships.
  - It holds no generation stamp and no release id, so identical tables always produce the
    identical dataset id.

**Schema rule: additive only.**
- Each dataset records its `schema_version`: the builder's `SCHEMA_VERSION`, passed through
  the sidecar. The Worker has a fixed set of versions it can read, and the probe fails closed
  (503) on any other.
- A migration to `wikilean-brain` may only add tables and add nullable columns.
  `ALTER TABLE ... ADD COLUMN` changes only the schema, so it runs immediately whatever the
  table size. Such a migration runs once and is verified afterwards, as `CLAUDE.md:134-137`
  requires.
- A migration never creates an index on a populated table, changes or drops a column in use,
  or backfills data. Each of those is one long statement on a shared database.
- A change that needs one of those adds a new table instead, together with a new
  `schema_version` whose datasets use it. The old table is dropped once no dataset of the old
  version remains, when it's already empty.
- The importer refuses a sidecar whose `schema_version` the applied migration doesn't
  support. The promoter checks the migration by commit, as it does for `wikilean`
  (`brain_promote_release.py:76-122`, `2091-2118`).
- Rollback stays safe: a Worker only ever runs with the selector it was deployed with, so an
  older Worker only meets datasets it already knows how to read.
- In the other direction, a Worker serves its selector's previous release, so it must read
  that dataset's `schema_version` too. This is part of the cross-release contract (section 1,
  Caching).

Static files, not tables:
- **`cells/frontier.json`** (new, written by `build_cell_shards.py`):
  - Shape: `{"_meta": {"generated_at"}, "areas": {"frontier:<Area>": <today's row>},
    "near_labels": {"path:...": "<label>"}, "cells": {"<id>": {"label", "aka"?, "f"?}},
    "unplaced": [...]}`.
  - About 241 KB on today's data: 116,256 bytes of frontier rows plus 124,740 bytes of their
    label rows.
  - TauCeti adds formal cells, not homeless ones, so it should stay small (assumed, not
    measured).
- **`sources.json` and `cells/frontier_graph.json`:** unchanged (the latter is 154,798 bytes
  today).
- **`cells/manifest.json`** for a D1 release drops `scheme`, `shards` and `traces`
  (`build_cell_shards.py:1343-1345`, `1369`, `1373`). It keeps `_meta`, `prov` and `roots`.

## 5. Import, prune and the promoter

**What the release binds.**
- `brain/data/serving.sqlite3` as an opaque artifact, its bytes pinned by sha256. The bytes
  depend on the SQLite version; the logical digests don't.
- `brain/data/serving-tables.json` as a JSON artifact. Its `logical_root` is the dataset id.
  The release id hashes the artifact list (`brain.ts:170-177`), so the release id binds the
  dataset.
- The public files of a D1 release are `cells/manifest.json`, `cells/frontier.json`,
  `cells/frontier_graph.json`, `sources.json` and `brain.html`. The four whole files, the
  shards, the trace buckets and `xref_index.json` leave the release.
- The layout rule (a release that lists the sidecar is a D1 release) is applied in:
  - `authority_contracts.py:147-167` and `3575-3579`;
  - `brain.ts:42-52` and `383`;
  - `brain-release-public.ts:31-61` and `473-475`;
  - `build_release.py:249-283`;
  - the activation bundle's public mapping (`brain_activation_bundle.py:489-496`,
    `1148-1149`);
  - the canary (`brain-canary.py:219-220`, `440-470`, `563-587`).
- The static layout's rules stay, for the retained previous release and old baselines
  (`brain_promote_release.py:2713`; `brain_activation_bundle.py:2650-2661`) and for the legacy
  tools.

**Verification when the release is frozen.** `verify_release_files` opens `serving.sqlite3`
read-only and immutable, the same way it opens `brain.sqlite3`
(`authority_contracts.py:4916-4942`). Then it:
- runs SQLite's integrity check and compares the exact schema;
- recomputes each table's digest from its rows and compares it with both the `tables` table
  and the sidecar, and checks `meta`'s Unicode version against the verifier's;
- re-derives only the base tables from `brain.sqlite3` (`cell_pages`, `aliases`, `supercells`
  apart from `fhist` and `kids`, `labels` apart from `libs`, `xy` and `bub`, `synapse_traces`
  and `xrefs`), the way it re-derives the static projections today
  (`authority_contracts.py:4284-4412`), streaming with the serving file attached instead of
  holding every node in memory (`4157-4181`);
- checks the derived tables (`name_keys`, `hot_prefixes`, `cell_under`, `fhist`, `kids`,
  `text_ids` and the explorer tables) by digest plus these invariants, so it doesn't carry a
  second copy of the builder's newest logic:
  - `name_keys`: every owner is a label id or a supercell path, `name_lc` is the lowercase of
    `name`, `len16` is the length of `name_lc` in UTF-16 units, and every label, aka and
    supercell label has its row;
  - `hot_prefixes`: every listed seq is a `name_keys` row starting with the prefix, in seq
    order, at most 100 per list, and every `live_sups` owner's subtree holds a cell;
  - `cell_under`: every `anc` is a `/`-prefix of a folder that lists the cell, or of its `p`;
  - `fhist` and `kids`: a supercell's `fhist` counts sum to its `n_sub`, and every `kids`
    entry is one of its children, with that child's `n_sub` and `fhist`, at most 64 of them;
  - `text_ids`: every node exists in `brain.sqlite3` and every owner in `cell_pages` or
    `supercells`;
  - explorer tables: every edge endpoint and scope node is a label id, every `bub` is a
    `bubbles` path, and every part parses, is at most 1 MB, and its scope is at most 8 MB.
- refuses a manifest whose traces carry their own `prov`;
- writes a verification receipt for the release, keyed by the manifest's sha256 and recording
  the verifier's commit. A later verification of the same manifest by the same verifier (the
  promoter's dry run and execute, the activation bundle's baseline check, the importer) checks
  the receipt and the pinned file hashes instead of repeating the full pass.

This adds a full pass over a 1-1.6 GB file, where the 900 s per-command timeout is already
expected to trip. So:
- verification gets its own timeout, separate from `command_timeout` (which also bounds the
  deploy and the reconcile fence), set from step 0's measured verify time before the first D1
  release;
- `inventory_tree` and `_scan_regular_tree` hash files in chunks instead of reading each one
  whole (`brain_promote_release.py:439`, `brain_activation_bundle.py:521`).

The intermediate files move out of `site/assets/brain/cells`, because the static file check
rejects extra files there (`build_release.py:264-283`).

**Import.** A new script, `site/ops/brain_d1_import.py`, run on the operator Mac with the
promoter's wrangler environment (`brain_promote_release.py:623-650`):

1. **Lock.** Take the promoter's `PromotionLock` on the same receipt root
   (`brain_deploy_journal.py:219-277`). The lock is nonblocking, so the importer fails fast
   while the promoter runs. Refuse if any promotion attempt is incomplete, as the promoter
   does (`brain_promote_release.py:4435-4448`).
2. **Check.** Verify the frozen release (`verify_release.py`, or its receipt) and read the
   sidecar. Refuse if the migration isn't applied or the `schema_version` is unsupported. The
   migration check is read-only, like `_require_d1_schema` (`brain_promote_release.py:2091-2118`).
3. **Register.**
   - If the dataset's row is in `deleting` (an interrupted prune), first finish that delete
     with the pruner's own steps, then import the dataset fresh. Pruning deletes a dataset's
     `import_ranges` before its rows, so no journal row survives for deleted data.
   - Insert the dataset row as `importing` if it is absent. Insert the release row as
     `candidate` with a new `seq`, or set a superseded row back to `candidate` with a new
     `seq`. Rows are keyed by `release_id`; `manifest_sha256` is recorded for information
     only.
   - Refuse if another protected candidate references a different dataset; the operator
     supersedes it first. So there is at most one candidate dataset (Pruning).
   - From this point the release is protected, and with it its dataset. The prune in step 4
     therefore can't delete a dataset this release is about to reuse.
4. **Prune** (below).
5. **Skip** if the dataset is already `ready`: there is nothing to import.
6. **Copy each table in `seq` ranges**, each range's statement text under about 90 KB, a few
   ranges per `wrangler d1 execute wikilean-brain --remote --json --command` call. That
   command posts to the query API (`cli.js:238337-238342`), not the import API that blocks the
   database (`cli.js:238241`).
   - Each range is written as `DELETE FROM t WHERE ds = ? AND seq BETWEEN lo AND hi;` followed
     by one multi-row `INSERT`. Rerunning a range after a failed or ambiguous call therefore
     repairs it, and nothing uses `INSERT OR IGNORE`.
   - A row too long for one statement is written as its first chunk, then as appends of the
     form `UPDATE t SET json = json || '<chunk>' WHERE ds = ? AND seq = ? AND length(json) =
     <prefix length>`. A retried append matches no row and changes nothing.
   - After a range's last write, the importer reads it back (`SELECT ... WHERE ds = ? AND seq
     BETWEEN lo AND hi ORDER BY seq`), hashes it with `store.digest_rows`, compares the hash
     with the same range of the local file, and then records the range in `import_ranges`.
     Both hashes cover the copied columns only (`name_keys` without `name`, `text_ids` without
     `body`). A mismatch rewrites that range.
   - A resumed import starts from the recorded coverage: it reads each table's
     `import_ranges` and copies only the `seq` gaps between recorded ranges, planning new
     ranges inside those gaps. It never re-plans a recorded range, so a changed range size
     can't collide with `UNIQUE(ds, tbl, lo)`.
   - Each call reports `duration`, `rows_written` and `size_after`. The importer slows down
     when `duration` grows and stops before a size ceiling.
7. **Build the FTS tables.** Create `names_<hex>` and `texts_<hex>`, set `automerge` to 0 and
   raise `crisismerge` on each, so no fill `INSERT` carries a merge. Fill them from the local
   file's `name_keys.name` and `text_ids.body`, by rowid range in the same way (DELETE by
   rowid range, then INSERT, journaled). Merge index segments only in explicit small
   `('merge', N)` steps, never one `optimize`. Verify each range's row count through the FTS
   table's `_docsize` shadow table, and check that 30 sampled queries return the same results
   as the local file in the same `names_mode`.
8. **Finish.** Check that every table's recorded ranges cover exactly 1..rows, so that their
   summed lengths equal the sidecar's row count; no `count(*)` runs over a table. Then run
   `UPDATE datasets SET state = 'ready', imported_at = ? WHERE ds = ? AND state = 'importing'`
   and check that it changed exactly one row. Zero changes means the dataset left `importing`
   under the importer, and the import fails.

Readers see none of this: no selector names the release yet, and the probe hides the dataset
until it is ready. The import stays out of the promoter's dry run, which must not change
anything (`brain_promote_release.py:4479`, `brain_activation_bundle.py:1653-1654`). It also
stays out of `_deploy`'s fenced window (`brain_promote_release.py:3582-3713`), so the deploy
stays short. If #150 shows that file imports don't block reads, importing whole tables as
files becomes an option; the batched path stays the default.

**Scratch mode.** `import --scratch <serving.sqlite3> <serving-tables.json>` takes a serving
file and its sidecar with no frozen release, for the trial import in step 2, before any
validator or `build_release.py` knows the D1 layout.
- In place of the release verification, it recomputes every table's digest from the file and
  checks them, and the logical root, against the sidecar.
- It registers the dataset under a candidate release row named `scratch:<dataset id>`, so
  pruning protects it like any candidate. The operator removes it afterwards with `supersede`
  and `prune`.
- Everything else (lock, register before prune, ranges, journal, FTS, finish) runs as above.

Two more commands:
- `supersede <release_id>` marks a candidate `superseded`. It takes the same lock. It refuses
  the live current and previous releases.
- `verify --sample N` is read-only and takes no lock. It re-reads N random journaled ranges of
  the live datasets and compares their copied-column digests with the same ranges of the
  release's local serving file and with `import_ranges`. The promoter calls it in-process
  after the canary, and the operator Mac's nightly runs it too. A mismatch after a passed
  canary is journaled like a failed canary: manual intervention required. A mismatch in the
  nightly fails the nightly run.

**Pruning.** The `prune` command, which also runs as step 4 of every import, after the import
has registered its release. It takes the same lock and refuses while any promotion attempt is
incomplete. It protects these releases:
- the current and previous releases named by the live selector, fetched the way the
  promoter's fence fetches it;
- the current and previous releases named by the staged selector and by the predeploy
  selector in the most recent attempt's intent (`brain_promote_release.py:3367-3401`), until
  that attempt's journal reaches a terminal outcome (`final_state`,
  `brain_deploy_journal.py:32`, `891-892`). After that the live selector is authoritative.
  Because import and prune already refuse while any attempt is incomplete, in practice they
  always protect from the live selector; this rule matters only if that refusal is ever
  relaxed;
- every `candidate` registered after the live current, meaning a larger `seq`; while the live
  current is a static release, that is every candidate. A candidate stays protected until
  someone runs `supersede` or a newer release goes live;
- every candidate whose dataset is still `importing`, until it is superseded.

A dataset is protected when any protected release references it. Import step 3 allows only
one candidate dataset, so at most three datasets are protected at once: the live current's,
the live previous's and one candidate's, fewer when they share. Step 2's remote import gate
requires three full footprints to fit under the 10 GB cap with room to spare.

Pruning then:
1. deletes the release rows that aren't protected;
2. for each dataset that no release row references, including one whose import was abandoned
   with `supersede`:
   1. sets `state = 'deleting'`, so the probe hides it;
   2. deletes its `import_ranges` rows, before any of its data;
   3. drops its two FTS tables;
   4. deletes its rows table by table, in `seq` ranges sized by bytes: each statement deletes
      at most about 4 MB of row data, using the table's `delete_bounds` in
      `datasets.tables_json`;
   5. deletes its `datasets` row last.

An interrupted prune therefore leaves a `deleting` dataset with no journal rows. The next
prune finishes it, or, if a new release needs the same dataset, import step 3 does.

The promoter holds the same lock for its whole run and checks readiness inside its final
fence, so no prune can land between that check and the deploy. Dropping a large FTS table is
still one statement (the prototype's indexes were 48 MB and 116 MB). Step 0 times it, and
prune runs off-peak.

**The promoter.**
- Two read-only checks run in prepare and in the final fence
  (`brain_promote_release.py:3645-3652`):
  - the candidate's release row exists, is `candidate`, and points at a `ready` dataset whose
    id is the one its `release.json` binds;
  - the same holds for the retained previous release when that release is a D1 release.
- After the deploy, the canary checks the D1 endpoints, and the promoter then calls
  `verify --sample` in-process on the new current and previous datasets. It is read-only and
  takes no lock, so the lock the promoter holds is no obstacle.
- A failed canary still requires manual intervention (`brain_promote_release.py:3803-3829`).
  A verify mismatch after a passed canary is journaled the same way.
- Switching the selector is unchanged: the deploy carries `current.json`.

**Rollback.** `wrangler rollback` stays the only lever (`docs/BRAIN-RELEASE-RUNBOOK.md:696-731`).
It restores the previous Worker version and its selector. The previous release's dataset is
protected, so it serves immediately. Responses the newer Worker computed for that release were
cached for at most 300 seconds and under the newer Worker's version id, so the restored Worker
serves its own output. Rolling back further works only while that release's dataset is still
present, which is more likely now that releases share datasets. Otherwise the release answers
503 until it is imported again.

**Keep `wikilean-brain` out of the daily backup.** Exports also make a database unavailable
(`cli.js:90322-90326`), and the rows can be rebuilt. The runbook adds one rule: only the
importer and the pruner write to this database.

**Cost to measure.** Each insert writes the row plus one row per index (unchecked), and FTS
writes its own internal rows. Record `rows_written` per import; the earlier estimate of
writes per release was made before these indexes existed. Content addressing removes the
import entirely for releases whose tables are unchanged.

## 6. Offline replay

Jack's decision (2026-11-08, decision 7): the replay tools learn the D1 layout in step 6,
before the cutover in step 7. So the first D1 release can be a full offline replay release,
like the 2026-10-04 release (`docs/BRAIN-ACTIVATION-2026-10-04.md:29`). Both profiles stay
publishable (`docs/BRAIN-RELEASE-RUNBOOK.md:29-33`).

Why before the cutover:
- After the cutover the D1 layout is the only layout main produces, so there is no window
  without a replay path.
- Replay hazards surface before the first D1 release binds them. The raw bytes of
  `serving.sqlite3` depend on the SQLite version, and its digests on Python's Unicode version.
- The TauCeti release, the largest data change so far, gets full replay evidence.

So `build_release.py` needs no refusal of replay releases in the D1 layout. Its profile switch
(`build_release.py:528-530`) works in both layouts once step 4 teaches it the D1 closure.

Step 6 consists of:
- a new replay inventory version, after `reducer-inputs-v3-folded.json`, with a `serving-db`
  stage that outputs `brain/data/serving.sqlite3` and `serving-tables.json`;
- the `cell-shards` stage's outputs split into the public files and the intermediates. The
  `build_cell_shards.py` output this needs (intermediates outside the public tree,
  `cells/frontier.json` with `near_labels`, the slim manifest) lands here as a mode the new
  inventory selects; step 7 makes it the nightly's default;
- `compile_offline_pack_v2.py` (`:2279`), `build_replay_release.py`, the reproducibility gate
  (`reproducibility_gate.py:209-224`, which today requires `REQUIRED_RELEASE_PATHS` and the
  static closure) and the private replay gate moved onto the D1 layout. The reproducibility
  gate shows that two clean-room builds agree on the sidecar's logical root and every table
  digest, and states what it requires of the raw file bytes;
- the older inventories (`reducer-inputs-v2.json`, `-v3.json`, `-v3-folded.json`) kept, so the
  static releases they describe stay checkable.

Provenance coverage over the serving rows lands earlier, in step 4, because it is also run by
hand (`check_provenance_coverage.py`).

## 7. Transition, tests and canary

**Running old and new side by side.**
- The Worker reads the layout from `release.json` and sends each reader to either a
  `StaticStore` (today's code, moved behind an interface) or the `D1Store`. A static release
  therefore still works on a Worker that has the D1 code, which keeps every PR releasable.
- `brain.html` is frozen into each release (the promoter requires the staged page to equal the
  candidate's, `brain_promote_release.py:2449-2451`), and the page switch merges in the same
  cutover as the builder change. So every release's page matches its own data.
- At the first D1 promotion, the selector's previous release is the last static one. The
  deploy still stages its static files, and a rollback lands on the static Worker version.
- After that promotion, `StaticStore`, the shard routing, the whole-file memos and their tests
  are deleted.

**Worker tests.**
- **Fixture.** `installBrainFixture` (`brain-fixture.ts:757-801`) adds
  `env.BRAIN_DB = makeD1(new DatabaseSync(":memory:"))` (`d1shim.ts:121`), created from the
  migration file and seeded with `datasets` and `releases` rows plus the fixture's cells,
  labels, aliases and supercells. A second install adds a second release to the same database,
  sometimes sharing its dataset, for rollover, cursor-rejection and rollback tests.
- **Fault knobs** become D1 faults: a dataset stuck in `importing`, an unknown
  `schema_version`, a dataset id that differs from `release.json`, and a hook that throws on
  statements touching one table.
- **Shim fix.** `_batchExec` decides by `columns()`, and a test runs a CTE inside a batch.
- **New tests:**
  - no query crosses datasets;
  - an importing dataset is never served;
  - an unknown `schema_version` answers 503;
  - two releases sharing one dataset both serve;
  - text hits and 2-character queries;
  - a non-ASCII label;
  - the statement pass on a long statement;
  - the community-edit check answers 503, never "create a node", when rows are missing;
  - a query-plan check: a test runs `EXPLAIN QUERY PLAN` on every statement `brain-db.ts` can
    send and fails on a full `SCAN` of a shared table (`json_each`, CTEs and FTS MATCH are
    allowed). It also flags every `USE TEMP B-TREE` whose input has no bound (a LIMIT under
    it, a capped `json_each` list or a builder ceiling the test names);
  - a current-release response is cached `immutable`, a previous-release response
    `max-age=300`, and the Cache API key carries the Worker version id;
  - the concurrency gate counts agent API and MCP calls with page misses, and the agent
    D1-cost limiter refuses past its budget.
- **Probe.** A vitest test fails loudly if `node:sqlite` lacks FTS5 trigram support or
  `contentless_delete` (unverified on CI's Node 22).
- **`first()`.** The shim's `first()` throws (`d1shim.ts:101-103`), so the module uses
  `all()`.
- **Old tests.** Tests that pin whole-file caching keep running against static fixtures until
  `StaticStore` is removed: `wiki/test/brain-api.test.ts:140-151`, `280-312`, `433-439`;
  `wiki/test/brain-edges.test.ts:664-692`.

**Other tests.**
- **Importer (Python).** The importer runs against a local SQLite database standing in for D1,
  with a fake runner that executes each generated `--command`. Covered:
  - batching;
  - a call that was applied but reported as failed;
  - DELETE-then-INSERT repair;
  - conditional appends;
  - resuming from the journal, from recorded coverage after the range plan changes;
  - the protected set: refusal while an attempt is incomplete, attempt selectors protecting
    only until the attempt's terminal outcome, a candidate newer than the live current,
    refusal of a second candidate dataset, `supersede`, and a shared dataset kept while it is
    still referenced;
  - register before prune: superseding a candidate and importing a fix with identical tables
    keeps the dataset and imports nothing;
  - an interrupted prune at each of its steps, then a prune or an import of the same dataset
    that finishes it;
  - a final `UPDATE` that changes no row fails the import;
  - `verify` taking no lock, so it runs while the lock is held;
  - scratch mode with a serving file and sidecar and no frozen release;
  - refusal of an unsupported schema version.
- **Builder (`brain/test_build_serving_db.py`, in PR #153).** It covers `seq`, the per-table
  digests (including `name_keys.name` and `text_ids.body`) and `delete_bounds`; the sidecar's
  id staying the same across rebuilds of the same tables; hot-prefix completeness; `cell_under`
  against today's `under` test on the fixture; `spath` rows against the page's area-search
  hay; `kids` holding up to 64 children; and the build failing on prov skew. Step 1b adds the
  8 MB cap on every precomputed explorer scope.
- **End-to-end.** `wiki/e2e/server.ts:151-168` serves `/assets/brain/*` from the fixture. It
  must also route `/api/brain/r/*` to the app, using an env with `BRAIN_DB`.
  `wiki/e2e/brain.spec.ts:28-32` keeps its three static reads and adds a check that the first
  endpoint call returns the release id.

**Canary** (`brain-canary.py`).
- The selector, manifest and static-file checks stay, including the byte comparison between
  the immutable and mutable copies of the files that stay static.
- For D1 releases, the shard check (`440-470`) and the big-file checks (`563-587`) become
  endpoint checks. Each requires the expected `release_id`, and a `dataset` equal to the
  sidecar root in `release.json`, a check that also works from
  `.github/workflows/brain-canary.yml`:
  - `cell` for the first hit of the existing filter check (`506-561`);
  - `search`, with name hits, text hits and their tags;
  - `level` for the roots and for one folder;
  - `explorer?scope=all`;
  - `synapse`.
- The promoter then calls `verify --sample` in-process; a mismatch is journaled like a failed
  canary.
- The 32 MiB response cap stays (`brain-canary.py:54`). A D1 explorer response is one part of
  at most 1 MB, and a whole scope is at most 8 MB.

## 8. Steps

The numbering matches issue #152. [affecting] means release-affecting under
`brain_release_authority.py:28-91`. Such a PR merges only when no built release is waiting to
be promoted. [neutral] PRs can land any time.

- **Layout at TauCeti scale** [affecting], PR #137: grid-bucketed repulsion and module
  bubbles.
0. **Operator, no merge.**
   - Run #150 on the operator Mac, adding the cases the probe lacks:
     - reads while a stream of `--command` batches is writing (the probe only tests a file
       import);
     - `contentless_delete=1` and `_docsize` row counts;
     - how long it takes to drop a 100 MB FTS table;
     - window functions;
     - whether D1 accepts the FTS5 `automerge`, `crisismerge` and `merge` statements.
   - Then run a full-size measurement build on a separate Linux build machine: build the full
     serving file, import it into a local D1 (`wrangler d1 execute --local`), and record:
     - sizes, the largest rows and the largest folder;
     - `rows_written`, the import's wall time and its call count;
     - the verify time;
     - query times for worst-case prefixes (`ta`, `tau`, `tauceti.`) and fragments,
       adversarial fragment phrases (common trigrams that are never adjacent), long OR text
       queries (8 common words, where bm25 walks each term's whole doclist), parts-mode
       prefixes of 4 or more characters, and under concurrent page load;
     - the number and bytes of precomputed explorer scopes at the live cap, and the total
       `explorer_scopes` bytes per dataset;
     - per-route D1 time for the agent API and MCP, which sets the agent D1-cost budget;
     - the row counts of `hot_prefixes` and `cell_under`.
   - Go ahead if trigram works or the fallback is chosen, the rows fit, reads stay unaffected,
     and the bounded statements answer in the tens of milliseconds.
1. **Serving database builder** [affecting], PR #153.
   - Section 4's tables apart from the explorer tables, `seq`, digests for every table
     (`name_keys.name` and `text_ids.body` included), `spath`, 64-child `kids`, `live_sups`,
     the sidecar with `delete_bounds`, the digest rule and the prov-skew failure.
   - `build_serving_db.py` joins the reducer scope (`brain/authority/reducer-inputs-v1.json`).
   - Releasable, since nothing runs it yet.
   - **1b. Explorer tables** [affecting], after PR #137, once the layout records `bub` and the
     bubble discs. Includes the 8 MB cap per precomputed scope.
2. **Migration and importer** [affecting because of `wiki/migrations-brain`; the importer in
   `site/ops` is neutral].
   - The importer's `import` (with `--scratch`), `prune`, `supersede` and `verify` commands:
     register before prune, finishing an interrupted delete, resume from recorded coverage,
     and a read-only `verify`.
   - Releasable, since nothing uses it yet.
   - Afterwards the operator applies the migration with `--command`, imports a full-size
     serving file and sidecar from step 0's build into the remote `wikilean-brain` in scratch
     mode, then supersedes and prunes it.
   - **Gate before the first D1 release:** this remote import of one full dataset must
     succeed, and it records the dataset's footprint (`size_after` before and after, indexes
     and FTS included), the statement size and timing of every call, the call count and the
     wall time. Three such footprints must fit under the 10 GB cap with room to spare.
3. **Worker**, in two PRs.
   - **3a. `BrainStore` interface** [affecting]. Readers call the interface; only the static
     implementation exists; no behavior change. Releasable.
   - **3b. `D1Store`** [affecting]. The binding, the page endpoints, their limiter bindings,
     the concurrency gate over page misses, the agent API and MCP, the agent D1-cost limiter,
     the cache rules (immutable for the current release, `max-age=300` for the previous, the
     version id in the Cache API key), the layout rule in `brain.ts`, the reference text
     (`mcp.ts:14`, `129-145`, `201-203`; `brain-api.ts:2769-2771`), and the shim fix with the
     new tests.
     - Releasable: it stays dormant until a release lists the sidecar.
     - Needs the database id from #150, and the migration must be applied before deploy.
4. **Release contract** [affecting, except the canary and bundle parts, which are neutral].
   The layout rule in the validators; serving verification (base tables re-derived, derived
   tables checked by digest and invariants, the Unicode version, the refusal of `traces.prov`,
   the verification receipt); provenance coverage over the serving rows; the legacy tools
   pinned to the static set; `build_release.py` choosing the layout per workspace; re-pinning
   the existing assembly plans for the changed `build_release.py`; and the canary checks.
   Releasable, since the static layout is unchanged.
5. **Promoter** [neutral, except the nightly's verify run: `brain-nightly.sh` is
   release-affecting by exception, `brain_release_authority.py:57`]. The prepare and fence
   checks, `verify --sample` called in-process after the canary with a mismatch journaled as
   manual intervention, the nightly's `verify --sample` run, the separate verify timeout,
   chunked hashing, and the runbook's import, prune, supersede and verify commands.
6. **Replay tools on the D1 layout** [affecting]: section 6. Releasable, since nothing builds
   the D1 layout by default yet.
7. **Cutover** [affecting, **held**].
   - The page reads the endpoints, and `site/test_frontier_page.py` changes with it.
   - `build_cell_shards.py` writes the D1 output by default: intermediates outside the public
     tree, `cells/frontier.json` and the slim manifest.
   - The nightly runs `build_serving_db.py` and its test after `test_frontier`
     (`brain-nightly.sh:773-779`).
   - `build_release.py` freezes the D1 layout when the workspace has a serving file, and the
     existing assembly plans are re-pinned again if its bytes change.
   - `query.py` and the other local tools read `serving.sqlite3`.
   - Held until steps 1 to 6 are merged, #150 has results, and step 2's remote scratch import
     gate has passed. From then on, every release built from main is a D1 release, either
     compatibility or offline replay.
   - It can be two PRs (page, builder) merged back to back, with no release cut between them.
8. **TauCeti harvest cap and the big files out of Git** [affecting], last. Why last:
   [`docs/BRAIN-TAUCETI.md`](BRAIN-TAUCETI.md), Plan.
9. **The release**: a full offline replay release, built, verified and imported; fresh P1B,
   Jack's approval, promote, canary.
10. **Cleanup** [affecting], after step 9: delete `StaticStore` and the static fixtures, and
    rewrite the crawling advice that names the static cells files (`docs/BRAIN-API.md:677-682`).

## Not verified yet

- Every limit marked "(unchecked)", and the `cli.js` line references.
- Trigram support and `contentless_delete=1` in D1 and in `node:sqlite` on CI, and counting FTS
  rows through `_docsize`. Whether D1 accepts the FTS5 `automerge`, `crisismerge` and `merge`
  statements.
- Whether reads stall during a file import or during `--command` batches (#150), and whether a
  multi-statement `--command` runs atomically (the importer doesn't rely on it). Whether the
  100 KB statement limit applies per statement or per `--command` request, which carries
  several ranges of about 90 KB.
- The largest `cell_pages`, `supercells` and explorer rows, and the largest folder by children
  and by cells.
- How many explorer scopes get precomputed and how big they are, the total `explorer_scopes`
  bytes per dataset, and whether every scope fits 8 MB after collapsing; the row counts of
  `hot_prefixes` and `cell_under`.
- `rows_written` and wall time per import, the verify time, and how long dropping a large FTS
  table takes.
- The remote footprint of one full dataset, indexes and FTS included, and so whether three
  datasets fit under 10 GB (step 2's gate).
- Worst-case times for adversarial fragment phrases, long OR text queries and parts-mode
  prefixes.
- How many folders have more than 64 children, where the grandchild preview can differ under
  filters.
- D1 latency from distant Cloudflare data centers, and the per-endpoint D1 time behind the
  limiter numbers and the agent D1-cost budget.
- Whether two clean-room builds of `serving.sqlite3` agree byte for byte on one SQLite version
  (step 6).

## Review

Two adversarial reviews of this design were resolved before step 1. The first found 7 major
and 10 minor findings plus 14 readers the first draft missed: among them pruning that could
delete a resumed import, unbounded reads, rows keyed by release, an importer that could not
resume, no schema evolution rule, and a bridge cap that changed results. The second, a
verification of the revised design, found 11 more, all minor: among them immutable caching
that broke rollback, too many protected datasets for the 10 GB cap, FTS inputs that were not
digested, and an importer that pruned before it registered. Every finding is resolved in the
sections above. PR #153 then had three reviews of its own (26 issues, 2 major), all fixed or
recorded in its description.
