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
| `sources.py` | 9a | the web-source families (the rest of the stage is TODO) |
| `export_records.py` | 6 | the sealed set, written |

Stages 7 to 11 are listed in docs/PIPELINE.md section 4 with what is built.

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
