"""Git/proposal closure, independent contributions and real pack integration."""
from __future__ import annotations

import copy
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import proposal_fold_sources as core
import export_proposal_fold as producer
from test_proposal_fold_adapter import fixture, jl, PROGRAM
import test_derived_catalog_sources as source_fixture

WHEN = "2026-09-08T20:00:00Z"


class ProposalSourcesTest(unittest.TestCase):
    source = source_fixture.DerivedSourcesTest.source

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repository = self.root / "repo"; self.repository.mkdir(mode=0o700)
        self.parents = self.root / "parents"; self.parents.mkdir(mode=0o700)
        self.store = self.root / "store"; self.store.mkdir(mode=0o700)
        self.sources, self.manifests = [], {}
        self.files, self.mathlib = fixture()
        folded, _ = core.adapter.fold(PROGRAM, self.files, self.mathlib)
        self.manual = [{"qid": qid, "path": path, "match_kind": "scope", "confidence": "high",
            "evidence": "original manual evidence " + qid, "proposer": "original actor", "skeptic": "accept"}
            for qid, path in sorted(core.MANUAL_KEYS)]
        for path in [*core.BASE_PATHS.values(), "catalog/data/rebuild_grounding.json", "catalog/data/source_registry.json",
                *[name for name in self.files if name.startswith("brain/proposals/")]]:
            self.write(path, self.files[path])
        self.write(core.CONTAINER_PATH, folded[core.CONTAINER_PATH])
        self.write("brain/data/discovery_proposals.jsonl", folded["brain/data/discovery_proposals.jsonl"])
        self.write("brain/data/fc_links.jsonl", folded["brain/data/fc_links.jsonl"] + jl([{
            "qid": "Q2", "decl": "Namespace.short.bad", "kind": "mentions", "evidence": "old rejected completed name"}]))
        self.git("init", "-q"); self.git("add", "brain", "catalog")
        self.commit("Before direct manual contributions")
        self.before = self.git("rev-parse", "HEAD").decode().strip()
        self.write(core.CONTAINER_PATH, folded[core.CONTAINER_PATH] + jl(self.manual))
        self.git("add", core.CONTAINER_PATH); self.commit("Five direct manual contributions")
        self.pin = self.git("rev-parse", "HEAD").decode().strip()
        tree = self.git("rev-parse", "HEAD^{tree}").decode().strip()
        tool = {"name": "fixture-git", "version": "1", "sha256": "a" * 64}
        objects = [{"name": name, "root": "fixture_git", "path": path, "sha256": core.io.sha(self.files[path]),
            "bytes": len(self.files[path]), "media_type": "application/json", "roles": ["normalized", "raw"], "redistribution": "restricted"}
            for name, path in (("grounding", "catalog/data/rebuild_grounding.json"), ("source_registry", "catalog/data/source_registry.json"))]
        curated = {"source": "fixture-fold-curation", "source_kind": "curated_git_tree", "pin": {"type": "git_commit", "value": self.pin, "tree": tree},
            "objects": objects, "license": {"expression": "CC0-1.0", "redistribution": "restricted"}, "acquisition": tool,
            "normalization": {"schema": "fixture.curated/v1", "tool": tool, "inputs": [o["name"] for o in objects], "outputs": [o["name"] for o in objects]}}
        self.sources.append(curated); self.manifests[curated["source"]] = core.io.source_plan_contracts._source_manifest_from_plan(curated, "fixture curation")
        hf = self.source("fixture-hf", {"statements": b"fixture"})
        self.source("wikilean-derived-hierarchy", {"hierarchy": self.files["catalog/data/hierarchy.json"]}, parent=hf)
        self.mathlib["Mathlib/README.md"] = b"theorem rescued : True := by trivial\n"
        tree = {"commit": "b" * 40, "entries": [{"path": path, "mode": "100644", "sha256": core.io.sha(raw), "bytes": len(raw)} for path, raw in sorted(self.mathlib.items())]}
        mathlib = self.source("mathlib-source", {"git_tree": core.io.canonical(tree), **{"file-" + core.io.sha(path.encode()): raw for path, raw in self.mathlib.items()}}, git_commit="b" * 40)
        self.source("mathlib-docs", {"declaration_oracle": self.files["oracle.json"]}, parent=mathlib)
        self.source("wikidata-observation", {"wikidata_universe": self.files["catalog/data/wikidata_universe.jsonl"]})
        fc = self.source("fixture-fc", {"fc-source": b"original fixture source"})
        self.source("git-harvest-formal-conjectures", {"formal-conjectures": self.files["catalog/data/formal_conjectures.jsonl"]}, parent=fc)
        self.plan = {"schema": core.PLAN_SCHEMA, "proposal_git_commit": self.pin, "manual_git_commit": self.pin,
            "parents": sorted(self.sources, key=lambda s: s["source"]), "reviewed_parent_manifest_ids": {name: m["source_manifest_id"] for name, m in self.manifests.items()},
            "bindings": {name: {"source": source, "object": obj} for name, (source, obj, _path) in core.BINDINGS.items() if source is not None}}
        self.plan["bindings"].update(grounding={"source": curated["source"], "object": "grounding"}, registry={"source": curated["source"], "object": "source_registry"})
        self.roots = {"fixture_parent": self.parents, "fixture_git": self.repository, core.GIT_ROOT: self.repository}
        profile = {"files": [{"path": path, "sha256": core.io.sha((core.ROOT / path).read_bytes())} for path in core.TOOL_FILES]}
        profile["profile_id"] = core.profile_id(profile)
        self.profile = profile
        registry = self.root / "profiles.json"; registry.write_bytes(core.io.canonical({"schema": core.PROFILE_SCHEMA, "current_profile": profile["profile_id"], "profiles": [profile]}))
        patch = mock.patch.object(core, "REGISTRY", registry); patch.start(); self.addCleanup(patch.stop)

    def git(self, *args):
        return subprocess.check_output(["/usr/bin/git", "-C", str(self.repository), *args], stderr=subprocess.PIPE,
            env={"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "LC_ALL": "C"})

    def commit(self, text):
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", text)

    def write(self, path, raw):
        target = self.repository / path; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(raw)

    def build(self):
        return producer.export(self.plan, self.roots, self.store, normalized_at=WHEN)

    def read_output(self, target, source, name, suffix=".jsonl"):
        return (target / "normalized" / source / (name + suffix)).read_bytes()

    def test_complete_replay_preserves_manual_origin_and_completed_rejection(self):
        target = self.build(); verified = producer.verify(target, self.roots)
        self.assertEqual(len(verified["source_manifest_ids"]), 4)
        self.assertEqual(self.read_output(target, core.MANUAL, "manual-containers"), jl(self.manual))
        self.assertEqual(len(core.adapter.rows(self.read_output(target, core.COMPOSED, "container-links"))), 6)
        report = json.loads(self.read_output(target, core.COMPARISON, "fold-comparison", ".json"))
        self.assertTrue(report["empty_fc_seed_reproduces_seeded_fold"])
        self.assertFalse(report["authority_baseline_approved"])
        self.assertEqual(len(report["explicit_fc_retractions"]), 1)
        self.assertEqual(report["delta"]["containers"]["removed"], [])
        self.assertEqual(report["delta"]["fc"]["removed"][0]["decl"], "Namespace.short.bad")
        fragment = json.loads((target / "source-fragment.json").read_bytes())
        self.assertNotIn("input_bindings", fragment)
        self.assertFalse(fragment["authority_bindings_changed"])
        fold_manifest = json.loads((target / "source-manifests" / (core.FOLD + ".json")).read_bytes())
        self.assertNotIn("prior-fc", fold_manifest["normalization"]["inputs"])
        for original in self.plan["parents"]: self.assertIn(original, fragment["sources"])
        self.assertEqual(self.build(), target)

    def test_equal_contributions_keep_both_origins_and_conflicts_refuse(self):
        raw = jl([self.manual[0]])
        projected, evidence = core.compose_containers(raw, raw, ["sha256:" + "a" * 64, "sha256:" + "b" * 64])
        self.assertEqual(len(core.adapter.rows(projected)), 1)
        self.assertEqual(json.loads(evidence)["n_contributions"], 2)
        with self.assertRaisesRegex(core.io.ExportError, "contributions conflict"):
            core.compose_containers(raw, jl([{**self.manual[0], "confidence": "low"}]), ["first", "second"])

    def test_nested_boolean_integer_difference_never_collapses_or_hides_drift(self):
        left = jl([{**self.manual[0], "evidence": {"nested": [1]}}])
        right = jl([{**self.manual[0], "evidence": {"nested": [True]}}])
        with self.assertRaisesRegex(core.io.ExportError, "contributions conflict"):
            core.compose_containers(left, right, ["first", "second"])
        delta = core.semantic_delta(left, right, lambda row: (row["qid"], row["path"]))
        self.assertEqual(len(delta["changed"]), 1)
        self.assertEqual(delta["added"], [])
        self.assertEqual(delta["removed"], [])

    def test_git_complete_scope_and_manual_predecessor_are_proved(self):
        groups = core.capture_curations(self.plan, self.roots)
        self.assertEqual(groups[core.MANUAL_BEFORE]["commit"], self.before)
        damaged = copy.deepcopy(groups[core.PROPOSALS])
        del damaged["values"]["brain/proposals/fixture.jsonl.verified.jsonl"]
        with self.assertRaisesRegex(core.io.ExportError, "omits a proposal shard"):
            core.verify_git(damaged["values"], damaged["commit"], damaged["tree"], damaged["proof"], proposal_scope=True)
        groups[core.MANUAL_AFTER]["values"][core.CONTAINER_PATH] += jl([self.manual[0]])
        with self.assertRaisesRegex(core.io.ExportError, "five reviewed"):
            core.manual_contributions(groups)

    def test_dirty_untracked_shards_and_prior_outputs_do_not_supply_inputs(self):
        target = self.build()
        self.write("brain/proposals/untracked.jsonl", b'{"action":"xref"}\n')
        self.write("brain/proposals/fixture.jsonl", b"wrong dirty bytes")
        self.write(core.CONTAINER_PATH, b"wrong dirty bytes")
        with mock.patch.dict(os.environ, {"BRAIN_MATHLIB_CHECKOUT": "/absent", "WIKILEAN_REPLAY_CONTEXT": "/absent"}), \
                mock.patch("socket.socket", side_effect=AssertionError("network")):
            self.assertEqual(self.build(), target)

    def test_parent_id_substitution_and_missing_ancestor_refuse(self):
        changed = copy.deepcopy(self.plan); changed["reviewed_parent_manifest_ids"]["wikidata-observation"] = "sha256:" + "e" * 64
        with self.assertRaisesRegex(core.io.ExportError, "explicitly reviewed identity"):
            core.capture_parents(changed, self.roots)
        changed = copy.deepcopy(self.plan)
        changed["parents"] = [s for s in changed["parents"] if s["source"] != "fixture-hf"]
        del changed["reviewed_parent_manifest_ids"]["fixture-hf"]
        with self.assertRaisesRegex(core.io.ExportError, "ancestor is missing"):
            core.capture_parents(changed, self.roots)

    def test_mathlib_subtree_includes_nonlean_source_search_and_cannot_omit(self):
        _s, _m, _o, captured, paths = core.capture_parents(self.plan, self.roots)
        self.assertIn("Mathlib/README.md", paths)
        member = next(o for s in self.plan["parents"] if s["source"] == "mathlib-source" for o in s["objects"] if o["name"] == "file-" + core.io.sha(b"Mathlib/README.md"))
        (self.parents / member["path"]).write_bytes(b"wrong bytes")
        with self.assertRaisesRegex(core.io.ExportError, "exact regular file|bytes differ"):
            core.capture_parents(self.plan, self.roots)

    def test_rehashed_modified_output_and_program_preimage_reject(self):
        target = self.build()
        path = target / "normalized" / core.MANUAL / "manual-containers.jsonl"
        path.write_bytes(b"[]\n")
        document = json.loads((target / "export.json").read_bytes())
        document["files"][path.relative_to(target).as_posix()] = {"sha256": core.io.sha(path.read_bytes()), "bytes": path.stat().st_size}
        document["export_id"] = core.contracts.domain_hash(core.EXPORT_SCHEMA, {k:v for k,v in document.items() if k != "export_id"})
        (target / "export.json").write_bytes(core.io.canonical(document))
        with self.assertRaisesRegex(core.io.ExportError, "independent reduction"):
            producer.verify(target, self.roots)
        programs = producer.implementation(); programs["brain/fold_proposals.py"] += b"\n# changed executed helper\n"
        with self.assertRaisesRegex(core.io.ExportError, "unreviewed fold program"):
            core.build_documents(self.plan, *core.capture_parents(self.plan, self.roots), core.capture_curations(self.plan, self.roots), self.profile, programs, WHEN)

    def test_unknown_entity_and_opaque_fc_seed_abort_before_publication(self):
        self.write("brain/proposals/new.jsonl", jl([{"qid":"Q999", "decl":"Oracle.ok"}]))
        self.git("add", "brain/proposals/new.jsonl"); self.commit("Unknown QID")
        self.plan["proposal_git_commit"] = self.git("rev-parse", "HEAD").decode().strip()
        with self.assertRaisesRegex(core.adapter.FoldError, "empty entity"):
            self.build()
        self.assertEqual(list(self.store.iterdir()), [])
        self.plan["proposal_git_commit"] = self.pin
        self.write("brain/data/fc_links.jsonl", jl([{"qid":"Q1","decl":"unproposed.opaque"}]))
        self.git("add", "brain/data/fc_links.jsonl"); self.commit("Opaque prior seed")
        self.plan["proposal_git_commit"] = self.git("rev-parse", "HEAD").decode().strip()
        # This commit also retains the unknown proposal, so remove it in Git.
        self.git("rm", "brain/proposals/new.jsonl"); self.commit("Remove unknown fixture proposal")
        self.plan["proposal_git_commit"] = self.git("rev-parse", "HEAD").decode().strip()
        with self.assertRaisesRegex(core.io.ExportError, "opaque prior FC"):
            self.build()
        self.assertEqual(list(self.store.iterdir()), [])

    def test_uncertain_rename_removes_only_owned_target(self):
        original = core.io.stage_io.publish_directory_no_replace
        def interrupted(owned, target):
            original(owned, target)
            owned.path.mkdir(mode=0o700)
            (owned.path / "unrelated").write_bytes(b"preserve")
            raise OSError("after rename")
        with mock.patch.object(core.io.stage_io, "publish_directory_no_replace", side_effect=interrupted), self.assertRaisesRegex(OSError, "after rename"):
            self.build()
        remaining = list(self.store.iterdir())
        self.assertEqual(len(remaining), 1)
        self.assertEqual((remaining[0] / "unrelated").read_bytes(), b"preserve")

    def test_actual_compiler_resolves_native_git_sources_and_full_fold_closure(self):
        import test_compile_offline_pack_v2 as fixture_module
        target = self.build(); fragment = json.loads((target / "source-fragment.json").read_bytes())
        fixture = fixture_module.OfflinePackCompilerTest(); fixture.setUp(); self.addCleanup(fixture.tearDown)
        fixture._upgrade_plan_v3()
        fixture.inventory["roots"].append({"id":"fold_result", "kind":"external_tree"}); fixture.inventory["roots"].sort(key=lambda x:x["id"])
        fixture.inventory["inputs"].append({"id":"candidate-fold", "class":"immutable_source_object", "cardinality":"one", "consumers":["brain/replay.py"],
            "path":"candidate.jsonl", "purpose":"fixture fold proof only", "requirement":"required", "root":"fold_result"})
        fixture.inventory["inputs"].append({"id":"candidate-comparison", "class":"immutable_source_object", "cardinality":"one", "consumers":["brain/replay.py"],
            "path":"comparison.json", "purpose":"fixture separate comparison evidence", "requirement":"required", "root":"fold_result"})
        fixture.inventory["inputs"].sort(key=lambda x:x["id"])
        fixture.inventory["inventory_id"] = core.contracts.reducer_input_inventory_identity(fixture.inventory)
        fixture.plan["inventory_id"] = fixture.inventory["inventory_id"]
        fixture.plan["sources"] = sorted([*fixture.plan["sources"], *fragment["sources"]], key=lambda x:x["source"])
        fixture.plan["input_bindings"].append({"input_id":"candidate-fold", "state":"present", "sources":[core.COMPOSED], "members":[{"path":"candidate.jsonl", "source":core.COMPOSED, "object":"container-links"}]})
        fixture.plan["input_bindings"].append({"input_id":"candidate-comparison", "state":"present", "sources":[core.COMPARISON], "members":[{"path":"comparison.json", "source":core.COMPARISON, "object":"fold-comparison"}]})
        fixture.plan["input_bindings"].sort(key=lambda x:x["input_id"])
        fixture.inventory_path.write_bytes(core.io.canonical(fixture.inventory)); fixture.plan_path.write_bytes(core.io.canonical(fixture.plan))
        result_root = self.root / "fold-result"; result_root.mkdir(mode=0o700)
        (result_root / "candidate.jsonl").write_bytes(self.read_output(target, core.COMPOSED, "container-links"))
        (result_root / "comparison.json").write_bytes(self.read_output(target, core.COMPARISON, "fold-comparison", ".json"))
        packed = fixture_module.compiler.compile_offline_pack_v2(fixture.plan_path.resolve(), fixture.inventory_path.resolve(), (fixture.base / "fold-pack").resolve(),
            roots={"repo":fixture.repo.resolve(), "external":fixture.external.resolve(), **self.roots, core.PHYSICAL_ROOT:target, "fold_result":result_root}, git_executable="/usr/bin/git")
        manifest,_ = core.contracts.load_canonical_json(packed.manifest_path)
        core.contracts.verify_offline_pack_files(manifest, packed.root, manifest_path=packed.manifest_path)


if __name__ == "__main__": unittest.main()
