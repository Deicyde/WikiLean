# The ranked-choice council

**Status: design only (2026-10-19).** Nothing is built: no Worker route, migration or page.
§4's SQL is a draft for a future numbered migration. The pilot (§8) and the next run (§9) take
place outside this repo.

## 1. Purpose

When a passage has several candidate declarations (in different libraries, or in different files
of one library), voters rank them, with "Not formalized" always on the ballot. The relation per
pair (exact, close, broad, narrow, part, related) stays the locate pipeline's per-pair judgement.
The council answers another question: **which candidate formalizes the passage best, and does any?**

- A passage's **candidates** are its records in a counting relation (exactMatch, closeMatch,
  broadMatch, narrowMatch, hasPart), one per distinct (repository, declaration). relatedMatch
  records stay off the ballot because they never count towards a status. A council needs at
  least two candidates; a single one is left to the challenge.
- The council ranks single declarations, and only the ones the library search found. It does not
  search, and whether several hasPart declarations together cover a passage stays a coverage claim.
- The council never writes a status, which stays derived from the records. An outcome that
  disagrees with them is exported as a disagreement to re-check in the next challenge round: the
  council accepts no candidate where the records say formalized or partial, or its first choice
  is not the basis card.

## 2. Ballot

- The options are the candidates, each named by its `pair_key`, plus `NOT_FORMALIZED`. A ballot is
  a strict ranking of every option: no ties and no omissions. A ballot that breaks this is refused
  when it is written, never repaired. An empty ranking `[]` withdraws the voter's earlier ballot,
  as `withdraw` does in the revised 0015.
- Candidates **above** `NOT_FORMALIZED` are acceptable formalizations, in order of preference: the
  declaration states the passage, or one clause of it, so showing it as a formalization does not
  mislead. Candidates **below** it do not formalize the passage (a related fact, a proof step,
  another setting). Their order is counted like the rest.
- Best means the declaration a reader should see first: it states more of the passage (a whole
  over a part; exact over close over broad) in the passage's own setting, and between equals it
  is the more canonical one (the definition over a derived equivalence).
- `relations` (optional) gives one of exactMatch, closeMatch, broadMatch, narrowMatch or hasPart
  per acceptable candidate. They do not enter the ranking; §5 takes one relation per accepted
  card from them, which the report sets beside the pipeline's. It never changes a record.

## 3. Keys

```python
passage_key = sha256(f"{pageid}\n{span_sha256}".encode("utf-8")).hexdigest()
pair_key    = sha256(f"{passage_key}\n{repository_url}\n{declaration}".encode("utf-8")).hexdigest()
```

- `pageid` is the Wikipedia page id in decimal. `span_sha256` is the record's subject
  `span_sha256`, the sha256 of the passage's span of the text layer as UTF-8.
- `repository_url` is the record object's `uri` as written (https, no trailing slash, no `.git`),
  e.g. `https://github.com/leanprover-community/mathlib4`. `declaration` is the fully qualified
  name in the object's `location.declaration`.
- Hashes are lowercase hex; `\n` is one LF; there is no trailing newline.

Both keys are free of revisions and commits, so ballots survive new revisions and new library
commits; `location_key` (source, item, library, commit, declaration) changes with both. A revision
that leaves the passage's text alone keeps its `passage_key`, and a changed passage is a new one
that needs new ballots. The snapshot (§4) records the commit a voter saw. The span depends on the
renderer, frozen at web-html/3; changing the renderer re-keys every passage.

## 4. Storage (draft SQL)

A draft for a future migration, append-only like the revised 0015: a ballot is never updated, and
a voter's current ballot is their newest row for the passage.

```sql
CREATE TABLE IF NOT EXISTS council_candidates (   -- what a ballot's pair_keys meant
  record_set   TEXT NOT NULL CHECK (length(record_set) = 64 AND record_set NOT GLOB '*[^0-9a-f]*'),
  pair_key     TEXT NOT NULL CHECK (length(pair_key) = 64 AND pair_key NOT GLOB '*[^0-9a-f]*'),
  passage_key  TEXT NOT NULL CHECK (length(passage_key) = 64 AND passage_key NOT GLOB '*[^0-9a-f]*'),
  pageid       INTEGER NOT NULL CHECK (pageid > 0),
  span_sha256  TEXT NOT NULL CHECK (length(span_sha256) = 64 AND span_sha256 NOT GLOB '*[^0-9a-f]*'),
  repository   TEXT NOT NULL CHECK (repository GLOB 'https://?*'),
  commit_sha   TEXT NOT NULL CHECK (length(commit_sha) = 40 AND commit_sha NOT GLOB '*[^0-9a-f]*'),
  declaration  TEXT NOT NULL CHECK (length(declaration) BETWEEN 1 AND 512),
  PRIMARY KEY (record_set, pair_key)
);
CREATE TABLE IF NOT EXISTS council_ballots (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  passage_key  TEXT NOT NULL CHECK (length(passage_key) = 64 AND passage_key NOT GLOB '*[^0-9a-f]*'),
  voter_kind   TEXT NOT NULL CHECK (voter_kind IN ('human', 'model')),
  voter_id     TEXT NOT NULL CHECK (length(voter_id) <= 128 AND (
                 (voter_kind = 'human' AND voter_id GLOB 'user:?*') OR
                 (voter_kind = 'model' AND voter_id GLOB 'model:?*'))),
  ranking      TEXT NOT NULL CHECK (json_valid(ranking) AND json_type(ranking) = 'array'),
  relations    TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(relations) AND json_type(relations) = 'object'),
  citation     TEXT NOT NULL CHECK (json_valid(citation) AND json_type(citation) = 'object'),
  supersedes   INTEGER REFERENCES council_ballots(id),
  created_at   INTEGER NOT NULL                  -- ms
);
CREATE INDEX IF NOT EXISTS idx_council_ballots_voter ON council_ballots (passage_key, voter_id, id);
CREATE VIEW IF NOT EXISTS council_ballots_latest AS
  SELECT b.* FROM council_ballots AS b
  WHERE b.id = (SELECT max(c.id) FROM council_ballots AS c
                WHERE c.passage_key = b.passage_key AND c.voter_id = b.voter_id);
```

- `ranking` lists pair_keys and `"NOT_FORMALIZED"`, best first; `relations` maps a pair_key to
  a relation. `voter_id` is `user:<users.id>` for a person, or `model:<id>` with the model id
  observed in the call's envelope or transcript.
- `citation` is `{model, model_observed, effort_requested, effort_observed, harness,
  harness_version, prompt_sha256, operator, created_at, run_id, record_set, seed}`.
  `model_observed` is false only when the harness reported no id and the requested one is stored.
  The efforts are §6's. `record_set` (the SEAL sha the candidates came from) joins a ballot to the
  snapshot rows it saw, and `seed` is §6's. A person's ballot has harness `wikilean-web`, the
  Worker build as its version, and null model, efforts, prompt, operator and run_id.
- `supersedes` is the voter's previous ballot on the passage (NULL for the first); a write whose
  `supersedes` is not the voter's latest is refused. People vote only from a session; model
  ballots come in bulk under the pipeline bearer and are never `human`.

## 5. Counting

Schulze (beatpath) sets the order and a direct majority over NOT_FORMALIZED decides acceptance,
per passage, counted separately for each voter kind, over the options
O = the current candidates ∪ {NOT_FORMALIZED}:

1. Take each voter's latest ballot, skipping withdrawn ones and options no longer candidates.
2. d[x][y] = the number of ballots that rank both x and y, with x above y, so a candidate added
   after a ballot was cast is compared only on the ballots that ranked it.
3. Strongest paths, by winning votes: p[x][y] = d[x][y] if d[x][y] > d[y][x], else 0; then for
   each i, each j ≠ i and each k ∉ {i, j}: p[j][k] = max(p[j][k], min(p[j][i], p[i][k])).
4. x beats y when p[x][y] > p[y][x]. This relation is transitive, so the full ranking orders the
   options by how many others each beats, most first.
5. Ties in that count put NOT_FORMALIZED first, then candidates by ascending pair_key.
6. A candidate c is accepted when more ballots rank it above NOT_FORMALIZED than below it,
   d[c][NOT_FORMALIZED] > d[NOT_FORMALIZED][c], whatever the strongest paths say; the accepted
   set is the accepted candidates in the full ranking's order. So a council split on whether a
   candidate formalizes the passage (a tie) does not accept it, with any number of voters. The
   beatpath does not decide acceptance because from three voters on it can deny a majority (2-1
   with three, by a cycle through NOT_FORMALIZED), and from four on accept a card without one (a
   2-2 tie with four, by a path through another candidate).
7. An accepted card's relation comes from the voters who accepted it and gave it one (§2): first
   the status (formalized or partial, by derive_status's mapping for the card alone) that most of
   their relations imply, then the most common relation among those that imply it. A tie goes to
   the weaker: the weaker status, then the weaker relation by strength, then by name.
8. Report n (ballots counted); the Condorcet winner (x with d[x][y] > d[y][x] for every other y;
   it is always Schulze's winner too) or null; the full ranking; the accepted set, in order, with
   each card's relation; d; and the pairs the tie-break ordered.
9. No candidate accepted: the council says no candidate formalizes the passage. Otherwise the
   first accepted candidate is the council's choice.

A tally with n < 2 is reported but has no outcome. Human and model tallies stay apart. Whether
human ballots override model ones, outweigh them or count with them is Jack's policy, left open,
and nothing combines the two before he sets it. A tally is a pure function of the latest ballots
and the snapshot: it is recomputed when needed, never stored as the truth.

## 6. Independence

- A voter sees the passage's text, the article's title and section, `NOT_FORMALIZED`, and for each
  candidate its repository, module, declaration and Lean statement (the corpus signature at the
  pinned commit).
- A voter never sees other ballots or tallies, the pipeline's relation, rank, confidence,
  challenge result or notes, the passage's status, or which card is the basis.
- Each ballot draws a fresh 32-hex seed, stored as `citation.seed`. Options are shown in ascending
  order of sha256("<seed>\n<pair_key>"), with the string `NOT_FORMALIZED` in place of a pair_key
  for that option, so it moves too. The shown order can be recomputed and position bias measured.
- A model ballot is one fresh call for that one passage (the pilot asked eight per call), with no
  tools, no web and no repository access. Every model gets the same prompt (its sha256 is in the
  citation) and is not told which other models vote.
- A voter's reasoning effort, where its CLI takes one, is set for the run and checked against the
  effort the call reports; a call that reports another gives no ballot. The citation keeps the
  effort requested and the one observed (null when the call reports none).
- People vote in a view that shows only what a model sees, and see the tally only after voting.
  Someone who has read the cards has seen their relations, one more reason the tallies stay apart.

## 7. Display

Jack's rule (2026-10-19, merged article page design §4a): provenance is a specific citation,
stored, and shown only under Details. For the council it is stronger:

- The default view shows no voting data. At most, the outcome sets the order of the cards: the
  accepted candidates first, in the council's order, then the rest in today's order. The basis
  card keeps its "This card sets the status" mark wherever it lands. When the council accepts no
  candidate the default view is unchanged, and the disagreement is exported for re-checking (§1).
- Ballots, tallies, each model's ranking and their citations appear only in the opt-in "How sure
  are we?" view under Details, worded specifically: for example, "Ranked 1st of 4 by Claude Opus 5
  (claude-opus-5) on 19 Oct 2026, run council-2026-10-19-jack-3f9a2c, by jack", never "AI voted".
- No page changes until a policy (§5) names the tally that counts.

## 8. Pilot

- **Set:** smoke 2's sealed record set (campaign `run:loc-smoke-2+chal-smoke-2`, universe `u1`).
  208 of its 331 formal passages have at least two candidates (§1), 650 in all: 68 passages have
  2, 72 have 3, 42 have 4 and 26 have 5.
- **Voters:** Claude and Codex through their CLIs, on the consented run host the smoke runs used,
  under `~/council-pilot/`, at most 6 calls at once: one ballot per voter and passage, 416 ballots
  in 52 calls of eight passages. Operator jack; run ids `council-<date>-jack-<6 hex>`. Spend cap
  100 USD: a 10-passage probe sets the estimate, and the run stops if the estimate exceeds the cap.
- **Outputs** (operator's notes, not this repo): ballots as JSONL rows in §4's shape, the
  candidate snapshot, the model tallies and a score report, quoting at most 12-word heads.
- **Scoring** against the smoke-2 audit (`SMOKE2-AUDIT.md`, operator notes
  `wikilean-overhaul-2026-10-17`). Its 59 audited items fall on 55 of the 208 passages (S1 23,
  S2 27, S4 9), and 47 of their records are candidates on a ballot; the other 12 are 11 records
  the challenge left at relatedMatch and one absence. The gold label is the audit's right
  relation (the recorded one where the audit kept it): a counting relation means acceptable,
  relatedMatch means not.
  - Acceptability: 44 of the 47 are acceptable, so accepting everything scores 94%. Report each
    voter and the council against that baseline with Wilson 95% intervals, and read the 3
    negatives one by one.
  - Relations: each voter's optional relation against the audit's, beside the pipeline's current
    relation on the same records.
  - Best pick: the audit names a better declaration on 6 items, and only one (S1-03, the
    definition proper) is a candidate; the council should rank it first. The other 5 are search
    misses no council can fix.
  - CRT (S4): the audit puts all 9 passages at partial or formalized; NOT_FORMALIZED should lose.
  - Agreement over all 208: the same first choice, the same acceptable set, Kendall's tau, and
    position bias (final rank against shown position).
- **Limits:** with two voters most councils agree or fall to the conservative tie-break, and 47
  scored records give wide intervals. The pilot measures each model as a ranker and how often the
  two agree; it tests the method, not a council at scale.

## 9. Next run

- **Voters:** three models, Claude, Codex and Muse, each through its own CLI under §6, Codex and
  Muse at high reasoning effort and Claude at its CLI's default. Gemini is pending a route that
  only an administrator can set up; until then it does not vote, and the harness keeps its
  adapter.
- **Counting:** §5. With all three ballots present a card cannot tie with NOT_FORMALIZED: every
  card is decided 3-0 or 2-1. A passage missing a ballot can split 1-1, and then accepts nothing.
