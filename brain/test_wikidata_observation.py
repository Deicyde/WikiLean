#!/usr/bin/env python3
"""Hermetic acquisition, identity, replay and crash tests for shared Wikidata observations."""
from __future__ import annotations

import copy
import base64
import contextlib
import io
import json
import multiprocessing
import os
import signal
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "brain"))
sys.path.insert(0, str(ROOT / "catalog" / "mathlib_deps"))
sys.path.insert(0, str(ROOT / "brain" / "ingest"))
import acquire_wikidata_observation as acquire  # noqa: E402
import install_wikidata_observation as installer  # noqa: E402
import plan_wikidata_observation as planning  # noqa: E402
import wikidata_observation as observation  # noqa: E402
import verify_wikidata_observation as verification  # noqa: E402

AUDIT = "2026-09-08T12:00:00Z"


def fixture_plan(root: Path) -> dict:
    paths = {}
    contents = {
        "concept-layer": observation.artifact({"qid": "Q1"}),
        "prior-brain-nodes": observation.artifact({"id": "Q2", "type": "concept"})
            + observation.artifact({"id": "decl:Nat.add", "type": "decl"}),
        "grounding": observation.canonical([{"qid": "Q1"}]),
        "universe-extension": observation.artifact({"qid": "Q2"}),
        "wikidata-crossrefs": observation.canonical({"xrefs": {"Q3": {}}}),
    }
    for name, raw in contents.items():
        path = root / (name + ".json")
        path.write_bytes(raw)
        paths[name] = path
    return planning.build_plan(paths)


def fixtures(plan: dict) -> list[dict]:
    records = []
    for index, request in enumerate(observation.requests_for(plan)):
        if request.stage == "universe":
            data = {"results": {"bindings": [{
                "x": {"value": "http://www.wikidata.org/entity/Q1"},
                "xLabel": {"value": "One"},
                "article": {"value": "https://en.wikipedia.org/wiki/One"},
            }]}}
        elif request.stage == "edges":
            data = {"results": {"bindings": [{
                "s": {"value": "http://www.wikidata.org/entity/Q1"},
                "p": {"value": "http://www.wikidata.org/entity/P31"},
                "pLabel": {"value": "instance of"},
                "o": {"value": "http://www.wikidata.org/entity/Q2"},
            }]}}
        else:
            data = {"entities": {q: {"id": q, "type": "item", "descriptions": {
                "en": {"language": "en", "value": "description " + q}}} for q in request.subjects}}
        records.append(observation.response_record(index, request, observation.canonical(data), 200,
            "application/json" if request.stage == "descriptions" else "application/sparql-results+json"))
    return records


def replace_response(records: list[dict], plan: dict, index: int, data: dict) -> list[dict]:
    records = copy.deepcopy(records)
    records[index] = observation.response_record(index, observation.requests_for(plan)[index],
        observation.canonical(data), 200, "application/json")
    return records


def publish_in_child(plan: dict, records: list, store: str, toolchain: dict, queue) -> None:
    try:
        result = acquire.publish_records(plan, records, store=Path(store), toolchain=toolchain, audit_time=AUDIT)
        queue.put(str(result))
    except BaseException as exc:
        queue.put(type(exc).__name__ + ": " + str(exc))


class ObservationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        registry = observation.reviewed_profiles()
        current = next(item for item in registry["profiles"] if item["profile_id"] == registry["current_profile"])
        # Offline replay fixtures retain recorded identities when the checkout advances.
        cls.toolchain = {
            "schema": observation.TOOLCHAIN_SCHEMA, "profile_id": current["profile_id"],
            "observation_policy": observation.OBSERVATION_POLICY,
            "transport_policy": copy.deepcopy(observation.TRANSPORT_POLICY),
            "python": {"implementation": "CPython", "version": "3.12.13", "startup": ["-I", "-S"], "sha256": "1" * 64},
            "curl": {"version": "curl fixture", "sha256": "2" * 64},
            "files": copy.deepcopy(current["files"]),
        }

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        # macOS TemporaryDirectory uses /var, which resolves through /private/var.
        self.root = Path(self.temporary.name).resolve()
        self.plan = fixture_plan(self.root)
        self.records = fixtures(self.plan)
        self.store = self.root / "store"

    def publish(self, *, audit=AUDIT, records=None, before_publish=None) -> Path:
        return acquire.publish_records(self.plan, self.records if records is None else records,
            store=self.store, toolchain=self.toolchain, audit_time=audit, before_publish=before_publish)

    def test_complete_bundle_round_trip_and_three_coherent_outputs(self) -> None:
        path = self.publish()
        verified = observation.verify_bundle(path)
        self.assertEqual(set(verified["normalized"]), set(observation.OUTPUTS))
        self.assertEqual(verified["manifest"]["observation_policy"], "independent-live-requests/no-snapshot")
        self.assertEqual(verified["lineage"]["acquisition_receipt_ids"],
                         [verified["receipt"]["acquisition_receipt_id"]])
        self.assertEqual(len(verified["lineage"]["outputs"]), 3)
        self.assertEqual(verified["receipt"]["batch"]["requests_total"], 14)

    def test_exported_v3_source_binds_shared_evidence_and_exact_configuration_preimages(self) -> None:
        path = self.publish()
        source = verification.source_entry(path, root_name="observation",
            license_policy={"expression": "CC0-1.0", "redistribution": "allowed"})
        import source_plan_contracts
        manifest = source_plan_contracts._source_manifest_from_plan(source, "fixture")
        bundle = observation.verify_bundle(path)
        evidence = source["evidence"]
        observation.contracts.validate_source_manifest_evidence_documents(manifest,
            receipts={bundle["receipt"]["acquisition_receipt_id"]: bundle["receipt"]},
            lineage=bundle["lineage"],
            request_parameter_preimages={item["parameters_sha256"]: {
                "parameters_sha256": item["parameters_sha256"], "bytes": item["bytes"], "media_type": item["media_type"]}
                for item in evidence["request_parameter_preimages"]})
        self.assertTrue({"request_plan", "toolchain"}.issubset(
            {item["name"] for item in source["objects"] if item["roles"] == ["receipt"]}))
        newer = self.publish(audit="2026-09-09T12:00:00Z")
        newer_source = verification.source_entry(newer, root_name="observation",
            license_policy={"expression": "CC0-1.0", "redistribution": "allowed"})
        newer_manifest = source_plan_contracts._source_manifest_from_plan(newer_source, "fixture")
        self.assertEqual(manifest["source_manifest_id"], newer_manifest["source_manifest_id"])

    def test_standalone_legacy_publishers_are_retired_before_network(self) -> None:
        for relative in ("catalog/mathlib_deps/fetch_wikidata_universe.py",
                         "catalog/mathlib_deps/fetch_wikidata_edges.py",
                         "brain/ingest/wikidata_descriptions.py"):
            result = subprocess.run([sys.executable, str(ROOT / relative)],
                                    capture_output=True, text=True, timeout=15)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Standalone Wikidata", result.stderr)
            self.assertIn("is retired", result.stderr)

    def test_later_unchanged_observation_has_new_evidence_and_same_logical_content(self) -> None:
        first = observation.verify_bundle(self.publish())
        second = observation.verify_bundle(self.publish(audit="2026-09-09T12:00:00Z"))
        self.assertNotEqual(first["bundle_id"], second["bundle_id"])
        self.assertEqual(first["normalized"], second["normalized"])
        self.assertEqual(first["receipt"]["acquisition_receipt_id"], second["receipt"]["acquisition_receipt_id"])
        self.assertEqual(first["lineage"]["normalization_lineage_id"], second["lineage"]["normalization_lineage_id"])

    def test_duplicate_publication_verifies_reuse(self) -> None:
        first = self.publish()
        self.assertEqual(first, self.publish())
        self.assertEqual(len(list(self.store.glob("[0-9a-f]" * 64))), 1)

    def test_installer_materializes_and_verifies_exact_three_file_family(self) -> None:
        path = self.publish()
        result = installer.install(self.root, path, plan_bytes=observation.canonical(self.plan))
        self.assertEqual(result["path"], path)
        for name, relative in installer.MIRRORS.items():
            self.assertEqual((self.root / relative).read_bytes(), result["normalized"][name])
        verified = installer.load_installed(self.root, required=True)
        self.assertEqual(verified["bundle_id"], result["bundle_id"])

    def test_concept_graph_builder_consumes_enabled_observation_edges(self) -> None:
        sys.path.insert(0, str(ROOT / "catalog"))
        import build_graph_v2 as graph
        installer.install(self.root, self.publish(), plan_bytes=observation.canonical(self.plan))
        catalog = self.root / "catalog"
        data = catalog / "data"
        oracle = self.root / "oracle.json"
        oracle.write_text('{"declarations":{"Fixture.foo":{}}}')
        checkout = self.root / "mathlib"
        checkout.mkdir()
        annotations = self.root / "site/annotations"
        annotations.mkdir(parents=True)
        live = data / "concept_graph.json"
        live.write_text(json.dumps({"nodes": [{"qid": "Q1"}, {"qid": "Q2"}], "edges": []}))
        grounding = data / "rebuild_grounding.json"
        grounding.write_text(json.dumps([
            {"qid": "Q1", "slug": "One", "formalizations": [{"decl": "Fixture.foo",
                "module": "Mathlib.Fixture", "match_kind": "exact", "confidence": "high"}]},
            {"qid": "Q2", "slug": "Two", "formalizations": []},
        ]))
        paths = {"HERE": catalog, "DATA": data, "LIVE": live, "ORACLE": oracle,
                 "CHECKOUT": checkout, "WD_EDGES": self.root / installer.MIRRORS["wikidata-edges"],
                 "ANNOT": annotations, "OUT": data / "concept_graph_v2.json",
                 "D2Q_OUT": data / "decl_to_qid_v2.json", "ROLES_OUT": data / "decl_qid_roles_v2.json",
                 "XREFS": data / "wikidata_crossrefs.json", "OVERRIDES": data / "grounding_overrides.jsonl"}
        with mock.patch.multiple(graph, **paths), mock.patch.object(graph.lift_formal_edges, "lift", return_value=[]), \
                mock.patch.object(sys, "argv", ["build_graph_v2.py", "--grounding", str(grounding)]), \
                mock.patch.object(installer, "load_installed", wraps=installer.load_installed) as load, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(graph.main(), 0)
        load.assert_called_once_with(self.root)
        built = json.loads(paths["OUT"].read_text())
        self.assertEqual(built["edges"], [{"from": "Q1", "to": "Q2", "source": "wikidata",
                                         "props": [{"p": "P31", "label": "instance of"}]}])
        self.assertEqual(json.loads(paths["D2Q_OUT"].read_text()), {"Fixture.foo": ["Q1"]})

    def test_installer_plan_mismatch_preserves_every_prior_mirror(self) -> None:
        path = self.publish()
        installer.install(self.root, path, plan_bytes=observation.canonical(self.plan))
        before = {relative: (self.root / relative).read_bytes() for relative in installer.MIRRORS.values()}
        changed = copy.deepcopy(self.plan)
        changed["volume_floors"]["edges"] = 1
        with self.assertRaisesRegex(observation.ObservationError, "reviewed request plan"):
            installer.install(self.root, path, plan_bytes=observation.canonical(changed))
        self.assertEqual(before, {relative: (self.root / relative).read_bytes() for relative in installer.MIRRORS.values()})

    def test_reader_rejects_loose_mirror_and_unexpected_run_generation(self) -> None:
        path = self.publish()
        installer.install(self.root, path, plan_bytes=observation.canonical(self.plan))
        with mock.patch.dict(os.environ, {"WIKILEAN_WIKIDATA_OBSERVATION_BUNDLE": str(self.root / "other")}):
            with self.assertRaisesRegex(observation.ObservationError, "this run"):
                installer.load_installed(self.root)
        (self.root / installer.MIRRORS["wikidata-edges"]).write_bytes(b"changed\n")
        with self.assertRaisesRegex(observation.ObservationError, "mirror generation mismatch"):
            installer.load_installed(self.root)

    def test_partial_install_rolls_back_whole_family(self) -> None:
        path = self.publish()
        installer.install(self.root, path, plan_bytes=observation.canonical(self.plan))
        second = self.publish(audit="2026-09-09T12:00:00Z")
        def fail(index):
            if index == 1:
                raise RuntimeError("injected middle-of-install failure")
        with self.assertRaisesRegex(RuntimeError, "middle-of-install"):
            installer.install(self.root, second, plan_bytes=observation.canonical(self.plan), after_replace=fail)
        self.assertEqual(installer.load_installed(self.root, required=True)["path"], path)
        self.assertFalse((self.root / installer.STATE / "journal.json").exists())

    def test_recovery_rejects_redirected_parent_before_any_repair(self) -> None:
        state = installer._state(self.root, create=True)
        transaction = state / ("transaction-" + "a" * 32)
        transaction.mkdir(mode=0o700)
        previous = [{"path": relative, "sha256": None, "bytes": 0}
                    for relative in [*installer.MIRRORS.values(), installer.MARKER]]
        journal = state / "journal.json"
        journal.write_bytes(observation.canonical({"schema": installer.JOURNAL_SCHEMA,
            "transaction": transaction.name, "previous": previous}))
        first = self.root / installer.MIRRORS["wikidata-universe"]
        first.parent.mkdir(parents=True)
        first.write_bytes(b"new installed universe")
        external = self.root / "external"
        external.mkdir()
        sentinel = external / "wikidata_edges.jsonl"
        sentinel.write_bytes(b"unrelated external data")
        (self.root / "catalog/mathlib_deps").symlink_to(external, target_is_directory=True)
        with self.assertRaises(OSError):
            installer._recover(self.root, state)
        self.assertEqual(sentinel.read_bytes(), b"unrelated external data")
        self.assertEqual(first.read_bytes(), b"new installed universe")
        self.assertTrue(journal.exists())

    def test_dangling_installation_state_never_falls_back_to_legacy(self) -> None:
        state = self.root / installer.STATE
        state.parent.mkdir(parents=True)
        state.symlink_to(self.root / "nonexistent", target_is_directory=True)
        with self.assertRaisesRegex(observation.ObservationError, "linked installation"):
            installer.load_installed(self.root)

    def test_published_bundle_requires_private_directories_and_owned_file_modes(self) -> None:
        path = self.publish()
        for target, mode in ((path, 0o777), (path / "requests", 0o755),
                             (path / "bundle.json", 0o666), (path / "normalized/wikidata_edges.jsonl", 0o664)):
            original = target.stat().st_mode & 0o777
            target.chmod(mode)
            try:
                with self.assertRaisesRegex(observation.ObservationError, "current-user-owned"):
                    observation.verify_bundle(path)
            finally:
                target.chmod(original)

    def test_prior_reviewed_whole_generation_remains_verifiable_after_checkout_changes(self) -> None:
        registry = copy.deepcopy(observation.reviewed_profiles())
        previous = copy.deepcopy(next(item for item in registry["profiles"]
                                     if item["profile_id"] == registry["current_profile"]))
        previous["files"][0]["sha256"] = "3" * 64
        previous["files"][-1]["sha256"] = "4" * 64
        previous["profile_id"] = observation.profile_identity(previous)
        registry["profiles"].append(previous)
        registry["profiles"].sort(key=lambda item: item["profile_id"])
        toolchain = copy.deepcopy(self.toolchain)
        toolchain.update({"profile_id": previous["profile_id"], "files": previous["files"]})
        with mock.patch.object(observation, "reviewed_profiles", return_value=registry):
            path = acquire.publish_records(self.plan, self.records, store=self.store,
                toolchain=toolchain, audit_time=AUDIT)
            with mock.patch.object(observation, "ROOT", self.root / "absent-future-checkout"):
                verified = observation.verify_bundle(path)
            self.assertEqual(verified["lineage"]["tool"]["sha256"], "4" * 64)
            mixed = copy.deepcopy(toolchain)
            mixed["files"][0] = copy.deepcopy(self.toolchain["files"][0])
            with self.assertRaisesRegex(observation.ObservationError, "one reviewed implementation generation"):
                observation.validate_toolchain(mixed)

    def test_live_producer_rejects_unreviewed_helper_bytes_before_transport(self) -> None:
        original = observation.read_regular
        def changed_helper(path, **kwargs):
            raw = original(path, **kwargs)
            return raw + b"\n# unreviewed\n" if path == ROOT / "brain/tools/execution_environment.py" else raw
        with mock.patch.object(observation, "read_regular", side_effect=changed_helper):
            with self.assertRaisesRegex(observation.ObservationError, "one reviewed implementation generation"):
                acquire.runtime_identity(Path("/usr/bin/curl"))

    @unittest.skipUnless(hasattr(os, "fork"), "requires POSIX process signals")
    def test_killed_install_is_rejected_then_recovered_before_next_install(self) -> None:
        first = self.publish()
        installer.install(self.root, first, plan_bytes=observation.canonical(self.plan))
        second = self.publish(audit="2026-09-09T12:00:00Z")
        pid = os.fork()
        if pid == 0:
            try:
                installer.install(self.root, second, plan_bytes=observation.canonical(self.plan),
                                  after_replace=lambda _index: os.kill(os.getpid(), signal.SIGKILL))
            finally:
                os._exit(3)
        _, status = os.waitpid(pid, 0)
        self.assertEqual(os.WTERMSIG(status), signal.SIGKILL)
        with self.assertRaisesRegex(observation.ObservationError, "unfinished"):
            installer.load_installed(self.root)
        installer.install(self.root, second, plan_bytes=observation.canonical(self.plan))
        self.assertEqual(installer.load_installed(self.root)["path"], second)

    def test_plan_binds_prior_nodes_and_preserves_legacy_selectors(self) -> None:
        self.assertEqual(self.plan["edge_qids"], ["Q1", "Q2"])
        self.assertEqual(self.plan["description_qids"], ["Q1", "Q2", "Q3"])
        prior = next(x for x in self.plan["selection_inputs"] if x["name"] == "prior-brain-nodes")
        self.assertEqual(prior["sha256"], observation.sha((self.root / "prior-brain-nodes.json").read_bytes()))
        (self.root / "prior-brain-nodes.json").unlink()
        # Replay succeeds after every ambient planning file is gone.
        for item in self.root.glob("*.json"):
            item.unlink()
        observation.verify_bundle(self.publish())

    def test_selector_corpus_preserves_non_nfc_docstrings_and_rejects_malformed_json(self) -> None:
        raw = ('{"id":"Q2","type":"concept","docstring":"Cafe\\u0301"}\n'
               '{"id":"decl:Foo","type":"decl","docstring":"de\\u0301f"}\n').encode()
        path = self.root / "prior-brain-nodes.json"
        path.write_bytes(raw)
        plan = planning.build_plan({name: self.root / (name + ".json") for name in observation.SELECTION_NAMES})
        prior = next(item for item in plan["selection_inputs"] if item["name"] == "prior-brain-nodes")
        self.assertEqual(prior["qids"], ["Q2"])
        self.assertEqual(prior["sha256"], observation.sha(raw))
        self.assertEqual(path.read_bytes(), raw)
        for invalid in (b'{"id":"Q1","id":"Q2","type":"concept"}',
                        b'{"id":"Q1","type":"concept","count":NaN}'):
            with self.assertRaisesRegex(observation.ObservationError, "invalid corpus JSON"):
                planning._selected(invalid, "prior-brain-nodes")

    def test_upstream_artifact_strings_preserve_non_nfc_content(self) -> None:
        index = len(self.records) - 1
        request = observation.requests_for(self.plan)[index]
        value = "Cafe\u0301"
        data = {"entities": {qid: {"id": qid, "type": "item", "descriptions": {
            "en": {"language": "en", "value": value}}} for qid in request.subjects}}
        records = copy.deepcopy(self.records)
        records[index] = observation.response_record(index, request, observation.artifact(data), 200, "application/json")
        bundle = observation.verify_bundle(self.publish(records=records))
        actual = json.loads(bundle["normalized"]["wikidata-descriptions"])
        self.assertEqual(actual["descriptions"]["Q1"], value)

    def test_plan_qid_scope_mismatch_and_missing_inputs_fail(self) -> None:
        changed = copy.deepcopy(self.plan)
        changed["edge_qids"] = ["Q1"]
        with self.assertRaisesRegex(observation.ObservationError, "bound selection"):
            observation.validate_plan(changed)
        changed = copy.deepcopy(self.plan)
        changed["selection_inputs"].pop()
        with self.assertRaises(observation.ObservationError):
            observation.validate_plan(changed)

    def test_explicit_absence_is_only_allowed_for_prior_nodes(self) -> None:
        changed = copy.deepcopy(self.plan)
        prior = next(x for x in changed["selection_inputs"] if x["name"] == "prior-brain-nodes")
        prior.update(state="absent", sha256=None, bytes=0, qids=[])
        changed["edge_qids"] = ["Q1"]
        observation.validate_plan(changed)
        changed["selection_inputs"][0].update(state="absent", sha256=None, bytes=0, qids=[])
        with self.assertRaises(observation.ObservationError):
            observation.validate_plan(changed)

    def test_truncated_surplus_reordered_and_failed_requests_never_publish(self) -> None:
        for records in (self.records[:-1], self.records + [self.records[-1]], list(reversed(self.records))):
            with self.subTest(size=len(records)):
                with self.assertRaises(observation.ObservationError):
                    self.publish(records=records)
        for status, content_type in ((503, "application/json"), (200, "text/html")):
            with self.assertRaises(observation.ObservationError):
                observation.response_record(0, observation.requests_for(self.plan)[0], b"{}", status, content_type)
        self.assertFalse(list(self.store.glob("[0-9a-f]" * 64)))

    def test_collapsed_class_or_description_scope_fails_closed(self) -> None:
        records = replace_response(self.records, self.plan, 0, {"results": {"bindings": []}})
        with self.assertRaisesRegex(observation.ObservationError, "volume floor"):
            self.publish(records=records)
        records = replace_response(self.records, self.plan, -1, {"entities": {}})
        with self.assertRaises(observation.ObservationError):
            self.publish(records=records)

    def test_reviewed_count_floors_cannot_be_bypassed_by_environment(self) -> None:
        self.plan["volume_floors"]["descriptions"] = 4
        with mock.patch.dict(os.environ, {"BRAIN_INGEST_FORCE": "1"}):
            with self.assertRaisesRegex(observation.ObservationError, "descriptions: below"):
                self.publish()

    def test_subject_outside_edge_batch_and_bad_description_redirect_rejected(self) -> None:
        records = replace_response(self.records, self.plan, 12, {"results": {"bindings": [{
            "s": {"value": "http://www.wikidata.org/entity/Q999"},
            "p": {"value": "http://www.wikidata.org/entity/P31"},
            "o": {"value": "http://www.wikidata.org/entity/Q2"},
        }]}})
        with self.assertRaisesRegex(observation.ObservationError, "outside"):
            self.publish(records=records)
        with self.assertRaisesRegex(observation.ObservationError, "redirect"):
            observation._descriptions({"entities": {"Q1": {"id": "Q2", "type": "item", "descriptions": {}}}}, ["Q1"])

    def test_description_missing_and_redirect_semantics_match_legacy(self) -> None:
        data = {"entities": {
            "Q1": {"missing": ""},
            "Q2": {"id": "Q4", "type": "item", "redirects": {"from": "Q2", "to": "Q4"},
                   "descriptions": {"en": {"value": "target"}}},
            "Q3": {"id": "Q3", "type": "item", "descriptions": {}},
        }}
        self.assertEqual(observation._descriptions(data, ["Q1", "Q2", "Q3"]), {"Q2": "target"})

    def test_all_request_and_normalization_semantics_match_legacy_adapters(self) -> None:
        import fetch_wikidata_edges as legacy_edges
        import fetch_wikidata_universe as legacy_universe
        import wikidata_descriptions as legacy_descriptions
        import base64
        plan = {**self.plan, "schema": observation.PLAN_SCHEMA}
        records = fixtures(plan)
        requests = observation.requests_for(plan)
        responses = [observation.parse(base64.b64decode(record["body_base64"]), "fixture")
                     for record in records]
        with (
            contextlib.redirect_stdout(io.StringIO()),
            mock.patch.object(legacy_universe, "query", side_effect=responses[:12]) as universe_query,
            mock.patch.object(legacy_universe.time, "sleep"),
        ):
            universe = legacy_universe.fetch_universe()
        for request, call in zip(requests[:12], universe_query.call_args_list):
            self.assertEqual(request.parameters, urllib.parse.urlencode({"query": call.args[0]}).encode())
        with (
            contextlib.redirect_stdout(io.StringIO()),
            mock.patch.object(legacy_edges, "sparql_query", return_value=responses[12]) as edge_query,
            mock.patch.object(legacy_edges.time, "sleep"),
        ):
            edges = legacy_edges.fetch_edges(self.plan["edge_qids"])
        self.assertEqual(requests[12].parameters,
                         urllib.parse.urlencode({"query": edge_query.call_args.args[0]}).encode())
        with mock.patch.object(legacy_descriptions.common, "curl_fetch", return_value=json.dumps(responses[13])) as get, \
                mock.patch.object(legacy_descriptions.time, "sleep"):
            descriptions = legacy_descriptions.fetch_batch(self.plan["description_qids"])
        self.assertEqual(get.call_args.args[0], requests[13].uri + "?" + requests[13].parameters.decode())
        normalized = observation.normalize(plan, records)
        self.assertEqual(normalized[observation.OUTPUTS["wikidata-universe"]],
                         b"".join(observation.artifact(row) for row in universe))
        self.assertEqual(normalized[observation.OUTPUTS["wikidata-edges"]],
                         b"".join(observation.artifact(row) for row in edges))
        self.assertEqual(json.loads(normalized[observation.OUTPUTS["wikidata-descriptions"]])["descriptions"], descriptions)

    def test_duplicate_response_json_keys_rejected(self) -> None:
        request = observation.requests_for(self.plan)[0]
        records = copy.deepcopy(self.records)
        records[0] = observation.response_record(0, request, b'{"results":{},"results":{"bindings":[]}}', 200, "application/json")
        with self.assertRaises(observation.ObservationError):
            self.publish(records=records)

    def test_atomic_failure_preserves_prior_bundle_and_cleans_owned_scratch(self) -> None:
        first = self.publish()
        before = (first / "bundle.json").read_bytes()
        def fail(_stage, _target):
            raise RuntimeError("injected crash")
        with self.assertRaisesRegex(RuntimeError, "injected crash"):
            self.publish(audit="2026-09-09T12:00:00Z", before_publish=fail)
        self.assertEqual((first / "bundle.json").read_bytes(), before)
        self.assertFalse(list(self.store.glob(".observation-*.tmp")))

    def test_stage_tamper_rejected_before_publication(self) -> None:
        def alter(stage, _target):
            (stage / observation.OUTPUTS["wikidata-edges"]).write_bytes(b"[]\n")
        with self.assertRaises(observation.ObservationError):
            self.publish(before_publish=alter)
        self.assertFalse(list(self.store.glob("[0-9a-f]" * 64)))

    def test_verifier_rejects_missing_surplus_linked_or_substituted_bytes(self) -> None:
        path = self.publish()
        normalized = path / observation.OUTPUTS["wikidata-edges"]
        original = normalized.read_bytes()
        normalized.write_bytes(b"[]\n")
        with self.assertRaises(observation.ObservationError):
            observation.verify_bundle(path)
        normalized.write_bytes(original)
        (path / "surplus").mkdir(mode=0o700)
        with self.assertRaisesRegex(observation.ObservationError, "undeclared"):
            observation.verify_bundle(path)
        (path / "surplus").rmdir()
        normalized.unlink()
        target = self.root / "linked"
        target.write_bytes(original)
        normalized.symlink_to(target)
        with self.assertRaises(observation.ObservationError):
            observation.verify_bundle(path)

    def test_wrong_generation_and_altered_toolchain_rejected(self) -> None:
        path = self.publish()
        with self.assertRaisesRegex(observation.ObservationError, "expected generation"):
            observation.verify_bundle(path, expected_id="sha256:" + "f" * 64)
        toolchain = copy.deepcopy(self.toolchain)
        toolchain["transport_policy"]["retry"] = "silent"
        with self.assertRaisesRegex(observation.ObservationError, "policy"):
            acquire.publish_records(self.plan, self.records, store=self.store, toolchain=toolchain, audit_time=AUDIT)

    def test_transport_isolated_exact_post_and_get_no_config_no_proxy(self) -> None:
        for index in (0, len(self.records) - 1):
            request = observation.requests_for(self.plan)[index]
            completed = subprocess.CompletedProcess([], 0, base64.b64decode(self.records[index]["body_base64"]),
                b"wikilean-observation-http-v2\t200\tapplication/json\t\n")
            with mock.patch.object(acquire.subprocess, "run", return_value=completed) as run:
                acquire._transport(request, Path("/usr/bin/curl"), 0)
            args = run.call_args.args[0]
            self.assertEqual(args[1], "--disable")
            self.assertNotIn("--location", args)
            self.assertNotIn("--retry", args)
            self.assertEqual(run.call_args.kwargs["env"], acquire.ENVIRONMENT)
            self.assertEqual(run.call_args.kwargs["env"]["NO_PROXY"], "*")
            self.assertEqual(run.call_args.kwargs["input"], request.parameters if request.kind == "http_post" else None)

    def retry_fixture(self):
        attempts = [{"request_index": index, "attempt": 1, "outcome": "succeeded",
                     "response": copy.deepcopy(record), "retry_delay_seconds": 0}
                    for index, record in enumerate(self.records)]
        failure = acquire.RequestFailure(0, "fixture gateway error", b"nginx Bad Gateway", status=502,
                                         content_type="text/html", curl_exit_code=22)
        attempts[0]["attempt"] = 2
        attempts.insert(0, {"request_index": 0, "attempt": 1, "outcome": "failed",
                            "response": acquire.failed_response(failure), "retry_delay_seconds": 10})
        toolchain = {**self.toolchain, "schema": observation.TOOLCHAIN_SCHEMA_V2,
                     "transport_policy": copy.deepcopy(observation.TRANSPORT_POLICY_V2)}
        return attempts, toolchain

    def test_explicit_retry_bundle_retains_failed_bytes_and_counts_actual_attempts(self) -> None:
        attempts, toolchain = self.retry_fixture()
        path = acquire.publish_records(self.plan, attempts, store=self.store, toolchain=toolchain, audit_time=AUDIT)
        verified = observation.verify_bundle(path)
        self.assertEqual(verified["manifest"]["schema"], observation.BUNDLE_SCHEMA_V2)
        receipt = verified["receipt"]
        self.assertEqual(receipt["schema"], observation.contracts.ACQUISITION_RECEIPT_SCHEMA_V2)
        self.assertEqual(receipt["batch"]["requests_total"], len(self.records) + 1)
        self.assertEqual(receipt["batch"]["requests_failed"], 1)
        self.assertEqual(receipt["batch"]["requests_succeeded"], len(self.records))
        self.assertEqual(receipt["attempts"][0]["response_sha256"], observation.sha(b"nginx Bad Gateway"))
        self.assertEqual(verified["normalized"], {k: observation.normalize(self.plan, self.records)[v] for k, v in observation.OUTPUTS.items()})
        self.assertIn(base64.b64encode(b"nginx Bad Gateway"), (path / "acquired.jsonl").read_bytes())
        self.assertEqual(observation.sha((path / "normalization-profile.json").read_bytes()), verified["lineage"]["tool"]["sha256"])

    def test_attempt_transcript_rejects_omissions_nonretryable_and_rehashed_substitutions(self) -> None:
        attempts, _toolchain = self.retry_fixture()
        for mutate in (
            lambda x: x.pop(),
            lambda x: x.pop(0),
            lambda x: x[0]["response"].update(http_status=401),
            lambda x: x[0]["response"].update(curl_exit_code=True),
            lambda x: x[0]["response"].update(body_sha256="f" * 64),
            lambda x: x[0].update(retry_delay_seconds=9),
            lambda x: x[0].update(retry_delay_seconds=301),
            lambda x: x[0]["response"].update(retry_after={"kind": "delay-seconds", "seconds": 20}),
            lambda x: x[1]["response"].update(index=False),
            lambda x: x[1]["response"].update(http_status=502),
            lambda x: x.append(copy.deepcopy(x[-1])),
        ):
            changed = copy.deepcopy(attempts)
            mutate(changed)
            with self.assertRaises(observation.ObservationError):
                observation.successful_attempt_records(self.plan, changed)

    def test_retry_policy_respects_retry_after_and_never_retries_invalid_success_payload(self) -> None:
        for status, code, expected in ((502, 22, 10), (429, 22, 10), (0, 28, 10),
                                       (200, 0, None), (401, 22, None), (403, 22, None), (0, 60, None)):
            failure = acquire.RequestFailure(0, "fixture", status=status, curl_exit_code=code)
            self.assertEqual(acquire.retry_delay(failure, 1), expected)
        failure = acquire.RequestFailure(0, "fixture", status=429, curl_exit_code=22,
                                         retry_after={"kind": "delay-seconds", "seconds": 45})
        self.assertEqual(acquire.retry_delay(failure, 1), 45)
        self.assertIsNone(acquire.retry_delay(failure, 5))
        failure.retry_after["seconds"] = 301
        self.assertIsNone(acquire.retry_delay(failure, 1))
        failure.retry_after = {"kind": "http-date", "value": "2030-01-01T00:00:00Z"}
        self.assertIsNone(acquire.retry_delay(failure, 1))
        for header in (b"9999999999999999", b"9" * 129, b"invalid-present-value"):
            failure.retry_after = acquire.parsed_retry_after(header)
            self.assertEqual(failure.retry_after, {"kind": "unsupported-or-excessive"})
            self.assertIsNone(acquire.retry_delay(failure, 1))

    def test_producer_owns_retry_attempts_before_complete_publication(self) -> None:
        _attempts, toolchain = self.retry_fixture()
        plan_path = self.root / "plan.json"
        plan_path.write_bytes(observation.canonical(self.plan))
        counts = {}
        def transport(request, curl, index):
            counts[index] = counts.get(index, 0) + 1
            if index == 0 and counts[index] <= 2:
                raise acquire.RequestFailure(index, "gateway", b"nginx Bad Gateway", status=502,
                                             content_type="text/html", curl_exit_code=22)
            return copy.deepcopy(self.records[index])
        with mock.patch.object(acquire, "require_isolated_startup"), \
                mock.patch.object(acquire, "runtime_identity", return_value=toolchain), \
                mock.patch.object(acquire, "_transport", side_effect=transport), \
                mock.patch.object(acquire.time, "sleep") as sleep:
            path = acquire.acquire(plan_path, store=self.store, curl=Path("/usr/bin/curl"), retry_transient=True)
        verified = observation.verify_bundle(path)
        self.assertEqual(verified["receipt"]["batch"]["requests_failed"], 2)
        self.assertEqual(counts[0], 3)
        self.assertIn(mock.call(10), sleep.call_args_list)
        self.assertIn(mock.call(20), sleep.call_args_list)
        self.assertEqual(len(list(self.store.glob("failed-attempt-*"))), 2)

    def test_historical_tool_profile_cannot_claim_explicit_retry_support(self) -> None:
        attempts, toolchain = self.retry_fixture()
        previous = next(p for p in observation.reviewed_profiles()["profiles"] if "retry_normalization_schema" not in p)
        toolchain.update(profile_id=previous["profile_id"], files=previous["files"])
        with self.assertRaisesRegex(observation.ObservationError, "did not support explicit retry"):
            observation.bundle_files(self.plan, attempts, toolchain, AUDIT)

    def test_v2_defaults_to_small_bounded_subquery_before_label_service(self) -> None:
        self.assertEqual(self.plan["schema"], observation.PLAN_SCHEMA_V2)
        plan = copy.deepcopy(self.plan)
        scope = [f"Q{value}" for value in range(1, 61)]
        next(row for row in plan["selection_inputs"] if row["name"] == "concept-layer")["qids"] = scope
        plan["edge_qids"] = scope
        requests = observation.requests_for(plan)
        edges = [request for request in requests if request.stage == "edges"]
        self.assertEqual([len(request.subjects) for request in edges], [25, 25, 10])
        self.assertEqual([qid for request in edges for qid in request.subjects], scope)
        for request in edges:
            query = urllib.parse.parse_qs(request.parameters.decode())["query"][0]
            self.assertIn("SELECT DISTINCT ?s ?p ?o WHERE", query)
            self.assertIn("FILTER(?o IN (" + ", ".join(f"wd:{q}" for q in scope) + "))", query)
            self.assertLess(query.index("FILTER(?o IN"), query.index("SERVICE wikibase:label"))
        previous = observation.requests_for({**plan, "schema": observation.PLAN_SCHEMA})
        self.assertEqual([len(request.subjects) for request in previous if request.stage == "edges"], [60])
        self.assertEqual(requests[:12], previous[:12])
        self.assertEqual([request for request in requests if request.stage == "descriptions"],
                         [request for request in previous if request.stage == "descriptions"])
        old_plan = {**self.plan, "schema": observation.PLAN_SCHEMA}
        self.assertEqual(observation.normalize(old_plan, fixtures(old_plan)),
                         observation.normalize(self.plan, self.records))

    def test_historical_profile_accepts_v1_but_cannot_claim_v2_requests(self) -> None:
        registry = observation.reviewed_profiles()
        previous = next(profile for profile in registry["profiles"] if "plan_schemas" not in profile)
        old_toolchain = {**self.toolchain, "profile_id": previous["profile_id"], "files": previous["files"]}
        old_plan = {**self.plan, "schema": observation.PLAN_SCHEMA}
        path = acquire.publish_records(old_plan, fixtures(old_plan), store=self.store,
                                       toolchain=old_toolchain, audit_time=AUDIT)
        self.assertEqual(observation.verify_bundle(path)["plan"]["schema"], observation.PLAN_SCHEMA)
        with self.assertRaisesRegex(observation.ObservationError, "did not support"):
            observation.bundle_files(self.plan, self.records, old_toolchain, AUDIT)

    def test_http200_invalid_or_truncated_payload_is_rejected_before_next_request(self) -> None:
        plan_path = self.root / "plan.json"
        plan_bytes = observation.canonical(self.plan)
        plan_path.write_bytes(plan_bytes)
        broken = b'{"results":{"bindings":[\njava.util.concurrent.TimeoutException\n'
        completed = subprocess.CompletedProcess([], 0, broken,
            b"wikilean-observation-http-v2\t200\tapplication/sparql-results+json\t\n")
        with mock.patch.object(acquire, "require_isolated_startup"), \
                mock.patch.object(acquire, "runtime_identity", return_value=self.toolchain), \
                mock.patch.object(acquire.subprocess, "run", return_value=completed) as transport, \
                mock.patch.object(acquire, "_publish_locked") as publish:
            with self.assertRaisesRegex(acquire.RequestFailure, "invalid HTTP response"):
                acquire.acquire(plan_path, store=self.store, curl=Path("/usr/bin/curl"))
        transport.assert_called_once()
        publish.assert_not_called()
        attempts = list(self.store.glob("failed-attempt-*"))
        self.assertEqual(len(attempts), 1)
        attempt = attempts[0]
        self.assertEqual((attempt / "response.bin").read_bytes(), broken)
        self.assertEqual((attempt / "request.parameters").read_bytes(), observation.requests_for(self.plan)[0].parameters)
        self.assertEqual((attempt / "request-plan.json").read_bytes(), plan_bytes)
        report = json.loads((attempt / "failure.json").read_bytes())
        self.assertFalse(report["authority"])
        self.assertEqual(report["response"]["received_sha256"], observation.sha(broken))
        self.assertEqual(set(path.name for path in attempt.iterdir()),
                         {"response.bin", "request.parameters", "request-plan.json", "failure.json"})
        self.assertEqual(attempt.stat().st_mode & 0o777, 0o700)
        self.assertTrue(all(path.stat().st_mode & 0o777 == 0o644 for path in attempt.iterdir()))
        self.assertFalse(list(self.store.glob("[0-9a-f]" * 64)))
        with self.assertRaises((observation.ObservationError, OSError)):
            observation.verify_bundle(attempt)

    def test_early_payload_shape_checks_reject_json_errors_and_malformed_bindings(self) -> None:
        request = observation.requests_for(self.plan)[0]
        bodies = [b'{"error":"timeout"}', b'{"results":{"bindings":{}}}',
                  b'{"results":{"bindings":[null]}}', b'{"results":{"bindings":[{}]}}',
                  b'{"results":{"bindings":[]},"results":{"bindings":[]}}',
                  b'{"results":{"bindings":[]},"bad":NaN}']
        for raw in bodies:
            with self.subTest(raw=raw):
                with self.assertRaises(observation.ObservationError):
                    observation.validate_response_payload(request, raw)
        with self.assertRaises(observation.ObservationError):
            observation.validate_response_payload(observation.requests_for(self.plan)[-1], b'{"entities":{}}')

    def test_failed_attempt_diagnostics_are_bounded_and_exclude_unreviewed_urls(self) -> None:
        store = acquire.prepare_store(self.store)
        request = observation.requests_for(self.plan)[0]
        raw = b"x" * 257
        with mock.patch.object(observation, "MAX_RESPONSE_BYTES", 64):
            target = acquire.record_failed_attempt(store, observation.canonical(self.plan), request,
                                                  acquire.RequestFailure(0, "transport failed", raw))
        report = json.loads((target / "failure.json").read_bytes())
        self.assertEqual((target / "response.bin").read_bytes(), raw[:64])
        self.assertTrue(report["response"]["truncated_to_bound"])
        self.assertEqual(report["response"]["received_sha256"], observation.sha(raw))
        unsafe = observation.Request(request.stage, request.kind, "https://example.invalid/private?token=fixture",
                                     request.parameters, request.subjects)
        with self.assertRaisesRegex(observation.ObservationError, "reviewed public endpoint"):
            acquire.record_failed_attempt(store, b"{}", unsafe, acquire.RequestFailure(0, "transport failed"))
        self.assertEqual(len(list(store.glob("failed-attempt-*"))), 1)

    def test_transport_failure_retains_exit_status_and_only_parsed_retry_after(self) -> None:
        request = observation.requests_for(self.plan)[0]
        cases = [
            (28, b"000\t\t", 0, None),
            (22, b"429\t\t120", 429, {"kind": "delay-seconds", "seconds": 120}),
            (22, b"503\ttext/html\tTue, 08 Sep 2026 20:30:00 GMT", 503,
             {"kind": "http-date", "value": "2026-09-08T20:30:00Z"}),
            (22, b"503\ttext/html\thttps://secret.invalid/?token=private", 503, {"kind": "unsupported-or-excessive"}),
        ]
        store = acquire.prepare_store(self.store)
        for code, trailer, status, retry in cases:
            with self.subTest(code=code, status=status):
                completed = subprocess.CompletedProcess([], code, b"failure-body",
                    b"curl: private stderr details\nwikilean-observation-http-v2\t" + trailer + b"\n")
                with mock.patch.object(acquire.subprocess, "run", return_value=completed):
                    with self.assertRaises(acquire.RequestFailure) as caught:
                        acquire._transport(request, Path("/usr/bin/curl"), 0)
                failure = caught.exception
                self.assertEqual((failure.curl_exit_code, failure.status, failure.retry_after), (code, status, retry))
                path = acquire.record_failed_attempt(store, observation.canonical(self.plan), request, failure)
                raw = (path / "failure.json").read_bytes()
                report = json.loads(raw)
                self.assertEqual(report["curl_exit_code"], code)
                self.assertEqual(report["http_status"], status)
                self.assertEqual(report["retry_after"], retry)
                self.assertNotIn(b"private", raw)
                self.assertNotIn(b"secret.invalid", raw)

    def test_retry_after_parser_rejects_unbounded_or_non_wait_header_content(self) -> None:
        for raw in (b"9" * 16, b"x" * 129, b"-1", b"0.5", b"120\tsecret", b"tomorrow",
                    b"Tue, 99 Sep 2026 20:30:00 GMT"):
            self.assertEqual(acquire.parsed_retry_after(raw), {"kind": "unsupported-or-excessive"})
        self.assertIsNone(acquire.parsed_retry_after(b""))
        self.assertEqual(acquire.parsed_retry_after(b"0"), {"kind": "delay-seconds", "seconds": 0})

    def test_unisolated_live_acquisition_fails_before_transport_or_store(self) -> None:
        plan = self.root / "plan.json"
        plan.write_bytes(observation.canonical(self.plan))
        with mock.patch.object(acquire, "_transport") as transport:
            with self.assertRaisesRegex(observation.ObservationError, "isolated"):
                acquire.acquire(plan, store=self.store, curl=Path("/usr/bin/curl"))
        transport.assert_not_called()
        self.assertFalse(self.store.exists())

    def test_concurrent_publishers_converge_on_one_complete_generation(self) -> None:
        context = multiprocessing.get_context("spawn")
        queue = context.Queue()
        processes = [context.Process(target=publish_in_child,
            args=(self.plan, self.records, str(self.store), self.toolchain, queue)) for _ in range(2)]
        for process in processes:
            process.start()
        for process in processes:
            process.join(30)
            self.assertEqual(process.exitcode, 0)
        results = [queue.get(timeout=5) for _ in processes]
        self.assertEqual(results[0], results[1])
        observation.verify_bundle(Path(results[0]))

    @unittest.skipUnless(hasattr(os, "fork"), "requires POSIX process signals")
    def test_sigkill_before_rename_leaves_no_published_partial_and_lock_recovers(self) -> None:
        first = self.publish()
        pid = os.fork()
        if pid == 0:
            try:
                self.publish(audit="2026-09-09T12:00:00Z",
                    before_publish=lambda _stage, _target: os.kill(os.getpid(), signal.SIGKILL))
            finally:
                os._exit(3)
        _, status = os.waitpid(pid, 0)
        self.assertTrue(os.WIFSIGNALED(status))
        self.assertEqual(os.WTERMSIG(status), signal.SIGKILL)
        observation.verify_bundle(first)
        self.assertEqual(len(list(self.store.glob("[0-9a-f]" * 64))), 1)
        observation.verify_bundle(self.publish(audit="2026-09-09T12:00:00Z"))


if __name__ == "__main__":
    unittest.main()
