#!/usr/bin/env python3
"""Pure semantics, complete source evidence, and actual v3 compiler regression."""
from __future__ import annotations
import copy
import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import external_git_sources as core
import export_external_git as producer
import test_derived_identifier_sources as fixture

io = core.io
WHEN = fixture.WHEN


def git_index(db, files, modes=None):
    modes = modes or {}
    directory = {}
    entries = []
    for path, raw in sorted(files.items()):
        mode, oid = modes.get(path, "100644"), io.git_oid("blob", raw)
        entries.append({"path": path, "mode": mode, "git_blob": oid, "sha256": io.sha(raw), "bytes": len(raw)})
        parts, target = path.split("/"), directory
        for part in parts[:-1]:
            target = target.setdefault(part, {})
        target[parts[-1]] = (mode, oid)
    def tree(items):
        rows = []
        for name, value in items.items():
            mode, oid = ("40000", tree(value)) if isinstance(value, dict) else value
            rows.append(((name + ("/" if mode == "40000" else "")).encode(), mode.encode() + b" " + name.encode() + b"\0" + bytes.fromhex(oid)))
        return io.git_oid("tree", b"".join(raw for _, raw in sorted(rows)))
    return {"repository": core.GIT_SOURCES[db], "commit": "1" * 40, "tree": tree(directory), "entries": entries}


class ExternalGitTest(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.IdentifierSourcesTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.fixture.sources[:] = [source for source in self.fixture.sources if source["source"] in core.PARENTS]
        self.fixture.manifests = {name: value for name, value in self.fixture.manifests.items() if name in core.PARENTS}
        self.nlab = {
            "README.md": b"retained license policy: no formal license\n",
            "pages/1/2/3/4/1/name": b"Group\n",
            "pages/1/2/3/4/1/content.md": b"[[!redirects Abstract group]]\n\n## Idea\nA group is a mathematical structure with an operation. [[Ring]] [[Ring]] [[missing]]\n",
            "pages/1/2/3/4/2/name": b"Ring\r\n",
            "pages/1/2/3/4/2/content.md": b"A ring has two binary operations, addition and multiplication. [[abstract GROUP]]",
            "pages/1/2/3/4/3/name": b"Group > history\n", "pages/1/2/3/4/3/content.md": b"historical junk",
            "pages/1/2/3/4/4/name": b"Missing content\n"}
        self.stacks = {
            "tags/tags": b"# comment\n0001,algebra-lemma-one\n0002,algebra-lemma-two\n0003,algebra-modules-definition-main\n",
            "algebra.tex": br"\begin{lemma}\label{lemma-one}A statement referring to \ref{lemma-two}.\end{lemma}\begin{proof}Use \ref{algebra-modules-definition-main}.\end{proof}\begin{lemma}\label{lemma-two}Another statement.\begin{slogan}Omit slogan\end{slogan}\cite{omitted}\end{lemma}",
            "algebra-modules.tex": br"\begin{definition}\label{definition-main}The definition uses \ref{algebra-lemma-one} and \ref{unknown}.\end{definition}",
            "COPYING": b"GNU Free Documentation License fixture evidence\n",
            "subdirectory/ignored.tex": b"not a root chapter"}
        for db, files in (("nlab", self.nlab), ("stacks", self.stacks)):
            index = git_index(db, files)
            self.fixture.source(db + "-source", {"git_tree": io.canonical(index),
                **{"file-" + io.sha(path.encode()): raw for path, raw in files.items()}}, commit=index["commit"])
        self.sources, self.manifests, self.roots = self.fixture.sources, self.fixture.manifests, self.fixture.roots
        self.plan = {"schema": core.PLAN_SCHEMA, "parents": sorted(self.sources, key=lambda s: s["source"]),
                     "reviewed_parent_manifest_ids": {name: m["source_manifest_id"] for name, m in self.manifests.items()}}
        self.store = self.root / "external-git-exports"
        self.store.mkdir(mode=0o700)

    def export(self):
        return producer.export(self.plan, self.roots, self.store, normalized_at=WHEN)

    @staticmethod
    def rows(path):
        return [json.loads(line) for line in path.read_bytes().splitlines()]

    def captured(self):
        return core.capture_parents(self.plan, self.roots)

    def test_complete_export_verifies_pair_envelopes_and_parent_ancestry(self):
        path = self.export()
        producer.verify(path, self.roots)
        fragment = json.loads((path / "source-fragment.json").read_bytes())
        self.assertEqual(len(fragment["sources"]), 7)
        for db, expected in (("nlab", (2, 2)), ("stacks", (3, 3))):
            pages, links = [self.rows(path / f"normalized/{db}_{kind}.jsonl") for kind in ("pages", "links")]
            self.assertEqual((len(pages) - 1, len(links) - 1), expected)
            core.build_context.validate_external_pair(db, pages[0]["_meta"], pages[1:], links[0]["_meta"], links[1:])
            self.assertEqual(pages[0]["_meta"]["source_pin"], "1" * 40)
            self.assertNotIn("fetched_at", pages[0]["_meta"])
            self.assertTrue(all("snippet_license" in page for page in pages[1:]))
            lineage = json.loads((path / f"evidence/external-{db}.json").read_bytes())
            self.assertIn(self.manifests[db + "-source"]["source_manifest_id"], lineage["parent_source_manifest_ids"])
        self.assertFalse(any("qid" in row for row in self.rows(path / "normalized/stacks_pages.jsonl")))

    def test_nlab_semantic_projection_matches_legacy_build_and_emit(self):
        import nlab
        legacy = self.root / "legacy-nlab"
        repo = self.root / "nlab-checkout"
        for name, raw in self.nlab.items():
            target = repo / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        path = self.export()
        qmap = core.nlab_qids(self.captured()[3])
        with mock.patch.object(core.common, "qid_map", return_value=qmap), mock.patch.object(core.common, "EXTERNAL_DIR", legacy):
            pages, links, stats = nlab.build(repo)
            core.common.emit("nlab", pages, links, {"source_pin": "1" * 40, **stats})
        for kind in ("pages", "links"):
            self.assertEqual(self.rows(legacy / f"nlab_{kind}.jsonl"), self.rows(path / f"normalized/nlab_{kind}.jsonl"))

    def test_stacks_semantic_projection_matches_legacy_main(self):
        legacy = self.root / "legacy-stacks"
        repo = self.root / "stacks-checkout"
        for name, raw in self.stacks.items():
            target = repo / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        spec = importlib.util.spec_from_file_location("legacy_stacks_fixture", core.ROOT / "brain/ingest/stacks.py")
        module = importlib.util.module_from_spec(spec)
        with mock.patch.object(core.common, "cache_path", return_value=repo):
            spec.loader.exec_module(module)
        path = self.export()
        with mock.patch.object(module, "refresh_clone", return_value="1" * 40), mock.patch.object(core.common, "EXTERNAL_DIR", legacy):
            module.main()
        for kind in ("pages", "links"):
            self.assertEqual(self.rows(legacy / f"stacks_{kind}.jsonl"), self.rows(path / f"normalized/stacks_{kind}.jsonl"))

    def test_redirect_qid_join_casefold_uses_explicit_scope_and_lowest_qid(self):
        captured = self.captured()[3]
        captured[("wikidata-crossrefs", "wikidata_crossrefs")] = io.artifact({"properties": {"P4215": ["nlab"]},
            "xrefs": {"Q2": {"nlab": ["abstract GROUP"]}, "Q1": {"nlab": ["abstract GROUP"]}}})
        parser = core.selected_parser("nlab", io.read(core.ROOT / "brain/ingest/nlab.py"), core.nlab_qids(captured))
        _tree, memory = core.complete_tree("nlab", self.manifests, captured)
        pages, links, stats = parser.build(memory)
        self.assertEqual(next(page["qid"] for page in pages if page["id"] == "Group"), "Q1")
        self.assertEqual(stats["n_junk_skipped"], 2)
        self.assertEqual(stats["n_links_unresolved"], 1)
        self.assertEqual(len(links), 2)

    def test_requested_scope_or_curated_mapping_cannot_be_omitted(self):
        captured = self.captured()[3]
        captured[("wikidata-crossrefs", "requested_qid_scope")] = io.canonical({"schema": io.SCOPE_SCHEMA, "qids": []})
        with self.assertRaisesRegex(io.ExportError, "scope"):
            core.nlab_qids(captured)
        captured = self.captured()[3]
        captured[(io.CURATED_SOURCE, "source_registry")] = fixture.cross_fixture.registry(nlab="P999")
        with self.assertRaisesRegex(io.ExportError, "P4215"):
            core.nlab_qids(captured)

    def test_incomplete_extra_or_reordered_git_members_fail(self):
        captured = self.captured()[3]
        key = ("nlab-source", "file-" + io.sha(b"README.md"))
        missing = dict(captured)
        del missing[key]
        with self.assertRaisesRegex(io.ExportError, "incomplete"):
            core.complete_tree("nlab", self.manifests, missing)
        with self.assertRaisesRegex(io.ExportError, "undeclared"):
            core.complete_tree("nlab", self.manifests, {**captured, ("nlab-source", "file-" + "9" * 64): b"extra"})
        index = io.parse(captured[("nlab-source", "git_tree")], "tree")
        index["entries"].reverse()
        captured[("nlab-source", "git_tree")] = io.canonical(index)
        with self.assertRaisesRegex(io.ExportError, "unordered"):
            core.complete_tree("nlab", self.manifests, captured)

    def test_git_root_and_blob_mismatch_fail(self):
        for field in ("tree", "git_blob"):
            captured = self.captured()[3]
            index = io.parse(captured[("nlab-source", "git_tree")], "tree")
            if field == "tree":
                index[field] = "9" * 40
            else:
                index["entries"][0][field] = "9" * 40
            captured[("nlab-source", "git_tree")] = io.canonical(index)
            with self.subTest(field=field), self.assertRaisesRegex(io.ExportError, "differs"):
                core.complete_tree("nlab", self.manifests, captured)

    def test_selected_symlink_is_rejected_without_following(self):
        memory = core.MemoryPath({"tags/tags": b"/tmp/unrelated"}, {"tags/tags": "120000"})
        parser = core.selected_parser("stacks", io.read(core.ROOT / "brain/ingest/stacks.py"), {})
        with self.assertRaisesRegex(io.ExportError, "not a regular file"):
            core.stacks_rows(memory, parser)

    def test_selected_directory_cannot_be_treated_as_missing_content(self):
        files = {"pages/1/2/3/4/1/name": b"Group", "pages/1/2/3/4/1/content.md/nested": b"directory payload"}
        memory = core.MemoryPath(files, {path: "100644" for path in files})
        parser = core.selected_parser("nlab", io.read(core.ROOT / "brain/ingest/nlab.py"), {})
        with self.assertRaisesRegex(io.ExportError, "not a regular file"):
            parser.build(memory)

    def test_pair_cleanup_preserves_legacy_control_collision_and_snippet_limits(self):
        pages = [{"db": "nlab", "id": name, "title": name, "url": "https://example.invalid/", "snippet": "word " * 200,
                  "aliases": ["alias\u200b", "alias"]} for name in ("Group", "Gro\u200bup", "\u200b")]
        links = [{"db": "nlab", "src": "Gro\u200bup", "dst": "Group"},
                 {"db": "nlab", "src": "Group", "dst": "Other\u200b"}]
        meta, pages, links = core.normalize_pair("nlab", pages, links, {"source_pin": "fixture"}, io.read(core.ROOT / "brain/ingest/common.py"))
        self.assertEqual((len(pages), len(links), meta["n_pages_dropped_bad_id"]), (1, 1, 2))
        self.assertEqual(pages[0]["aliases"], ["alias"])
        self.assertLessEqual(len(pages[0]["snippet"]), 600)
        self.assertEqual(links[0]["dst"], "Other")

    def test_unselected_symlink_blob_retains_complete_tree_proof(self):
        captured = self.captured()[3]
        files = {**self.nlab, "unselected-symlink": b"/tmp/unrelated"}
        captured[("nlab-source", "git_tree")] = io.canonical(git_index("nlab", files, {"unselected-symlink": "120000"}))
        captured[("nlab-source", "file-" + io.sha(b"unselected-symlink"))] = b"/tmp/unrelated"
        _tree, memory = core.complete_tree("nlab", self.manifests, captured)
        self.assertEqual(memory.files["unselected-symlink"], b"/tmp/unrelated")

    def test_missing_stacks_tags_and_empty_nlab_fail(self):
        captured = self.captured()[3]
        files = {"README.md": b"no content"}
        for db in core.GIT_SOURCES:
            wrong = {key: raw for key, raw in captured.items() if key[0] != db + "-source"}
            wrong[(db + "-source", "git_tree")] = io.canonical(git_index(db, files))
            wrong[(db + "-source", "file-" + io.sha(b"README.md"))] = b"no content"
            with self.subTest(db=db), self.assertRaises((io.ExportError, RuntimeError)):
                core.reduce_git(db, self.manifests, {}, wrong, producer.implementation())

    def test_pure_parser_and_pair_prefix_do_not_create_cache_or_use_network(self):
        with mock.patch.object(core.common, "cache_path", side_effect=AssertionError("cache")), \
             mock.patch.object(core.common, "qid_map", side_effect=AssertionError("loose crossrefs")), \
             mock.patch("socket.socket", side_effect=AssertionError("network")), \
             mock.patch.object(core.common, "external_pair_lock", side_effect=AssertionError("legacy publication")):
            path = self.export()
            producer.verify(path, self.roots)

    def test_parser_selector_changes_fail_closed(self):
        raw = io.read(core.ROOT / "brain/ingest/stacks.py").replace(b"def parse_chapter(", b"def changed_parse_chapter(")
        with self.assertRaisesRegex(io.ExportError, "selector differs"):
            core.selected_parser("stacks", raw, {})
        raw = io.read(core.ROOT / "brain/ingest/common.py").replace(b"    validate_external_pair(db, meta, kept_pages, meta, kept_links)", b"    changed_validation(db, meta, kept_pages, meta, kept_links)")
        with self.assertRaisesRegex(io.ExportError, "boundary differs"):
            core.normalize_pair("stacks", [], [], {}, raw)

    def test_explicit_reviewed_id_and_source_content_are_required(self):
        self.plan["reviewed_parent_manifest_ids"]["nlab-source"] = "sha256:" + "f" * 64
        with self.assertRaisesRegex(io.ExportError, "explicitly reviewed"):
            self.export()
        self.assertEqual(list(self.store.iterdir()), [])

    def test_no_replace_repeat_and_output_tamper_rejected(self):
        path = self.export()
        self.assertEqual(self.export(), path)
        (path / "normalized/nlab_pages.jsonl").write_bytes(b"{}\n")
        files = io.capture_tree(path)
        document = io.parse(files.pop("export.json"), "export")
        document["files"] = {name: {"sha256": io.sha(raw), "bytes": len(raw)} for name, raw in sorted(files.items())}
        document["export_id"] = core.contracts.domain_hash(core.EXPORT_SCHEMA, {key: value for key, value in document.items() if key != "export_id"})
        (path / "export.json").write_bytes(io.canonical(document))
        with self.assertRaisesRegex(io.ExportError, "independent reduction"):
            producer.verify(path, self.roots)

    def test_uncertain_publish_removes_owned_final_and_preserves_recreated_stage(self):
        original_publish = io.stage_io.publish_directory_no_replace
        replacements = []
        def publish_then_fail(owned, target):
            original_publish(owned, target)
            owned.path.mkdir(mode=0o700)
            (owned.path / "unrelated").write_bytes(b"preserve recreated stage")
            replacements.append(owned.path)
            raise RuntimeError("fixture failure after rename")
        with mock.patch.object(io.stage_io, "publish_directory_no_replace", side_effect=publish_then_fail), \
                self.assertRaisesRegex(RuntimeError, "fixture failure after rename"):
            self.export()
        self.assertEqual(list(self.store.iterdir()), replacements)
        self.assertEqual((replacements[0] / "unrelated").read_bytes(), b"preserve recreated stage")

    def test_full_profile_and_program_preimages_are_bound(self):
        path = self.export()
        profile = json.loads((path / "normalization/profile.json").read_bytes())
        for db in core.GIT_SOURCES:
            lineage = json.loads((path / f"evidence/external-{db}.json").read_bytes())
            self.assertEqual(lineage["tool"]["sha256"], io.sha(io.canonical(profile)))
        with mock.patch.object(core.common, "__file__", "/unreviewed/common.py"):
            with self.assertRaisesRegex((ValueError, io.ExportError), "helper origin"):
                producer.implementation()

    def test_actual_compiler_accepts_all_pairs_and_full_evidence(self):
        import test_compile_offline_pack_v2 as compiler_fixture
        path = self.export()
        fragment = json.loads((path / "source-fragment.json").read_bytes())
        fixture = compiler_fixture.OfflinePackCompilerTest()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture._upgrade_plan_v3()
        inventory, plan = fixture.inventory, fixture.plan
        inventory["inputs"] = [item for item in inventory["inputs"] if item["id"] != "optional_external"]
        plan["input_bindings"] = [item for item in plan["input_bindings"] if item["input_id"] != "optional_external"]
        for binding in fragment["input_bindings"]:
            family = binding["input_id"].removeprefix("external-")
            inventory["inputs"].append({"id": binding["input_id"], "class": "immutable_source_object", "root": "external",
                "path_pattern": "*_" + family + ".jsonl", "cardinality": "many", "requirement": "required", "consumers": ["brain/replay.py"], "purpose": "external Git fixture"})
            for member in binding["members"]:
                (fixture.external / member["path"]).write_bytes((path / ("normalized/" + member["object"] + ".jsonl")).read_bytes())
        inventory["inputs"].sort(key=lambda item: item["id"])
        inventory["inventory_id"] = core.contracts.reducer_input_inventory_identity(inventory)
        fixture.inventory_path.write_bytes(io.canonical(inventory))
        plan["inventory_id"] = inventory["inventory_id"]
        plan["sources"] = sorted([*plan["sources"], *fragment["sources"]], key=lambda item: item["source"])
        plan["input_bindings"] = sorted([*plan["input_bindings"], *fragment["input_bindings"]], key=lambda item: item["input_id"])
        fixture.plan_path.write_bytes(io.canonical(plan))
        result = compiler_fixture.compiler.compile_offline_pack_v2(fixture.plan_path.resolve(), fixture.inventory_path.resolve(),
            (fixture.base / "external-git-pack").resolve(), roots={**self.roots, core.PHYSICAL_ROOT: path,
                "repo": fixture.repo.resolve(), "external": fixture.external.resolve()}, git_executable="/usr/bin/git")
        manifest, _ = core.contracts.load_canonical_json(result.manifest_path)
        core.contracts.verify_offline_pack_files(manifest, result.root, manifest_path=result.manifest_path)


if __name__ == "__main__":
    unittest.main()
