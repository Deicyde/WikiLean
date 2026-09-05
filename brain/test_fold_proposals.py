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
import copy
import io
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fold_proposals as F  # noqa: E402


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def wikidata_entity(qid: str) -> dict:
    return {
        "id": qid,
        "labels": {"en": {"value": f"concept {qid}"}},
        "aliases": {"en": []},
        "descriptions": {"en": {"value": "synthetic concept"}},
        "claims": {"P31": []},
        "sitelinks": {},
    }


def run_fake_fetch(qids: list[str], fake_run, fake_sleep=None) -> dict[str, dict]:
    old_run = F.subprocess.run
    old_sleep = F.time.sleep
    try:
        F.subprocess.run = fake_run
        F.time.sleep = fake_sleep or (lambda _seconds: None)
        return F.fetch_entities(qids)
    finally:
        F.subprocess.run = old_run
        F.time.sleep = old_sleep


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


def test_qid_syntax_is_canonical_and_checked_before_request():
    assert F.is_qid("Q1")
    assert F.is_qid("Q999999999999999")
    for value in ("Q0", "Q01", "q1", "Q-1", "Q", 1, None, {"qid": "Q1"}):
        assert not F.is_qid(value)

    called = False

    def must_not_run(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("invalid QID reached curl")

    try:
        run_fake_fetch(["Q0"], must_not_run)
    except F.WikidataAcquisitionError:
        pass
    else:
        raise AssertionError("non-canonical QID was accepted")
    assert not called


def test_fetch_entities_canonical_success_and_redirect():
    entity = wikidata_entity("Q1")
    entity["aliases"]["en"] = [{"value": "one"}]
    entity["claims"]["P31"] = [{
        "mainsnak": {"datavalue": {"value": {"id": "Q5"}}},
    }]
    entity["sitelinks"]["enwiki"] = {"title": "Concept one"}
    result = run_fake_fetch(
        ["Q1"],
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"entities": {"Q1": entity}}),
            stderr="",
        ),
    )
    assert result == {"Q1": {
        "qid": "Q1", "requested": "Q1", "label": "concept Q1",
        "aliases": ["one"], "description": "synthetic concept",
        "classes": ["Q5"], "enwiki_slug": "Concept_one",
    }}

    target = wikidata_entity("Q2")
    redirected = run_fake_fetch(
        ["Q1"],
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps({
                "redirects": [{"from": "Q1", "to": "Q2"}],
                "entities": {"Q2": target},
            }),
            stderr="",
        ),
    )
    assert redirected["Q1"]["requested"] == "Q1"
    assert redirected["Q1"]["qid"] == "Q2"
    assert redirected["Q1"]["label"] == "concept Q2"


def test_fetch_entities_isolates_no_such_entity_and_preserves_valid_peers():
    bad = "Q999999999999999"
    calls: list[list[str]] = []
    sleeps: list[int] = []

    def fake_run(args, **_kwargs):
        ids = args[-1].split("&ids=", 1)[1].split("|")
        calls.append(ids)
        if bad in ids:
            payload = {"error": {"code": "no-such-entity", "info": "bad ID"}}
        else:
            payload = {"entities": {qid: wikidata_entity(qid) for qid in ids}}
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    result = run_fake_fetch(
        ["Q1", bad, "Q2"], fake_run, lambda seconds: sleeps.append(seconds)
    )
    assert result["Q1"]["qid"] == "Q1"
    assert result[bad] == {"missing": True}
    assert result["Q2"]["qid"] == "Q2"
    assert calls == [
        ["Q1", bad, "Q2"], ["Q1"], [bad, "Q2"], [bad], ["Q2"],
    ]
    assert sleeps == [1] * len(calls)

    normal_missing = run_fake_fetch(
        ["Q3"],
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"entities": {"Q3": {"id": "Q3", "missing": ""}}}),
            stderr="",
        ),
    )
    assert normal_missing == {"Q3": {"missing": True}}


def test_fetch_entities_keeps_other_api_errors_fatal():
    calls = 0

    def fake_run(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"error": {"code": "maxlag", "info": "retry"}}),
            stderr="",
        )

    try:
        run_fake_fetch(["Q1", "Q2"], fake_run)
    except F.WikidataAcquisitionError as exc:
        assert "API error" in str(exc)
    else:
        raise AssertionError("non-no-such API error did not fail closed")
    assert calls == 1


def test_fetch_entities_rejects_wrong_scalar_and_list_item_types():
    cases: list[tuple[str, dict]] = []
    for name in ("id", "label", "alias", "description", "p31", "sitelink"):
        entity = copy.deepcopy(wikidata_entity("Q1"))
        if name == "id":
            entity["id"] = 1
        elif name == "label":
            entity["labels"]["en"]["value"] = 1
        elif name == "alias":
            entity["aliases"]["en"] = [{"value": 1}]
        elif name == "description":
            entity["descriptions"]["en"]["value"] = 1
        elif name == "p31":
            entity["claims"]["P31"] = [{
                "mainsnak": {"datavalue": {"value": {"id": 5}}},
            }]
        else:
            entity["sitelinks"]["enwiki"] = {"title": 1}
        cases.append((name, entity))

    for name, entity in cases:
        try:
            run_fake_fetch(
                ["Q1"],
                lambda *_args, _entity=entity, **_kwargs: SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({"entities": {"Q1": _entity}}),
                    stderr="",
                ),
            )
        except F.WikidataAcquisitionError:
            pass
        else:
            raise AssertionError(f"wrong {name} scalar/list item type was accepted")


def test_fetch_entities_rejects_request_and_parse_failures():
    old_run = F.subprocess.run
    old_sleep = F.time.sleep
    scenarios = [
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired("curl", 120)),
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=22, stdout="", stderr="upstream failed"),
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout="not json", stderr=""),
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout=json.dumps({"entities": {}}), stderr=""),
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"entities": {"Q1": {"labels": []}}}),
            stderr=""),
    ]
    try:
        F.time.sleep = lambda _seconds: None
        for fake_run in scenarios:
            F.subprocess.run = fake_run
            try:
                F.fetch_entities(["Q1"])
            except F.WikidataAcquisitionError:
                pass
            else:
                raise AssertionError("failed Wikidata batch did not fail closed")
    finally:
        F.subprocess.run = old_run
        F.time.sleep = old_sleep


def test_external_page_ids_rejects_interrupted_first_publication():
    tmp = Path(tempfile.mkdtemp(prefix="fold_proposals_external_orphan_test_"))
    external = tmp / "external"
    external.mkdir(parents=True)
    controls = F.external_pair_control_paths(external, "fixture")
    controls["journal"].write_text("{}\n")
    old_catalog = F.CATALOG
    old_reader = F.read_stable_external_pair
    old_cache = F._ext_page_ids
    called = False

    def reject_orphan(db, pages, links):
        nonlocal called
        called = True
        assert db == "fixture"
        assert pages == external / "fixture_pages.jsonl"
        assert links == external / "fixture_links.jsonl"
        raise ValueError("interrupted first publication")

    try:
        F.CATALOG = tmp
        F.read_stable_external_pair = reject_orphan
        F._ext_page_ids = {}
        try:
            F.external_page_ids("fixture")
        except ValueError:
            pass
        else:
            raise AssertionError("orphan links were treated as clean absence")
        assert called
    finally:
        F.CATALOG = old_catalog
        F.read_stable_external_pair = old_reader
        F._ext_page_ids = old_cache
        shutil.rmtree(tmp, ignore_errors=True)


def test_main_preserves_every_output_after_partial_acquisition_failure():
    tmp = Path(tempfile.mkdtemp(prefix="fold_proposals_fetch_failure_test_"))
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
        "crossref_sources": {}, "frontier_sources": {},
    }))
    (catalog / "rebuild_grounding.json").write_text("[]")
    write_jsonl(catalog / "wikidata_universe.jsonl", [])
    write_jsonl(catalog / "universe_extension.jsonl", [{"sentinel": "extension"}])
    write_jsonl(catalog / "grounding_overrides.jsonl", [{"sentinel": "override"}])

    qids = [f"Q{1000 + i}" for i in range(51)]
    write_jsonl(proposals / "partial_fetch.jsonl", [
        {"qid": qid, "qid_label": f"concept {qid}",
         "decl": "Mathlib.Example", "module": "Mathlib.Test",
         "confidence": "medium", "evidence": "synthetic discovery",
         "proposer": "test"}
        for qid in qids
    ])

    # Seed every direct output family with recognizable bytes.  The absent
    # discovery_rejected.jsonl also proves no new dump/temp file is created.
    for name in ("container_links.jsonl", "discovery_proposals.jsonl",
                 "grading_disputes.jsonl", "ext_anchor_links.jsonl",
                 "fc_links.jsonl"):
        (data / name).write_bytes(f"sentinel:{name}\n".encode())

    old = {
        "CATALOG": F.CATALOG,
        "PROPOSALS": F.PROPOSALS,
        "DATA": F.DATA,
        "CHECKOUT": F.CHECKOUT,
        "ORACLE": F.ORACLE,
        "fetch_entities": F.fetch_entities,
        "run": F.subprocess.run,
        "sleep": F.time.sleep,
        "frontier_names": F._frontier_names,
        "ext_page_ids": F._ext_page_ids,
        "oracle_modules": F._oracle_modules,
    }
    calls = 0

    def partial_then_fail(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            entities = {qid: wikidata_entity(qid) for qid in sorted(qids)[:50]}
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"entities": entities}),
                stderr="",
            )
        return SimpleNamespace(returncode=28, stdout="", stderr="timeout")

    try:
        F.CATALOG = catalog
        F.PROPOSALS = proposals
        F.DATA = data
        F.CHECKOUT = checkout
        F.ORACLE = oracle
        F.subprocess.run = partial_then_fail
        F.time.sleep = lambda _seconds: None
        F._frontier_names = {}
        F._ext_page_ids = {}
        F._oracle_modules = None
        before = tree_bytes(tmp)

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            assert F.main() == 1

        assert calls == 2
        assert "FATAL: Wikidata acquisition failed" in stderr.getvalue()
        assert "chunk 1 curl exited 28" in stderr.getvalue()
        assert tree_bytes(tmp) == before
        assert not (data / "discovery_rejected.jsonl").exists()
        assert not list(tmp.rglob("*.tmp"))

        F.fetch_entities = lambda requested: {
            requested[0]: {
                "qid": requested[0], "requested": requested[0],
                "label": f"concept {requested[0]}", "aliases": [],
                "description": "synthetic concept", "classes": [],
                "enwiki_slug": None,
            },
        }
        outer_stderr = io.StringIO()
        with contextlib.redirect_stderr(outer_stderr):
            assert F.main() == 1
        assert "Wikidata acquisition returned only 1/51" in outer_stderr.getvalue()
        assert tree_bytes(tmp) == before
    finally:
        F.CATALOG = old["CATALOG"]
        F.PROPOSALS = old["PROPOSALS"]
        F.DATA = old["DATA"]
        F.CHECKOUT = old["CHECKOUT"]
        F.ORACLE = old["ORACLE"]
        F.fetch_entities = old["fetch_entities"]
        F.subprocess.run = old["run"]
        F.time.sleep = old["sleep"]
        F._frontier_names = old["frontier_names"]
        F._ext_page_ids = old["ext_page_ids"]
        F._oracle_modules = old["oracle_modules"]
        shutil.rmtree(tmp, ignore_errors=True)


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
    veto_row = {
        "qid": "Q999002", "qid_label": "vetoed concept",
        "decl": "Mathlib.Example", "module": "Mathlib.Test",
        "confidence": "medium", "evidence": "conflicting discovery",
        "proposer": "test",
    }
    rejected_shard = proposals / "veto_rejected.jsonl"
    write_jsonl(rejected_shard, [veto_row])
    write_jsonl(Path(str(rejected_shard) + ".verified.jsonl"), [{
        **veto_row, "verdict": "reject", "verify_note": "wrong concept",
    }])
    accepted_shard = proposals / "veto_accepted.jsonl"
    write_jsonl(accepted_shard, [veto_row])
    write_jsonl(Path(str(accepted_shard) + ".verified.jsonl"), [{
        **veto_row, "verdict": "accept", "verify_note": "duplicate accepted",
    }])
    reject_only = {
        "qid": "Q999003", "qid_label": "rejected concept",
        "decl": "Mathlib.Example", "module": "Mathlib.Test",
        "confidence": "medium", "evidence": "rejected discovery",
        "proposer": "test",
    }
    reject_only_shard = proposals / "reject_only.jsonl"
    write_jsonl(reject_only_shard, [reject_only])
    write_jsonl(Path(str(reject_only_shard) + ".verified.jsonl"), [{
        **reject_only, "verdict": "reject", "verify_note": "wrong concept",
    }])
    write_jsonl(proposals / "locally_invalid.jsonl", [
        {"qid": "Q0", "qid_label": "invalid qid", "decl": "Mathlib.Example"},
        {"qid": 123, "qid_label": "invalid qid", "decl": "Mathlib.Example"},
        {"qid": "Q999004", "path": "path:not/in/hierarchy"},
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
    fetch_calls: list[list[str]] = []

    def fetch_known(qids: list[str]) -> dict[str, dict]:
        fetch_calls.append(list(qids))
        return {qid: fetched[qid] for qid in qids}

    try:
        F.CATALOG = catalog
        F.PROPOSALS = proposals
        F.DATA = data
        F.CHECKOUT = checkout
        F.ORACLE = oracle
        F.fetch_entities = fetch_known
        F._frontier_names = {}
        F._ext_page_ids = {}
        F._oracle_modules = None

        first_stdout = io.StringIO()
        with contextlib.redirect_stdout(first_stdout):
            assert F.main() == 0
        assert fetch_calls == [["Q999001"]]

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
        assert fetch_calls == [["Q999001"]]
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
