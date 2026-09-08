"""ProofWiki XML parity and closed source normalization regressions."""
import base64
import copy
import gzip
import json
import random
import sys
import unittest
from pathlib import Path
from unittest import mock
from xml.sax.saxutils import escape

sys.path.insert(0, str(Path(__file__).resolve().parent))
import proofwiki_sources as core
import export_proofwiki_sources as producer
import test_public_file_sources as raw_fixture
import test_wikidata_crossref_sources as cross_fixture

io = core.io
WHEN = cross_fixture.WHEN


def xml_page(title, ns="0", text="", redirect=None):
    return "<page><title>" + escape(title) + "</title><ns>" + ns + "</ns>" + (
        '<redirect title="' + escape(redirect) + '"/>' if redirect else "") + "<revision><text>" + escape(text) + "</text></revision></page>"


class ProofwikiSourcesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.xml = ('<mediawiki xmlns="http://www.mediawiki.org/xml/export-0.11/">' +
            xml_page("Alpha", text="== Theorem ==\nAlpha is an example mathematical proposition. [[Definition:Ring]] [[Beta]]\n== Proof ==\n[[Beta]]") +
            xml_page("Alpha/Proof 2", text="An alternative proof uses [[Axiom:Choice]] and [[Missing]].") +
            xml_page("Beta", text="A second mathematical theorem has enough text for a snippet. [[Alpha]]") +
            xml_page("Definition:Ring", ns="102", text="A ring is an algebraic structure with addition and multiplication.") +
            xml_page("Definition:Ring/Example", ns="102", text="A definition namespace subpage stays an independent page.") +
            xml_page("Axiom:Choice", ns="100", text="An axiom of choice supplies a choice function.") +
            xml_page("Alias Alpha", redirect="Alpha") + xml_page("Cycle1", redirect="Cycle2") + xml_page("Cycle2", redirect="Cycle1") +
            xml_page("User:Ignored", ns="2", text="Not in scope.") + xml_page("Main Page") +
            "<!--" + base64.b64encode(random.Random(0).randbytes(1400000)).decode() + "--></mediawiki>").encode()
        cls.raw = gzip.compress(cls.xml, mtime=0)

    def setUp(self):
        self.xref = cross_fixture.CrossrefExportTest(); self.xref.setUp(); self.addCleanup(self.xref.doCleanups)
        self.root = self.xref.root
        self.xref.registry = cross_fixture.registry(proofwiki="P6781")
        (self.xref.repository / io.REGISTRY_PATH).write_bytes(self.xref.registry)
        self.xref.git("add", io.REGISTRY_PATH)
        self.xref.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "ProofWiki property")
        self.xref.commit = self.xref.git("rev-parse", "HEAD").decode().strip()
        capture = cross_fixture.fixture
        first, second = capture.entity("Q1"), capture.entity("Q2")
        first["claims"]["P6781"] = [cross_fixture.statement("Alias_Alpha", pid="P6781")]
        second["claims"]["P6781"] = [cross_fixture.statement("Definition:Ring", pid="P6781")]
        toolchain = capture.fake_toolchain()
        self.xref.bundle = capture.acquire.publish_transcript(capture.plan_bytes(["Q1", "Q2"]),
            [capture.response({"entities": {"Q1": first, "Q2": second}})], store=self.root / "proofwiki-entity-capture",
            acquisition_tool=capture.fake_tool(toolchain), acquisition_toolchain=toolchain, audit_time=WHEN)
        xref_export = self.xref.export()
        self.sources = json.loads((xref_export / "source-fragment.json").read_bytes())["sources"]
        self.dump = raw_fixture.PublicFileSourcesTest(); self.dump.raw = self.raw
        self.dump.setUp(); self.addCleanup(self.dump.doCleanups)
        self.dump.raw, self.dump.response = self.raw, self.dump.response_for(self.raw)
        self.raw_export = raw_fixture.core.archive.publish(self.dump.build(), self.root / "raw-source", raw_fixture.core.EXPORT_SCHEMA)
        fragment = json.loads((self.raw_export / "source-fragment.json").read_bytes())
        self.sources += fragment["sources"]
        self.roots = {io.PHYSICAL_ROOT: xref_export, io.GIT_ROOT: self.xref.repository, fragment["physical_root"]: self.raw_export}
        self.manifests = {source["source"]: io.source_plan_contracts._source_manifest_from_plan(source, "fixture") for source in self.sources}
        self.plan = {"schema": core.PLAN_SCHEMA, "parents": sorted(self.sources, key=lambda item: item["source"]),
            "reviewed_parent_manifest_ids": {name: manifest["source_manifest_id"] for name, manifest in self.manifests.items()}}
        programs = producer.implementation()
        self.profile = {"files": [{"path": path, "sha256": io.sha(raw)} for path, raw in sorted(programs.items())]}
        self.profile["profile_id"] = core.profile_id(self.profile)
        registry = self.root / "normalizer-profiles.json"
        registry.write_bytes(io.canonical({"schema": core.PROFILE_SCHEMA, "current_profile": self.profile["profile_id"], "profiles": [self.profile]}))
        patch = mock.patch.object(core, "REGISTRY", registry); patch.start(); self.addCleanup(patch.stop)
        self.store = self.root / "proofwiki-exports"; self.store.mkdir(mode=0o700)

    def export(self):
        return producer.export(self.plan, self.roots, self.store, normalized_at=WHEN)

    @staticmethod
    def rows(path):
        return [json.loads(line) for line in path.read_bytes().splitlines()]

    def test_complete_pair_namespaces_redirects_collapse_context_and_snippet_attribution(self):
        path = self.export(); producer.verify(path, self.roots)
        pages, links = [self.rows(path / ("normalized/proofwiki_" + kind + ".jsonl")) for kind in ("pages", "links")]
        index = {page["id"]: page for page in pages[1:]}
        self.assertEqual(set(index), {"Alpha", "Beta", "Definition:Ring", "Definition:Ring/Example", "Axiom:Choice"})
        self.assertEqual(index["Alpha"]["aliases"], ["Alias_Alpha"])
        self.assertEqual(index["Alpha"]["qid"], "Q1")
        self.assertEqual(index["Definition:Ring"]["kind_hint"], "definition")
        self.assertEqual(index["Axiom:Choice"]["kind_hint"], "axiom")
        self.assertEqual(index["Alpha"]["kind_hint"], "theorem")
        self.assertEqual(index["Alpha"]["snippet_license"], core.common.SNIPPET_LICENSE["proofwiki"])
        contexts = {(row["src"], row["dst"]): row["context"] for row in links[1:]}
        self.assertEqual(contexts[("Alpha", "Beta")], "statement")
        self.assertEqual(contexts[("Alpha", "Axiom:Choice")], "proof")
        core.build_context.validate_external_pair("proofwiki", pages[0]["_meta"], pages[1:], links[0]["_meta"], links[1:])
        self.assertEqual(pages[0]["_meta"]["source_pin"], "sha256:" + io.sha(self.raw))
        self.assertEqual(self.export(), path)

    def test_full_projection_matches_actual_legacy_main(self):
        sys.path.insert(0, str(core.ROOT / "brain/ingest"))
        import proofwiki
        dump = self.root / "latest.xml.gz"; dump.write_bytes(self.raw)
        legacy = self.root / "legacy"
        qmap = core.proofwiki_qids(core.capture_parents(self.plan, self.roots)[3])
        with mock.patch.object(core.common, "cache_path", return_value=dump), mock.patch.object(core.common, "qid_map", return_value=qmap), \
                mock.patch.object(core.common, "EXTERNAL_DIR", legacy), mock.patch.object(proofwiki, "ensure_dump"), mock.patch.object(sys, "argv", ["proofwiki"]):
            proofwiki.main()
        path = self.export()
        for kind in ("pages", "links"):
            self.assertEqual(self.rows(path / f"normalized/proofwiki_{kind}.jsonl"), self.rows(legacy / f"proofwiki_{kind}.jsonl"))

    def test_no_ambient_cache_crossrefs_network_or_legacy_publication(self):
        with mock.patch.object(core.common, "cache_path", side_effect=AssertionError("cache")), \
                mock.patch.object(core.common, "qid_map", side_effect=AssertionError("ambient refs")), \
                mock.patch.object(core.common, "external_pair_lock", side_effect=AssertionError("write")), \
                mock.patch("socket.socket", side_effect=AssertionError("network")):
            path = self.export(); producer.verify(path, self.roots)

    def test_scope_and_property_mapping_are_required(self):
        original = core.capture_parents(self.plan, self.roots)[3]
        changed = dict(original); changed[("wikidata-crossrefs", "requested_qid_scope")] = io.canonical({"schema": io.SCOPE_SCHEMA, "qids": []})
        with self.assertRaisesRegex(io.ExportError, "scope"): core.proofwiki_qids(changed)
        changed = dict(original); changed[(io.CURATED_SOURCE, "source_registry")] = cross_fixture.registry(proofwiki="P1")
        with self.assertRaisesRegex(io.ExportError, "P6781"): core.proofwiki_qids(changed)

    def test_wrong_reviewed_parent_or_changed_raw_bytes_prevent_publication(self):
        self.plan["reviewed_parent_manifest_ids"]["proofwiki-dump"] = "sha256:" + "f" * 64
        with self.assertRaisesRegex(io.ExportError, "explicitly reviewed"): self.export()
        self.assertEqual(list(self.store.iterdir()), [])

    def test_selector_empty_or_invalid_xml_are_rejected(self):
        program = io.read(core.ROOT / "brain/ingest/proofwiki.py")
        with self.assertRaisesRegex(io.ExportError, "selector differs"):
            core.project_xml(self.raw, {}, program.replace(b"def iter_pages(", b"def different_iter_pages("))
        with self.assertRaises(core.ET.ParseError): core.project_xml(gzip.compress(b"<broken>"), {}, program)
        pages, links, meta = core.project_xml(gzip.compress(b'<mediawiki xmlns="http://www.mediawiki.org/xml/export-0.11/"/>'), {}, program)
        with self.assertRaisesRegex(RuntimeError, "0 pages"):
            core.pairs.normalize_pair("proofwiki", pages, links, meta, io.read(core.ROOT / "brain/ingest/common.py"))

    def test_semantic_xml_reader_rechecks_decompression_bound(self):
        program = io.read(core.ROOT / "brain/ingest/proofwiki.py")
        with mock.patch.object(core, "MAX_XML_BYTES", 128), self.assertRaisesRegex(io.ExportError, "decompressed-byte bound"):
            core.project_xml(self.raw, {}, program)

    def test_whole_program_preimages_and_input_lineage_remain_closed(self):
        path = self.export()
        lineage = json.loads((path / "evidence/external-proofwiki.json").read_bytes())
        self.assertEqual(set(lineage["parent_source_manifest_ids"]), {self.manifests[name]["source_manifest_id"] for name in
            ("proofwiki-dump", "wikidata-crossrefs", io.CURATED_SOURCE)})
        self.assertEqual({item["object"] for item in lineage["inputs"]}, {"compressed_dump", "wikidata_crossrefs", "requested_qid_scope", "source_registry"})
        self.assertEqual(lineage["tool"]["sha256"], io.sha(io.canonical(self.profile)))
        programs = producer.implementation(); programs["brain/ingest/proofwiki.py"] += b"\n# changed"
        with self.assertRaisesRegex(io.ExportError, "preimages"):
            core.build_documents(self.plan, *core.capture_parents(self.plan, self.roots), self.profile, programs, WHEN)

    def test_rehashed_changed_output_fails_independent_reduction(self):
        path = self.export(); (path / "normalized/proofwiki_pages.jsonl").write_bytes(b"{}\n")
        files = io.capture_tree(path); document = io.parse(files.pop("export.json"), "export")
        document["files"] = {name: {"sha256": io.sha(raw), "bytes": len(raw)} for name, raw in sorted(files.items())}
        document["export_id"] = core.contracts.domain_hash(core.EXPORT_SCHEMA, {key: value for key, value in document.items() if key != "export_id"})
        (path / "export.json").write_bytes(io.canonical(document))
        with self.assertRaisesRegex(io.ExportError, "independent reduction"): producer.verify(path, self.roots)

    def test_uncertain_publication_cleans_owned_final_and_preserves_recreated_stage(self):
        original_publish = io.stage_io.publish_directory_no_replace
        replacement = []
        def renamed_then_failed(owned, destination):
            original_publish(owned, destination)
            owned.path.mkdir(mode=0o700)
            (owned.path / "unrelated").write_bytes(b"preserve this recreated stage")
            replacement.append(owned.path)
            raise RuntimeError("fixture failure after rename")
        with mock.patch.object(io.stage_io, "publish_directory_no_replace", side_effect=renamed_then_failed), \
                self.assertRaisesRegex(RuntimeError, "fixture failure"):
            self.export()
        self.assertEqual(list(self.store.iterdir()), replacement)
        self.assertEqual((replacement[0] / "unrelated").read_bytes(), b"preserve this recreated stage")

    def test_actual_v3_compiler_accepts_all_xml_source_evidence(self):
        import test_compile_offline_pack_v2 as compiler_fixture
        target = self.export(); fragment = json.loads((target / "source-fragment.json").read_bytes())
        fixture = compiler_fixture.OfflinePackCompilerTest(); fixture.setUp(); self.addCleanup(fixture.tearDown)
        fixture._upgrade_plan_v3()
        fixture.inventory["roots"].append({"id": "proofwiki_normalized", "kind": "external_tree"})
        fixture.inventory["roots"].sort(key=lambda item: item["id"])
        fixture.inventory["inputs"].append({"id": "proofwiki-pages", "class": "immutable_source_object", "cardinality": "one",
            "consumers": ["brain/replay.py"], "path": "proofwiki_pages.jsonl", "purpose": "derived ProofWiki source", "requirement": "required", "root": "proofwiki_normalized"})
        fixture.inventory["inputs"].sort(key=lambda item: item["id"])
        fixture.inventory["inventory_id"] = core.contracts.reducer_input_inventory_identity(fixture.inventory)
        fixture.plan["inventory_id"] = fixture.inventory["inventory_id"]
        fixture.plan["sources"] = sorted([*fixture.plan["sources"], *fragment["sources"]], key=lambda item: item["source"])
        fixture.plan["input_bindings"].append({"input_id": "proofwiki-pages", "state": "present", "sources": ["external-proofwiki"],
            "members": [{"path": "proofwiki_pages.jsonl", "source": "external-proofwiki", "object": "proofwiki_pages"}]})
        fixture.plan["input_bindings"].sort(key=lambda item: item["input_id"])
        fixture.inventory_path.write_bytes(io.canonical(fixture.inventory)); fixture.plan_path.write_bytes(io.canonical(fixture.plan))
        packed = compiler_fixture.compiler.compile_offline_pack_v2(fixture.plan_path.resolve(), fixture.inventory_path.resolve(), (fixture.base / "proofwiki-pack").resolve(),
            roots={"repo": fixture.repo.resolve(), "external": fixture.external.resolve(), **self.roots, core.PHYSICAL_ROOT: target,
                "proofwiki_normalized": target / "normalized"}, git_executable="/usr/bin/git")
        manifest, _ = core.contracts.load_canonical_json(packed.manifest_path)
        core.contracts.verify_offline_pack_files(manifest, packed.root, manifest_path=packed.manifest_path)


if __name__ == "__main__": unittest.main()
