"""Pure legacy semantics and complete parent/tool/compiler evidence fixtures."""
from __future__ import annotations

import copy
import io
import importlib.util
import importlib._bootstrap_external
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import git_harvest_sources as core
import export_git_harvest as cli

WHEN = "2026-09-08T20:00:00Z"


class GitHarvestTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.store = self.root / "harvests"
        self.profile, self.programs, self.dependencies = core.current_implementation()
        self.registry(core, self.profile, "harvest-profile.json")
        parent = core.parent
        self.parent_programs = {name: (core.ROOT / name).read_bytes() for name in parent.TOOL_FILES}
        self.parent_profile = {"files": [{"path": name, "sha256": core.sha(raw)} for name, raw in sorted(self.parent_programs.items())],
                               "repositories": parent.REPOSITORIES}
        self.parent_profile["profile_id"] = parent.profile_id(self.parent_profile)
        self.registry(parent, self.parent_profile, "parent-profile.json")
        self.families = copy.deepcopy(core.FAMILIES)
        self.license = b"fixture reviewed license\n"
        for options in self.families.values(): options["license_sha256"] = core.sha(self.license)
        patch = mock.patch.object(core, "FAMILIES", self.families); patch.start(); self.addCleanup(patch.stop)
        self.trees = {
            "formal-conjectures": {"FormalConjectures/Example.lean": ("100644", b"/-! Module https://www.erdosproblems.com/42 -/\nnamespace Demo\n/-- Cafe\xcc\x81 https://oeis.org/A000045 -/\n@[category research open, AMS 11]\ntheorem alpha : True := by trivial\nend Demo\n")},
            "erdos": {"data/problems.yaml": ("100644", b"- number: 42\n  status: {state: open}\n  prize: '$100'\n  oeis: [A000045]\n  tags: [number theory]\n  formalized: {state: yes}\n- number: 43\n  prize: 'no'\n")},
            "tauceti": {"TauCeti/Example.lean": ("100644", b"namespace Example\n/-- A retained statement -/\ndef beta : Nat := 1\nend Example\n")}}
        self.roots = {}
        self.ids = {}
        for family, options in self.families.items():
            self.trees[family].update({"LICENSE": ("100644", self.license), "ignored.txt": ("100644", b"not a parser input"),
                "unselected-link": ("120000", b"ignored.txt")})
            self.add_parent(family)
        self.plan = {"schema": core.PLAN_SCHEMA, "reviewed_parent_manifest_ids": self.ids, "families": self.families}

    def registry(self, module, profile, path):
        target = self.root / path
        target.write_bytes(core.canonical({"schema": module.PROFILE_SCHEMA, "current_profile": profile["profile_id"], "profiles": [profile]}))
        patch = mock.patch.object(module, "REGISTRY", target); patch.start(); self.addCleanup(patch.stop)

    @staticmethod
    def tar(files):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:gz") as archive:
            folder = tarfile.TarInfo("upstream"); folder.type = tarfile.DIRTYPE; archive.addfile(folder)
            for path, (mode, raw) in sorted(files.items()):
                member = tarfile.TarInfo("upstream/" + path)
                if mode == "120000":
                    member.type = tarfile.SYMTYPE; member.linkname = raw.decode(); archive.addfile(member)
                else:
                    member.size = len(raw); member.mode = 0o755 if mode == "100755" else 0o644
                    archive.addfile(member, io.BytesIO(raw))
        return output.getvalue()

    def add_parent(self, family):
        parent = core.parent
        name = self.families[family]["parent"]
        plan = {"schema": parent.PLAN_SCHEMA, "source": name, "repository": parent.REPOSITORIES[name], "commit": "a" * 40}
        tree = parent.archive.tree_hash(self.trees[family])
        metadata = {"sha": plan["commit"], "url": "https://api.github.com/repos/" + plan["repository"] + "/git/commits/" + plan["commit"],
            "tree": {"sha": tree, "url": "https://api.github.com/repos/" + plan["repository"] + "/git/trees/" + tree}}
        tool = {"schema": parent.TOOL_SCHEMA, "profile_id": self.parent_profile["profile_id"], "files": self.parent_profile["files"],
            "python": {"sha256": "b" * 64, "version": "CPython 3.12.13 -I -S"}, "gh": {"sha256": "c" * 64, "version": "gh version fixture"}}
        capture = parent.capture_files(plan, {"commit": core.canonical(metadata), "source_tar": self.tar(self.trees[family])}, tool, self.parent_programs, WHEN)
        files = parent.build_export({name: raw for name, raw in capture.items() if name != "manifest.json"}, self.parent_profile, self.parent_programs, WHEN)
        target = parent.archive.publish(files, self.root / (family + "-parents"), parent.EXPORT_SCHEMA)
        self.roots[name] = target
        self.ids[name] = json.loads(files["source-manifest.json"])["source_manifest_id"]

    def build(self, when=WHEN):
        return cli.export(self.plan, self.roots, self.store, normalized_at=when)

    def test_complete_verified_parents_produce_five_legacy_projections(self):
        path = self.build()
        result = core.verify_export(path, self.roots)
        self.assertEqual(set(result["source_manifest_ids"]), set(self.families))
        fragment = json.loads((path / "source-fragment.json").read_bytes())
        self.assertEqual(len(fragment["sources"]), 6)
        self.assertEqual({item["input_id"] for item in fragment["input_bindings"]},
            {"formal-conjectures", "erdos-joins", "external-pages", "external-links", "tauceti"})
        fc = self.rows(path, "formal-conjectures")[1]
        self.assertEqual((fc["decl"], fc["module"], fc["file"]), ("Demo.alpha", "FormalConjectures.Example", "FormalConjectures/Example.lean"))
        self.assertEqual(fc["category"], "research open")
        self.assertIn("Cafe\u0301", fc["docstring"])
        self.assertEqual(self.rows(path, "tauceti")[1]["decl"], "Example.beta")
        joins = self.rows(path, "erdos-joins")
        self.assertEqual(joins[1]["prize"], "$100")
        self.assertIs(joins[1]["formalized"], True)  # Preserve SafeLoader's legacy YAML1.1 'yes' behavior.
        pages, links = self.rows(path, "erdos-pages"), self.rows(path, "erdos-links")
        core.adapters.common.validate_external_pair("erdos", pages[0]["_meta"], pages[1:], links[0]["_meta"], [])
        self.assertEqual(len(pages), 3)
        self.assertEqual(len(links), 1)
        self.assertEqual(self.build(), path)

    @staticmethod
    def rows(path, output):
        return [json.loads(line) for line in (path / "normalized" / core.OUTPUTS[output]).read_bytes().splitlines()]

    def test_lineage_has_exact_selected_native_files_tree_license_and_parent(self):
        path = self.build()
        selection = json.loads((path / "normalization/configuration.json").read_bytes())["selected_native_paths"]
        for family in self.families:
            lineage = json.loads((path / "evidence" / (family + ".json")).read_bytes())
            names = {item["object"] for item in lineage["inputs"]}
            expected = {"git_tree", *("file-" + core.sha(name.encode()) for name in ["LICENSE", *selection[family]])}
            self.assertEqual(names, expected)
            self.assertEqual(lineage["parent_source_manifest_ids"], [self.ids[self.families[family]["parent"]]])
            self.assertNotIn("file-" + core.sha(b"ignored.txt"), names)
        self.assertEqual((path / "parents/formal-conjectures-source/source-fragment.json").read_bytes(),
            (self.roots["formal-conjectures-source"] / "source-fragment.json").read_bytes())

    def test_wrong_reviewed_parent_identity_rejected_before_publication(self):
        self.ids["tauceti-source"] = "sha256:" + "f" * 64
        with self.assertRaisesRegex(core.parent.EvidenceError, "explicitly reviewed"):
            self.build()
        self.assertFalse(self.store.exists())

    def test_rehashed_parent_tree_tampering_cannot_supply_inputs(self):
        root = self.roots["erdosproblems-source"]
        files, _ = core.parent.archive.read_bundle(root, core.parent.EXPORT_SCHEMA)
        source = json.loads(files["source-manifest.json"])
        selected = next(item for item in source["objects"] if item["name"] == "file-" + core.sha(b"data/problems.yaml"))
        files["objects/sha256/" + selected["sha256"]] = b"- number: 99\n"
        self.roots["erdosproblems-source"] = core.parent.archive.publish(core.parent.archive.manifest_files(files, core.parent.EXPORT_SCHEMA),
            self.root / "tampered-parents", core.parent.EXPORT_SCHEMA)
        with self.assertRaisesRegex(core.parent.EvidenceError, "independent public-Git"):
            self.build()

    def test_freshly_reviewed_wrong_license_and_selected_symlink_fail(self):
        for mode, raw, target in (("100644", b"different terms", "LICENSE"), ("120000", b"ignored.txt", "TauCeti/Example.lean")):
            with self.subTest(target=target):
                original = self.trees["tauceti"][target]
                self.trees["tauceti"][target] = (mode, raw)
                self.add_parent("tauceti")
                with self.assertRaises((core.parent.EvidenceError, ValueError)):
                    self.build()
                self.trees["tauceti"][target] = original

    def test_empty_or_wrong_scope_and_unsafe_yaml_are_loud(self):
        import yaml
        for raw in (b"{}\n", b"!!python/object/apply:os.system ['touch /tmp/forbidden']\n", b"- number: ''\n"):
            with self.subTest(raw=raw):
                self.trees["erdos"]["data/problems.yaml"] = ("100644", raw)
                self.add_parent("erdos")
                with self.assertRaises((ValueError, yaml.YAMLError)):
                    self.build()
        self.trees["formal-conjectures"] = {"LICENSE": ("100644", self.license), "other/A.lean": ("100644", b"def alpha := 1")}
        self.add_parent("formal-conjectures")
        with self.assertRaisesRegex(ValueError, "selection is empty"):
            self.build()

    def test_no_ambient_checkout_cache_clock_or_network_supplies_normalization(self):
        first = self.build()
        with mock.patch.dict(os.environ, {"BRAIN_TC_CHECKOUT": "/missing", "BRAIN_FC_CHECKOUT": "/missing", "HTTPS_PROXY": "https://unused.invalid"}), \
                mock.patch("socket.socket", side_effect=AssertionError("network")), \
                mock.patch.object(core.adapters.fc, "ensure_checkout", side_effect=AssertionError("checkout")), \
                mock.patch.object(core.adapters.lean_repo, "ensure_checkout", side_effect=AssertionError("checkout")):
            self.assertEqual(self.build(), first)

    def test_audit_timestamp_changes_do_not_change_source_identity_or_rows(self):
        first = self.build()
        second = self.build("2026-09-09T00:00:00Z")
        self.assertNotEqual(first, second)
        self.assertEqual(core.verify_export(first, self.roots)["source_manifest_ids"], core.verify_export(second, self.roots)["source_manifest_ids"])
        for output in core.OUTPUTS:
            self.assertEqual(self.rows(first, output), self.rows(second, output))

    def test_rehashed_changed_output_is_rejected_by_independent_reduction(self):
        path = self.build()
        files, _ = core.parent.archive.read_bundle(path, core.EXPORT_SCHEMA)
        files["normalized/" + core.OUTPUTS["tauceti"]] = b'{"decl":"forged"}\n'
        target = core.parent.archive.publish(core.parent.archive.manifest_files(files, core.EXPORT_SCHEMA), self.root / "tampered", core.EXPORT_SCHEMA)
        with self.assertRaisesRegex(core.parent.EvidenceError, "independent source reduction"):
            core.verify_export(target, self.roots)

    def test_dependency_and_program_preimages_require_one_whole_generation(self):
        captured = core.capture_parents(self.plan, self.roots)
        for member in ("brain/git_harvest_adapters.py", "brain/ingest/erdosproblems.py", "brain/tools/execution_environment.py"):
            changed = {**self.programs, member: self.programs[member] + b"\n"}
            with self.assertRaisesRegex(core.parent.EvidenceError, "reviewed generation"):
                core.build_documents(self.plan, captured, self.profile, changed, self.dependencies, WHEN)
        for member in ("yaml/constructor.py", "pyyaml-6.0.3.dist-info/METADATA"):
            changed = {**self.dependencies, member: self.dependencies[member] + b"\n"}
            with self.assertRaisesRegex(core.parent.EvidenceError, "PyYAML preimage"):
                core.build_documents(self.plan, captured, self.profile, self.programs, changed, WHEN)
        runtime = copy.deepcopy(self.profile)
        runtime["runtime"]["pyyaml"]["loader"] = "yaml.cyaml.CSafeLoader"
        runtime["profile_id"] = core.profile_id(runtime)
        with self.assertRaisesRegex(core.parent.EvidenceError, "reviewed generation"):
            core.build_documents(self.plan, captured, runtime, self.programs, self.dependencies, WHEN)

    def test_actual_package_identity_and_isolated_bootstrap(self):
        import yaml
        runtime, files = core.dependencies.capture()
        self.assertEqual(runtime["pyyaml"]["version"], "6.0.3")
        self.assertEqual(runtime["pyyaml"]["loader"], "yaml.loader.SafeLoader")
        self.assertIn("yaml/loader.py", files)
        self.assertIn("pyyaml-6.0.3.dist-info/RECORD", files)
        code = "import sys;from pathlib import Path;sys.path.insert(0,sys.argv[1]);import git_harvest_dependencies as d;d.load_yaml(Path(sys.argv[2]));print(d.capture()[0]['pyyaml']['loader'])"
        result = subprocess.run([sys.executable, "-I", "-S", "-c", code, str(core.ROOT / "brain"), str(Path(yaml.__file__).parent)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        self.assertEqual(result.stdout.strip(), "yaml.loader.SafeLoader")

    def test_missing_dependency_or_package_substitution_is_rejected(self):
        import yaml
        with mock.patch.object(yaml, "__version__", "6.0.2"):
            with self.assertRaisesRegex(ValueError, "6.0.3"):
                core.dependencies.capture()
        with self.assertRaisesRegex(ValueError, "another YAML"):
            core.dependencies.load_yaml(self.root)

    def test_valid_unmeasured_bytecode_cannot_supply_root_or_submodule_code(self):
        import yaml
        installed = Path(yaml.__file__).parent
        package = self.root / "isolated-package/yaml"
        shutil.copytree(installed, package, ignore=shutil.ignore_patterns("__pycache__"))
        metadata = next(path for path in installed.parent.iterdir() if path.name.lower() == "pyyaml-6.0.3.dist-info")
        shutil.copytree(metadata, package.parent / metadata.name)
        for relative in ("__init__.py", "loader.py"):
            source = package / relative
            raw = source.read_bytes()
            code = compile(b"raise RuntimeError('unmeasured bytecode executed')\n" + raw, str(source), "exec")
            data = importlib._bootstrap_external._code_to_timestamp_pyc(code, int(source.stat().st_mtime), len(raw))
            cache = Path(importlib.util.cache_from_source(str(source)))
            cache.parent.mkdir(exist_ok=True); cache.write_bytes(data)
        # Establish that this is executable valid bytecode, not a malformed
        # fixture that the normal importer would already ignore.
        normal = subprocess.run([sys.executable, "-I", "-S", "-c", "import sys;sys.path.insert(0,sys.argv[1]);import yaml", str(package.parent)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertNotEqual(normal.returncode, 0)
        self.assertIn("unmeasured bytecode executed", normal.stderr)
        script = "import sys;from pathlib import Path;sys.path.insert(0,sys.argv[1]);import git_harvest_dependencies as d;d.load_yaml(Path(sys.argv[2]));import yaml;assert yaml.safe_load('value: yes')['value'] is True;d.capture();print('captured-source-only')"
        isolated = subprocess.run([sys.executable, "-I", "-S", "-c", script, str(core.ROOT / "brain"), str(package)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        self.assertEqual(isolated.stdout.strip(), "captured-source-only")

    def test_preloaded_unmeasured_yaml_is_not_accepted_as_source_evidence(self):
        script = "import sys;sys.path.insert(0,sys.argv[1]);import yaml;import git_harvest_dependencies as d;d.capture()"
        result = subprocess.run([sys.executable, "-c", script, str(core.ROOT / "brain")], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("fresh process", result.stderr)

    def test_existing_generation_and_symlink_ancestry_are_never_overwritten(self):
        captured = core.capture_parents(self.plan, self.roots)
        files = core.build_documents(self.plan, captured, self.profile, self.programs, self.dependencies, WHEN)
        identity = json.loads(files["manifest.json"])["identity"].removeprefix("sha256:")
        self.store.mkdir(mode=0o700)
        target = self.store / identity; target.mkdir(mode=0o700)
        sentinel = target / "sentinel"; sentinel.write_bytes(b"preserve")
        with self.assertRaises((OSError, core.parent.EvidenceError)):
            self.build()
        self.assertEqual(sentinel.read_bytes(), b"preserve")
        self.assertEqual(list(self.store.iterdir()), [target])
        alias = self.root / "alias"; alias.symlink_to(self.store, target_is_directory=True)
        with self.assertRaisesRegex(core.parent.EvidenceError, "ancestry"):
            cli.export(self.plan, self.roots, alias, normalized_at=WHEN)

    def test_actual_v3_compiler_accepts_all_parent_and_harvest_evidence(self):
        import test_compile_offline_pack_v2 as compiler_fixture
        path = self.build()
        fragment = json.loads((path / "source-fragment.json").read_bytes())
        fixture = compiler_fixture.OfflinePackCompilerTest()
        fixture.setUp(); self.addCleanup(fixture.tearDown)
        fixture._upgrade_plan_v3()
        inventory, plan = fixture.inventory, fixture.plan
        inventory["roots"].extend([{"id": "git_harvest_normalized", "kind": "external_tree"}, {"id": "git_harvest_external", "kind": "external_tree"}])
        inventory["roots"].sort(key=lambda item: item["id"])
        inventory["inputs"].extend({"id": name, "class": "immutable_source_object", "cardinality": "one", "consumers": ["brain/replay.py"],
            "path": core.OUTPUTS[name], "purpose": "harvest compiler integration", "requirement": "required", "root": "git_harvest_normalized"}
            for name in ("formal-conjectures", "erdos-joins", "tauceti"))
        inventory["inputs"].extend({"id": "external-" + name, "class": "immutable_source_object", "cardinality": "many",
            "consumers": ["brain/replay.py"], "path_pattern": "*_" + name + ".jsonl", "purpose": "Erdos pair integration",
            "requirement": "required", "root": "git_harvest_external"} for name in ("pages", "links"))
        inventory["inputs"].sort(key=lambda item: item["id"])
        inventory["inventory_id"] = core.contracts.reducer_input_inventory_identity(inventory)
        plan["inventory_id"] = inventory["inventory_id"]
        plan["sources"].extend(fragment["sources"]); plan["sources"].sort(key=lambda item: item["source"])
        plan["input_bindings"].extend(fragment["input_bindings"])
        plan["input_bindings"].sort(key=lambda binding: binding["input_id"])
        fixture.inventory_path.write_bytes(core.canonical(inventory)); fixture.plan_path.write_bytes(core.canonical(plan))
        roots = {"repo": fixture.repo.resolve(), "external": fixture.external.resolve(), core.PHYSICAL_ROOT: path,
                 "git_harvest_normalized": path / "normalized", "git_harvest_external": path / "normalized/catalog/data/external"}
        roots.update({name.replace("-", "_") + "_export": target for name, target in self.roots.items()})
        packed = compiler_fixture.compiler.compile_offline_pack_v2(fixture.plan_path.resolve(), fixture.inventory_path.resolve(),
            (fixture.base / "harvest-pack").resolve(), roots=roots, git_executable="/usr/bin/git")
        manifest, _ = core.contracts.load_canonical_json(packed.manifest_path)
        core.contracts.verify_offline_pack_files(manifest, packed.root, manifest_path=packed.manifest_path)


if __name__ == "__main__": unittest.main()
