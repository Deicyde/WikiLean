# WikiLean daily `@[wikidata]` batch bot

Runs one batch of 10 `@[wikidata]` cross-reference tags per day through review →
merge-or-recycle against `leanprover-community/mathlib4`.

## The loop

Each run does two phases:

1. **Settle** the current PR (`settle.py`, deterministic):
   - **Gate:** ≥2 distinct human reviewers on the PR **and** ≥24h since it opened.
   - When the gate is open, classify each tag and **split** the PR down to its
     green tags (`split.py`, force-push), then post a ready-to-merge comment.
   - A maintainer (jcommelin) merges the now-all-green PR (bot can't merge upstream).
2. **Open** the next batch (`open_batch_pr.py`): requeued (retargeted) recycled
   tags + Brain-suggested `formalizes` edges + fresh pool tags → 10 → new PR +
   crossref comments + `LLM-generated` label + side-by-side table.

## Per-tag rule (`settle.py`)

- A bare **note/comment = "defer to the other reviewer"** → ignored.
- A **maintainer's explicit approve/reject/revise/flag trumps** everything.
- Otherwise: any **reject/revise/flag → recycle**; else **≥1 approve → green**;
  else (no review) → recycle.

So one approval suffices per tag (silence = agreement), but any unresolved
objection recycles, and a maintainer overrides. Recording model: the detailed
reviewer GitHub-**Approves** the PR (blanket = approve all) + inline
revise/reject exceptions; the double-checking maintainer inline-flags only their
concerns.

## Recycle triage (`triage.py`) — the ONE LLM step

The deterministic settler produces the recycle set; `triage.py` asks an LLM (via
`claude -p`) to read each tag's reviewer notes + context and decide **requeue**
(a fixable retarget — proposes & verifies a `suggested_decl`) or **cut** (not
cleanly in Mathlib / not worth a review slot). Tag *generation* stays
deterministic — the LLM only proposes the target declaration; `open_batch_pr.py`
applies it.

## Determinism boundary (hard rule)

Everything is deterministic **except** `triage.py`. The split, the gate, the
green/recycle classification, PR opening, comments, and label are all plain
fetch/parse/git/gh — no LLM.

## Safety / rollout

- **Everything is `--dry-run` by default.** `daily_bot.py` and `split.py` mutate
  GitHub/Mathlib only with `--apply`.
- Run `--apply` **by hand** for the first several daily cycles and watch each
  step (force-push, daily PR, triage) before wrapping in cron.
- `split.py` force-pushes the *fork* branch with `--force-with-lease`.
- Confirm the daily cadence with Mathlib maintainers before automating.

## Run

    python3 daily_bot.py --mathlib ~/path/to/mathlib4              # dry-run
    python3 daily_bot.py --mathlib ~/path/to/mathlib4 --apply      # act

`state/bot_state.json` tracks `{current_pr, batch_num, branch}`;
`state/recycle_queue.json` carries requeued retargets to the next batch.
`state/brain_queue.json` carries graduated Brain `formalizes` edges for the
review-gated Brain lane:

    python3 brain/harvest_community_edges.py   # graduate live Brain edits
    python3 bot/brain_queue.py                 # refresh state/brain_queue.json

## Database-agnostic review/queue lane

The daily loop above is still the Wikidata lane, but the lower-level queue and
review pieces now work over a generic crossref shape: `{db, id, decl, file}`.
Legacy `{qid, decl, file}` rows are normalized to `{db:"wikidata", id:qid}`.

Supported queue/review databases:

- `wikidata` → `@[wikidata Q...]`, `/queue`, `/api/queue`, `/review?...`
- `lmfdb` → `@[lmfdb knowl.id]`, `/queue/lmfdb`, `/api/queue/lmfdb`,
  `/review?...&db=lmfdb`

LMFDB candidates can be built deterministically from the Brain graph by joining
Wikidata P12987 knowl xrefs to Brain `formalizes` edges:

    python3 bot/lmfdb_queue.py --dry-run
    python3 bot/lmfdb_queue.py
    python3 bot/publish_queue.py --db lmfdb --payload bot/state/lmfdb_queue.json

For an approved LMFDB batch JSON, use the same PR applicator:

    python3 bot/open_batch_pr.py --approved lmfdb_approved.json --mathlib ~/mathlib4 --all

The generic settler/table/splitter take `--db lmfdb`; without `--db` they remain
Wikidata-compatible.

## Files

| file | role | LLM? |
|---|---|---|
| `settle.py` | gate + green/recycle classification | no |
| `split.py` | remove recycled tags, rebuild, force-push to greens | no |
| `triage.py` | requeue-vs-cut + retarget suggestion | **yes** |
| `brain_queue.py` | graduated Brain formalizes edges → queue suggestions | no |
| `lmfdb_queue.py` | Brain LMFDB xrefs + formalizes edges → queue suggestions | no |
| `daily_bot.py` | orchestrator (settle → split → triage → open) | only via triage |
| `open_batch_pr.py` | apply tags + build + open PR (from the existing pipeline) | no |
