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


def bundle_entity(
    requested: str,
    *,
    qid: str | None = None,
    label: str | None = None,
) -> dict:
    return {
        "qid": qid or requested,
        "requested": requested,
        "label": label if label is not None else f"concept {requested}",
        "aliases": [],
        "description": "synthetic concept",
        "classes": [],
        "enwiki_slug": None,
    }


def make_fold_fixture(tmp: Path, rows: list[dict]) -> tuple[Path, Path, Path, Path, Path]:
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
    write_jsonl(proposals / "requests.jsonl", rows)
    return catalog, proposals, data, checkout, oracle


@contextlib.contextmanager
def patched_fold_fixture(
    catalog: Path, proposals: Path, data: Path, checkout: Path, oracle: Path
):
    old = {
        "CATALOG": F.CATALOG,
        "PROPOSALS": F.PROPOSALS,
        "DATA": F.DATA,
        "CHECKOUT": F.CHECKOUT,
        "ORACLE": F.ORACLE,
        "verify": F.verify_wikidata_entity_bundle,
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
        F._frontier_names = {}
        F._ext_page_ids = {}
        F._oracle_modules = None
        yield
    finally:
        F.CATALOG = old["CATALOG"]
        F.PROPOSALS = old["PROPOSALS"]
        F.DATA = old["DATA"]
        F.CHECKOUT = old["CHECKOUT"]
        F.ORACLE = old["ORACLE"]
        F.verify_wikidata_entity_bundle = old["verify"]
        F._frontier_names = old["frontier_names"]
        F._ext_page_ids = old["ext_page_ids"]
        F._oracle_modules = old["oracle_modules"]


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
    assert F.is_qid("Q999999999999")
    for value in (
        "Q0", "Q01", "q1", "Q-1", "Q", "Q1000000000000", 1, None,
        {"qid": "Q1"},
    ):
        assert not F.is_qid(value)


def test_request_plan_is_exact_canonical_atomic_and_does_not_fold():
    tmp = Path(tempfile.mkdtemp(prefix="fold_proposals_plan_test_"))
    rows = [
        {"qid": "Q200", "qid_label": "concept Q200", "decl": "Mathlib.Example"},
        {"qid": "Q100", "qid_label": "concept Q100", "decl": "Mathlib.Example"},
        {"qid": "Q0", "qid_label": "invalid", "decl": "Mathlib.Example"},
    ]
    catalog, proposals, data, checkout, oracle = make_fold_fixture(tmp, rows)
    plan = tmp / "request-plan.json"
    plan.write_bytes(b"stale-plan")
    called = False

    def must_not_verify(_path):
        nonlocal called
        called = True
        raise AssertionError("planning mode attempted bundle verification")

    try:
        with patched_fold_fixture(catalog, proposals, data, checkout, oracle):
            F.verify_wikidata_entity_bundle = must_not_verify
            catalog_before = tree_bytes(catalog)
            assert F.main(["--write-wikidata-request-plan", str(plan)]) == 0
            assert plan.read_bytes() == (
                b'{"qids":["Q100","Q200"],'
                b'"schema":"wikilean.wikidata-entity-request-plan/v1"}'
            )
            assert not called
            assert not list(data.iterdir())
            assert tree_bytes(catalog) == catalog_before
            assert not list(tmp.rglob("*.tmp"))

            # Empty is still an exact plan and atomically replaces the stale
            # non-empty one; acquisition can then be skipped.
            write_jsonl(proposals / "requests.jsonl", [])
            assert F.main(["--write-wikidata-request-plan", str(plan)]) == 0
            assert plan.read_bytes() == (
                b'{"qids":[],"schema":"wikilean.wikidata-entity-request-plan/v1"}'
            )
            assert not called
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_fold_contains_no_live_wikidata_transport():
    source = Path(F.__file__).read_text()
    assert "_fetch_entity_chunk" not in source
    assert "fetch_entities" not in source
    assert "wbgetentities" not in source
    assert "wikidata.org/w/api.php" not in source


def test_empty_need_rejects_a_bundle_and_folds_without_one():
    tmp = Path(tempfile.mkdtemp(prefix="fold_proposals_empty_bundle_test_"))
    catalog, proposals, data, checkout, oracle = make_fold_fixture(tmp, [])
    bundle_path = (tmp / "unneeded-bundle").absolute()
    called = False

    def must_not_verify(_path):
        nonlocal called
        called = True
        raise AssertionError("empty request set attempted bundle verification")

    try:
        with patched_fold_fixture(catalog, proposals, data, checkout, oracle):
            F.verify_wikidata_entity_bundle = must_not_verify
            before = tree_bytes(tmp)
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                assert F.main([
                    "--wikidata-entity-bundle", str(bundle_path),
                ]) == 1
            assert "not allowed when no unknown QIDs" in stderr.getvalue()
            assert tree_bytes(tmp) == before
            assert not called

            assert F.main([]) == 0
            assert not called
            assert (data / "discovery_proposals.jsonl").read_bytes() == b""
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


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


def test_main_preserves_every_output_on_bundle_failure_or_wrong_coverage():
    tmp = Path(tempfile.mkdtemp(prefix="fold_proposals_bundle_failure_test_"))
    qids = ["Q1001", "Q1002"]
    rows = [
        {"qid": qid, "qid_label": f"concept {qid}",
         "decl": "Mathlib.Example", "module": "Mathlib.Test",
         "confidence": "medium", "evidence": "synthetic discovery",
         "proposer": "test"}
        for qid in qids
    ]
    catalog, proposals, data, checkout, oracle = make_fold_fixture(tmp, rows)
    for name in (
        "container_links.jsonl", "discovery_proposals.jsonl",
        "grading_disputes.jsonl", "ext_anchor_links.jsonl", "fc_links.jsonl",
    ):
        (data / name).write_bytes(f"sentinel:{name}\n".encode())
    bundle_path = (tmp / "sealed-bundle").absolute()

    try:
        with patched_fold_fixture(catalog, proposals, data, checkout, oracle):
            before = tree_bytes(tmp)

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                assert F.main([]) == 1
            assert "require --wikidata-entity-bundle" in stderr.getvalue()
            assert tree_bytes(tmp) == before

            verifier_called = False

            def must_not_verify_relative(_path):
                nonlocal verifier_called
                verifier_called = True
                raise AssertionError("relative bundle path reached verifier")

            F.verify_wikidata_entity_bundle = must_not_verify_relative
            with contextlib.redirect_stderr(io.StringIO()):
                assert F.main(["--wikidata-entity-bundle", "relative/bundle"]) == 1
            assert not verifier_called
            assert tree_bytes(tmp) == before

            def reject_bundle(_path):
                raise F.WikidataEntityBundleError("tampered bundle")

            F.verify_wikidata_entity_bundle = reject_bundle
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                assert F.main([
                    "--wikidata-entity-bundle", str(bundle_path),
                ]) == 1
            assert "bundle verification failed: tampered bundle" in stderr.getvalue()
            assert tree_bytes(tmp) == before

            wrong_bundles = [
                SimpleNamespace(
                    requested_qids=("Q1001",),
                    entities={"Q1001": bundle_entity("Q1001")},
                ),
                SimpleNamespace(
                    requested_qids=("Q1001", "Q1003"),
                    entities={
                        "Q1001": bundle_entity("Q1001"),
                        "Q1003": bundle_entity("Q1003"),
                    },
                ),
                SimpleNamespace(
                    requested_qids=tuple(qids),
                    entities={"Q1001": bundle_entity("Q1001")},
                ),
                SimpleNamespace(
                    requested_qids=tuple(qids),
                    entities={
                        **{qid: bundle_entity(qid) for qid in qids},
                        "Q1003": bundle_entity("Q1003"),
                    },
                ),
            ]
            for wrong in wrong_bundles:
                F.verify_wikidata_entity_bundle = lambda _path, value=wrong: value
                with contextlib.redirect_stderr(io.StringIO()):
                    assert F.main([
                        "--wikidata-entity-bundle", str(bundle_path),
                    ]) == 1
                assert tree_bytes(tmp) == before

            assert not (data / "discovery_rejected.jsonl").exists()
            assert not list(tmp.rglob("*.tmp"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_main_preserves_requested_qid_on_redirect_and_rejects_explicit_missing():
    tmp = Path(tempfile.mkdtemp(prefix="fold_proposals_bundle_success_test_"))
    redirected, missing = "Q999001", "Q999002"
    rows = [
        {"qid": redirected, "qid_label": "target concept",
         "decl": "Mathlib.Example", "module": "Mathlib.Test",
         "confidence": "medium", "evidence": "redirected discovery",
         "proposer": "test"},
        {"qid": missing, "qid_label": "missing concept",
         "decl": "Mathlib.Example", "module": "Mathlib.Test",
         "confidence": "medium", "evidence": "missing discovery",
         "proposer": "test"},
    ]
    catalog, proposals, data, checkout, oracle = make_fold_fixture(tmp, rows)
    write_jsonl(catalog / "universe_extension.jsonl", [])
    write_jsonl(catalog / "grounding_overrides.jsonl", [])
    bundle_path = (tmp / "sealed-bundle").absolute()
    calls: list[Path] = []

    redirected_entity = bundle_entity(
        redirected, qid="Q2", label="target concept"
    )
    redirected_entity.update({
        "aliases": ["requested concept"],
        "description": "target description",
        "classes": ["Q1936384"],
        "enwiki_slug": "Target_concept",
    })
    bundle = SimpleNamespace(
        requested_qids=(redirected, missing),
        entities={redirected: redirected_entity, missing: {"missing": True}},
    )

    def verify(path: Path):
        calls.append(path)
        return bundle

    try:
        with patched_fold_fixture(catalog, proposals, data, checkout, oracle):
            F.verify_wikidata_entity_bundle = verify
            assert F.main([
                "--wikidata-entity-bundle", str(bundle_path),
            ]) == 0

        assert calls == [bundle_path]
        discovery = read_jsonl(data / "discovery_proposals.jsonl")
        assert [row["src"] for row in discovery] == [redirected]
        rejected = read_jsonl(data / "discovery_rejected.jsonl")
        assert len(rejected) == 1
        assert rejected[0]["qid"] == missing
        assert rejected[0]["rejected_reason"] == "fold-check: qid missing upstream"
        assert read_jsonl(catalog / "universe_extension.jsonl") == [{
            "qid": redirected,
            "label": "target concept",
            "description": "target description",
            "classes": ["Q1936384"],
            "enwiki_slug": "Target_concept",
            "source": "discovery",
        }]
    finally:
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
        "verify": F.verify_wikidata_entity_bundle,
        "frontier_names": F._frontier_names,
        "ext_page_ids": F._ext_page_ids,
        "oracle_modules": F._oracle_modules,
    }
    bundle_path = (tmp / "sealed-bundle").absolute()
    verify_calls: list[Path] = []

    def verify_bundle(path: Path):
        verify_calls.append(path)
        return SimpleNamespace(
            requested_qids=("Q999001",),
            entities=fetched,
        )

    try:
        F.CATALOG = catalog
        F.PROPOSALS = proposals
        F.DATA = data
        F.CHECKOUT = checkout
        F.ORACLE = oracle
        F.verify_wikidata_entity_bundle = verify_bundle
        F._frontier_names = {}
        F._ext_page_ids = {}
        F._oracle_modules = None

        first_stdout = io.StringIO()
        with contextlib.redirect_stdout(first_stdout):
            assert F.main([
                "--wikidata-entity-bundle", str(bundle_path),
            ]) == 0
        assert verify_calls == [bundle_path]

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
            assert F.main([]) == 0
        assert verify_calls == [bundle_path]
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
        F.verify_wikidata_entity_bundle = old["verify"]
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
