# `scripts/pipeline/` — WikiLean's own locate pipeline

The **producer** of sealed locate record sets. `scripts/articles/` is the consumer: the
store builder, the uploader and the intake verifier. Design and the stage order:
[docs/PIPELINE.md](../../docs/PIPELINE.md); the record-set format itself is
[docs/ARTICLES.md](../../docs/ARTICLES.md) section 3.

Two rules hold over everything here (docs/PIPELINE.md section 0):

- **Nothing imports formal-companion or calls a formal-companion command.** Not as a
  library, not as a vendored copy, not as a subprocess.
- **One shared piece: Literature Links**, pinned by
  `wiki/src/vendor/literature-links/provenance.json` and reached only through
  `links.py`.

Release-neutral: `scripts/` is in `site/ops/brain_release_authority.py`'s neutral list,
so the pipeline changes without a release.

## The modules

| module | stage | what it is |
|---|---|---|
| `links.py` | 1 | Literature Links, pinned; the one checker |
| `keys.py` | 2 | `passage_key`, `pair_key`, `bundle_key`, canonical JSON |
| `vocab.py` | — | the locate vocabulary, pinned to the store builder's |
| `status.py` | 3 | the derived status, under status rules 1, 2 and 3 |
| `readback.py` | 4 | a sealed set read back into outcome rows; the seal check |
| `records.py` | 5 | the records of one passage, and the cut the layer gives it |
| `export_records.py` | 6 | the sealed set, written |
| `gate/` | 7 | the out-of-tree gate: rules 1 to 10, the text layer, the stager |
| `tally.py` | 8 | documents → finding rows → the challenge → the status → outcome rows |
| `carry.py` | 8 | the carry-over map between two cuts of one page |
| `corpus.py` | 8, 10 | which files of a run's corpus are documents, and its receipt |
| `index.py` | 8 | the declaration index the proof statuses join from |
| `items.py` | — | the passage item grammar: `clean()` hashes, `join_key()` joins |
| `sources.py` | 9a | the web-source families: the permalink, the container, the licence |
| `sourceset.py` | 9a | the append-only, versioned manifest, and its pin |
| `render_web.py` | 9a | the frozen `web-html/3` renderer, a **use** of `site/anchor_resolve.text_layer` |
| `wikinet.py` | 9a | the one module of this pipeline that may fetch |
| `fetch_sources.py` | 9a | fetch and render; a dry run unless `--execute` |
| `cut.py` | 9b | the statement-level cut: the plan, and the frozen inventory read back |
| `candidates.py` | 9c | candidate retrieval over the declaration index |
| `locate.py` | 9c | the locate run: retrieval, the tasks, the prompts |
| `challenge.py` | 9d | which records a challenge judges, and the plan it writes |
| `prompts.py`, `prompts/` | 9 | the prompts, rendered from the run's own paths |
| `run/` | 10 | plan, the sandboxed agent call, harvest, cost, the spend watchdog |
| `contribute/` | 11 | the work-repo transport, the claim, the sandbox and the middle |

All eleven stages are built. What is left is a **measurement**, not a stage: whether
stage 9's prompts produce the judgements batch 1's did needs a paid run, and **no model
call of any kind has been made from here**. docs/PIPELINE.md section 4 has the table,
section 10 the list and section 12 the proof that is owed.

**Two rules of stage 9 that are easy to break by accident.** The text layer is
`site/anchor_resolve.text_layer`'s and nothing else computes one: `render_web` aligns the
tree's text onto it and refuses a body the two readers disagree about, so a second copy of
the collapse rule cannot creep in. And `wikinet` is the only module that may reach the
network: `test_wikinet.py` greps every other module here and fails on one that names a
fetch.

## Running

Python 3.12, from the repository root, by path:

```bash
python3 scripts/pipeline/test_status.py          # one test module
./scripts/ci-python.sh                           # every gate, including these
```

Every `test_*.py` here is named by a `run_check` line in `scripts/ci-python.sh`
(`scripts/check_ci_python_tests.py` fails when one is not).

## Fixtures

`fixtures/recordset/` is the pipeline's own synthetic sealed set, written by
`fixtures/make_fixture.py` and rebuilt by the tests, which refuse a difference. Two
invented articles carry every outcome and shape the format has.
`scripts/articles/build_store.py` builds it, so what this producer writes is what
WikiLean's consumer accepts.

Synthetic only. WikiLean is public: no article text, no host names, no operator paths,
no tokens. The status rules are pinned against the producer's own truth tables and the
hand-written shared tables, which live in `scripts/articles/fixtures/` because the store
builder is pinned to them too.

A test that needs a **real** sealed record set reads it from the directory
`WIKILEAN_LOCATE_SETS` names and skips when that is unset, so a fresh checkout runs
green without one.

The gate suite's synthetic source (`gate/test_gate.py`: four blocks, one display, three
passages) is the one the run suite plans against and the one the contribute suite's
stub worker answers, so a document that passes the gate is the document the rest of the
pipeline is tested on.

## Running a batch, end to end

Nothing here calls a model without `--execute`.

```bash
python3 scripts/pipeline/run/plan.py --run-id loc-1 --out <root> \
    --inventory <corpus>/inv-1 --inventory-run inv-1 \
    --universe <file> --manifest <file> --sources <dir> --index <file>
python3 scripts/pipeline/run/agent.py --root <root> --task <id> --prompt <file>  # dry run
python3 <root>/gate/verify_locate.py --gate-config <root>/gate/gate.json \
    --root <worktree> --findings-dir <worktree>/findings
python3 scripts/pipeline/run/harvest.py --run-id loc-1 --from <documents> \
    --to <corpus>/loc-1 --usage <root>/usage --gate-log <root>/gate-log
python3 scripts/pipeline/tally.py --run-id loc-1 --corpus <corpus> --runs <runs> \
    --universe <file> --index <file> --challenge-run chl-1 \
    --outcomes <file> --report <file> --export-rows <file>
python3 scripts/pipeline/export_records.py --rows <file> --sources <file> ...
```
