#!/usr/bin/env python3
"""Unit + integration tests for fold_proposals' action:"xref" rtype.

Exercises the ext-anchor fold path against the REAL catalog (crossref
registry, external pages, wikidata universe, decl oracle) but a synthetic
proposals dir + isolated brain/data output dir:

  - row_key: two xref proposals on one QID must NOT collide (the pre-fix key
    ignored the xref (db,id), so skeptic overlay verdicts cross-applied)
  - accept path: real page id (catalog/data/external/nlab_pages.jsonl) + real
    universe QID + skeptic accept → folded into ext_anchor_links.jsonl
  - machine-reject path: nonexistent page id rejected even with skeptic accept
  - registry-reject path: db not in source_registry crossref_sources
  - skeptic-reject path: verdict reject → discovery_rejected.jsonl, and the
    any-reject veto kills an accepted duplicate in another shard
  - unechoed row: REJECTED (anchors never fold pending — the skeptic is the
    only real gate against a prompt-injected cartographer), stays in
    proposals for the next skeptic pass
  - merge-dedupe: pre-existing ext_anchor_links.jsonl rows survive the fold,
    EXCEPT keys rejected this fold (retraction); re-fold is byte-idempotent

All test QIDs are already in catalog/data/wikidata_universe.jsonl so the fold
never hits the network. Run:

    python3 brain/test_fold_xref.py
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fold_proposals as F  # noqa: E402

# Real fixtures (asserted before use so a catalog change fails loudly):
#   Q181296 "abelian group", Q188276 "pigeonhole principle",
#   Q188295 "Fermat's little theorem" — all in wikidata_universe.jsonl.
#   nlab page ids "abelian group" and "group" — in nlab_pages.jsonl.
QID_AB, QID_PIGEON, QID_FERMAT = "Q181296", "Q188276", "Q188295"


def xrow(qid, qid_label, db, pid, **kw):
    base = {"action": "xref", "qid": qid, "qid_label": qid_label,
            "xref": {"db": db, "id": pid}, "title": pid,
            "url": f"https://example.org/{pid}", "reason": "same concept",
            "confidence": "medium", "proposer": "test-cartographer"}
    base.update(kw)
    return base


def write_jsonl(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def test_row_key_no_collision_for_two_xrefs_on_one_qid():
    a = xrow(QID_AB, "abelian group", "nlab", "abelian group")
    b = xrow(QID_AB, "abelian group", "nlab", "group")
    assert F.row_key(a) != F.row_key(b), "xref (db,id) must be part of the key"
    # overlay echo (base row + verdict) keys identically to its base row
    assert F.row_key({**a, "verdict": "accept"}) == F.row_key(a)
    # int/str page ids key identically (agents echo ids back as strings)
    c = xrow("Q42", "x", "oeis", 37)
    assert F.row_key(c) == F.row_key(xrow("Q42", "x", "oeis", "37"))
    # non-xref rows keep their identity semantics
    d1 = {"qid": "Q1", "decl": "Foo.bar"}
    d2 = {"qid": "Q1", "decl": "Foo.baz"}
    assert F.row_key(d1) != F.row_key(d2)
    assert F.row_key(d1) == F.row_key(dict(d1))


def fold_in_tmp(tmp: Path) -> tuple[Path, Path]:
    """Point the module at tmp proposals/data dirs and run main()."""
    proposals, data = tmp / "proposals", tmp / "data"
    proposals.mkdir(exist_ok=True)
    data.mkdir(exist_ok=True)
    old_p, old_d = F.PROPOSALS, F.DATA
    F.PROPOSALS, F.DATA = proposals, data
    try:
        rc = F.main()
        assert rc == 0, f"fold exited {rc}"
    finally:
        F.PROPOSALS, F.DATA = old_p, old_d
    return proposals, data


def test_fold_xref_end_to_end():
    # fixture sanity — fail loudly if the real catalog moved under us
    assert QID_AB in F.known_qids(), "Q181296 missing from wikidata universe"
    ids = F.external_page_ids("nlab")
    assert ids and "abelian group" in ids and "group" in ids
    assert "nlab" in F.crossref_dbs() and "notadb" not in F.crossref_dbs()

    tmp = Path(tempfile.mkdtemp(prefix="fold_xref_test_"))
    try:
        rows = [
            # A: accept path (real page, real QID, skeptic accept)
            xrow(QID_AB, "abelian group", "nlab", "abelian group"),
            # B: machine reject — page id does not exist (skeptic accepted!)
            xrow(QID_PIGEON, "pigeonhole principle", "nlab", "zz-no-such-page"),
            # E: machine reject — db not a crossref_sources key
            xrow(QID_AB, "abelian group", "notadb", "whatever"),
            # F: skeptic reject — same QID as A, different page (row_key fix:
            # under the old key this verdict would cross-apply to A)
            xrow(QID_AB, "abelian group", "nlab", "group"),
            # D: no skeptic echo → pending, confidence capped high→medium
            xrow(QID_FERMAT, "Fermat's little theorem", "nlab", "group",
                 confidence="high"),
        ]
        shard = tmp / "proposals" / "ext_anchor_20260701.jsonl"
        write_jsonl(shard, rows)
        write_jsonl(Path(str(shard) + ".verified.jsonl"), [
            {**rows[0], "verdict": "accept", "verify_note": "same concept"},
            {**rows[1], "verdict": "accept", "verify_note": "looks fine"},
            {**rows[2], "verdict": "accept", "verify_note": "looks fine"},
            {**rows[3], "verdict": "reject", "verify_note": "field vs object"},
            # rows[4] deliberately unechoed
        ])
        # second shard: duplicate of F, skeptic-accepted → any-reject veto
        shard2 = tmp / "proposals" / "ext_anchor_20260702.jsonl"
        write_jsonl(shard2, [rows[3]])
        write_jsonl(Path(str(shard2) + ".verified.jsonl"),
                    [{**rows[3], "verdict": "accept", "verify_note": "dup ok"}])
        # pre-seed the output file → merge-dedupe must preserve this row…
        seed = {"qid": "Q11518", "db": "nlab", "id": "Pythagorean theorem",
                "confidence": "medium", "evidence": {"proposer": "seed"}}
        # …but a pre-existing row whose key is REJECTED this fold must be
        # retracted (F/rows[3] is skeptic-rejected below)
        stale = {"qid": QID_AB, "db": "nlab", "id": "group",
                 "confidence": "medium", "evidence": {"proposer": "seed-stale"}}
        write_jsonl(tmp / "data" / "ext_anchor_links.jsonl",
                    [{"_meta": {"n_rows": 2}}, seed, stale])

        _, data = fold_in_tmp(tmp)

        out = read_jsonl(data / "ext_anchor_links.jsonl")
        assert "_meta" in out[0], "first line must be _meta"
        by_key = {(r["qid"], r["db"], r["id"]): r for r in out[1:]}
        assert out[0]["_meta"]["n_rows"] == len(by_key) == 2, sorted(by_key)

        a = by_key[(QID_AB, "nlab", "abelian group")]          # accept path
        assert a["confidence"] == "medium"
        assert a["evidence"]["skeptic"] == "accept"
        assert a["evidence"]["proposer"] == "test-cartographer"
        # D (unechoed) must NOT fold — anchors require an explicit skeptic accept
        assert (QID_FERMAT, "nlab", "group") not in by_key
        assert (seed["qid"], seed["db"], seed["id"]) in by_key  # merge-dedupe
        # F + its dup rejected AND the stale pre-seeded row retracted
        assert (QID_AB, "nlab", "group") not in by_key

        why: list[tuple[tuple, str]] = []
        for r in read_jsonl(data / "discovery_rejected.jsonl"):
            x = r.get("xref") or {}
            why.append(((r.get("qid"), x.get("db"), str(x.get("id"))),
                        r["rejected_reason"]))
        reasons = {}
        for k, v in why:
            reasons.setdefault(k, []).append(v)
        assert "page id not in" in reasons[(QID_PIGEON, "nlab", "zz-no-such-page")][0]
        assert "crossref_sources" in reasons[(QID_AB, "notadb", "whatever")][0]
        pending_reasons = reasons[(QID_FERMAT, "nlab", "group")]
        assert any("requires a skeptic verdict" in r for r in pending_reasons), \
            f"unechoed anchor must be rejected-as-pending, got {pending_reasons}"
        group_reasons = sorted(reasons[(QID_AB, "nlab", "group")])
        assert len(group_reasons) == 2, \
            f"expected F + vetoed duplicate, got {group_reasons}"
        assert group_reasons[0].startswith("fold-check: conflicting")  # dup vetoed
        assert group_reasons[1].startswith("skeptic:")                 # F itself
        assert (QID_AB, "nlab", "abelian group") not in reasons

        # re-fold: byte-idempotent (no timestamps), rows neither dup nor lost
        first = (data / "ext_anchor_links.jsonl").read_bytes()
        fold_in_tmp(tmp)
        assert (data / "ext_anchor_links.jsonl").read_bytes() == first
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    main()
