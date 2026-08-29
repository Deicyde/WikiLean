#!/usr/bin/env python3
"""Hermetic regression tests for fold_proposals output finalization.

The fixture isolates proposals, brain data, and catalog data so it can exercise
FC/repo retraction plus the trailing override, universe-extension, and report
logic without reading or modifying the production corpus.

Run:

    python3 brain/test_fold_proposals.py
"""
from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fold_proposals as F  # noqa: E402


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_completed_retract_key_matches_fold_completion():
    names = {"Top.Namespace.target"}
    assert F._completed_retract_key("Q1", "Namespace.target", names) == (
        "Q1", "Top.Namespace.target")
    assert F._completed_retract_key("Q1", "Top.Namespace.target", names) == (
        "Q1", "Top.Namespace.target")
    ambiguous = {"A.Namespace.target", "B.Namespace.target"}
    assert F._completed_retract_key("Q1", "Namespace.target", ambiguous) == (
        "Q1", "Namespace.target")
    assert F._completed_retract_key("Q1", "Namespace.target", None) == (
        "Q1", "Namespace.target")
    malformed = F._completed_retract_key(
        {"bad": "qid"}, "Namespace.target", names
    )
    assert malformed == ('{"bad":"qid"}', "Top.Namespace.target")
    assert hash(malformed)


def test_main_retracts_links_and_runs_trailing_outputs():
    tmp = Path(tempfile.mkdtemp(prefix="fold_proposals_test_"))
    catalog = tmp / "catalog" / "data"
    proposals = tmp / "brain" / "proposals"
    data = tmp / "brain" / "data"
    checkout = tmp / "Mathlib"
    oracle = tmp / "declaration-data.json"
    for path in (catalog, proposals, data, checkout):
        path.mkdir(parents=True, exist_ok=True)

    oracle.write_text(json.dumps({"declarations": {"Mathlib.Example": {}}}))
    (catalog / "hierarchy.json").write_text(json.dumps({"libraries": {}}))
    (catalog / "source_registry.json").write_text(json.dumps({
        "crossref_sources": {},
        "frontier_sources": {
            "formal_conjectures": {},
            "tauceti": {},
        },
    }))
    (catalog / "rebuild_grounding.json").write_text(json.dumps([{
        "qid": "Q1",
        "formalizations": [{"decl": "Mathlib.Example"}],
    }]))
    write_jsonl(catalog / "wikidata_universe.jsonl", [{
        "qid": "Q1", "label": "alpha", "aliases": [],
    }])
    write_jsonl(catalog / "universe_extension.jsonl", [])
    write_jsonl(catalog / "grounding_overrides.jsonl", [])
    write_jsonl(catalog / "formal_conjectures.jsonl", [
        {"_meta": {"n_decls": 1}},
        {"decl": "Top.FC.target"},
    ])
    write_jsonl(catalog / "tauceti.jsonl", [
        {"_meta": {"n_decls": 1}},
        {"decl": "TauCeti.Repo.target"},
    ])

    write_jsonl(data / "fc_links.jsonl", [
        {"_meta": {"n_rows": 1}},
        {"qid": "Q1", "decl": "Top.FC.target", "kind": "mentions",
         "confidence": "medium", "evidence": {"proposer": "stale"}},
    ])
    write_jsonl(catalog / "tauceti_links.jsonl", [
        {"_meta": {"repo": "tauceti", "n_rows": 1}},
        {"qid": "Q1", "decl": "TauCeti.Repo.target", "repo": "tauceti",
         "kind": "mentions", "confidence": "medium",
         "evidence": {"proposer": "stale"}},
    ])

    rows = [
        {"action": "fc_link", "qid": "Q1", "qid_label": "alpha",
         "decl": "FC.target", "kind": "mentions", "evidence": "not the concept",
         "proposer": "test"},
        {"action": "repo_link", "repo": "tauceti", "qid": "Q1",
         "qid_label": "alpha", "decl": "Repo.target", "kind": "mentions",
         "evidence": "not the concept", "proposer": "test"},
        # Canonical-name aliases accepted in the same fold must still be
        # vetoed by the rejected suffix spelling above (any-reject wins).
        {"action": "fc_link", "qid": "Q1", "qid_label": "alpha",
         "decl": "Top.FC.target", "kind": "mentions", "evidence": "duplicate alias",
         "proposer": "test"},
        {"action": "repo_link", "repo": "tauceti", "qid": "Q1",
         "qid_label": "alpha", "decl": "TauCeti.Repo.target", "kind": "mentions",
         "evidence": "duplicate alias", "proposer": "test"},
        {"action": "override", "qid": "Q1",
         "set": {"match_kind:Mathlib.Example": "related"},
         "reason": "correct the grade", "proposer": "test"},
        {"qid": "Q999001", "qid_label": "new concept",
         "decl": "Mathlib.Example", "module": "Mathlib.Test",
         "confidence": "high", "evidence": "synthetic discovery",
         "proposer": "test"},
    ]
    shard = proposals / "regression.jsonl"
    write_jsonl(shard, rows)
    write_jsonl(Path(str(shard) + ".verified.jsonl"), [
        {**rows[0], "verdict": "reject", "verify_note": "wrong FC join"},
        {**rows[1], "verdict": "reject", "verify_note": "wrong repo join"},
        {**rows[2], "verdict": "accept", "verify_note": "alias looks valid"},
        {**rows[3], "verdict": "accept", "verify_note": "alias looks valid"},
        {**rows[4], "verdict": "accept", "verify_note": "grade correction valid"},
    ])

    fetched = {
        "Q999001": {
            "qid": "Q999001", "requested": "Q999001", "label": "new concept",
            "aliases": [], "description": "synthetic concept",
            "classes": ["Q1936384"], "enwiki_slug": "New_concept",
        },
    }
    old = {
        "CATALOG": F.CATALOG,
        "PROPOSALS": F.PROPOSALS,
        "DATA": F.DATA,
        "CHECKOUT": F.CHECKOUT,
        "ORACLE": F.ORACLE,
        "fetch_entities": F.fetch_entities,
        "frontier_names": F._frontier_names,
        "ext_page_ids": F._ext_page_ids,
        "oracle_modules": F._oracle_modules,
    }
    try:
        F.CATALOG = catalog
        F.PROPOSALS = proposals
        F.DATA = data
        F.CHECKOUT = checkout
        F.ORACLE = oracle
        F.fetch_entities = lambda qids: {qid: fetched[qid] for qid in qids}
        F._frontier_names = {}
        F._ext_page_ids = {}
        F._oracle_modules = None

        first_stdout = io.StringIO()
        with contextlib.redirect_stdout(first_stdout):
            assert F.main() == 0

        fc_rows = read_jsonl(data / "fc_links.jsonl")
        assert fc_rows == [{"_meta": {
            "source": "brain/fold_proposals.py",
            "inputs": "brain/proposals/fc_links_*.jsonl",
            "n_rows": 0,
        }}]
        repo_rows = read_jsonl(catalog / "tauceti_links.jsonl")
        assert repo_rows == [{"_meta": {
            "source": "brain/fold_proposals.py",
            "inputs": "brain/proposals/repo_link_tauceti_*.jsonl",
            "repo": "tauceti", "kinds": ["mentions"], "n_rows": 0,
        }}]

        overrides = read_jsonl(catalog / "grounding_overrides.jsonl")
        assert len(overrides) == 1
        assert overrides[0]["qid"] == "Q1"
        assert overrides[0]["set"] == {"match_kind:Mathlib.Example": "related"}
        assert overrides[0]["reason"].startswith("[test|skeptic:accept]")

        extension = read_jsonl(catalog / "universe_extension.jsonl")
        assert extension == [{
            "qid": "Q999001", "label": "new concept",
            "description": "synthetic concept", "classes": ["Q1936384"],
            "enwiki_slug": "New_concept", "source": "discovery",
        }]
        discovery = read_jsonl(data / "discovery_proposals.jsonl")
        assert discovery[0]["confidence"] == "medium"
        assert discovery[0]["evidence"]["skeptic"] == "pending"

        report = first_stdout.getvalue()
        assert "folded: 0 container links, 1 discovery links" in report
        assert "0 fc links (0 total in file)" in report
        assert "tauceti: 0 folded (0 total in catalog/data/tauceti_links.jsonl)" in report
        assert "1 new overrides, 1 universe-extension rows" in report
        assert "1 rows carry skeptic:pending (capped at medium confidence)" in report

        first_fc = (data / "fc_links.jsonl").read_bytes()
        first_repo = (catalog / "tauceti_links.jsonl").read_bytes()
        second_stdout = io.StringIO()
        with contextlib.redirect_stdout(second_stdout):
            assert F.main() == 0
        assert (data / "fc_links.jsonl").read_bytes() == first_fc
        assert (catalog / "tauceti_links.jsonl").read_bytes() == first_repo
        assert len(read_jsonl(catalog / "grounding_overrides.jsonl")) == 1
        assert len(read_jsonl(catalog / "universe_extension.jsonl")) == 1
        assert "0 new overrides, 0 universe-extension rows" in second_stdout.getvalue()
    finally:
        F.CATALOG = old["CATALOG"]
        F.PROPOSALS = old["PROPOSALS"]
        F.DATA = old["DATA"]
        F.CHECKOUT = old["CHECKOUT"]
        F.ORACLE = old["ORACLE"]
        F.fetch_entities = old["fetch_entities"]
        F._frontier_names = old["frontier_names"]
        F._ext_page_ids = old["ext_page_ids"]
        F._oracle_modules = old["oracle_modules"]
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> None:
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_")]
    for test in tests:
        test()
        print(f"  ok  {test.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    main()
