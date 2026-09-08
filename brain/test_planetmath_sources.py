"""PlanetMath fresh-scope, exact Git tree, legacy parser and compiler checks."""
import copy
import gzip
import io
import json
import os
import subprocess
import sys
import tarfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import planetmath_source_evidence as core
import planetmath_normalization as normalization
import planetmath_sources as cli
import test_wikidata_crossref_sources as upstream

WHEN = "2026-09-08T23:00:00Z"


def listed(name, number):
    return {"id": number, "name": name, "full_name": "planetmath/" + name, "private": False,
        "url": "https://api.github.com/repos/planetmath/" + name, "html_url": "https://github.com/planetmath/" + name,
        "owner": {"login": "planetmath", "type": "Organization"}, "default_branch": "master"}


def tex(cid, title, related="", aliases="", body="A sufficiently long first paragraph about mathematical content.", kind="Theorem"):
    return ("\\pmcanonicalname{" + cid + "}\n\\pmtitle{" + title + "}\n\\pmrelated{" + related + "}\n\\pmsynonym{" + aliases + "}\n\\pmtype{" + kind + "}\n\\begin{document}\n" + body + "\n\\end{document}\n").encode()


class PlanetMathSourcesTest(unittest.TestCase):
    def setUp(self):
        self.upstream = upstream.CrossrefExportTest(); self.upstream.setUp(); self.addCleanup(self.upstream.doCleanups)
        self.root = self.upstream.root
        (self.upstream.repository / core.io.REGISTRY_PATH).write_bytes(upstream.registry(planetmath="P7726"))
        self.upstream.git("add", core.io.REGISTRY_PATH)
        self.upstream.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "PlanetMath registry")
        self.upstream.commit = self.upstream.git("rev-parse", "HEAD").decode().strip()
        first, second = upstream.fixture.entity("Q1"), upstream.fixture.entity("Q2")
        first["claims"]["P7726"] = [upstream.statement(s, pid="P7726") for s in ("CamelThing", "camelthing")]
        second["claims"]["P7726"] = [upstream.statement(s, pid="P7726") for s in ("CAMELTHING", "Target")]
        toolchain = upstream.fixture.fake_toolchain()
        self.upstream.bundle = upstream.fixture.acquire.publish_transcript(upstream.fixture.plan_bytes(["Q1", "Q2"]),
            [upstream.fixture.response({"entities": {"Q1": first, "Q2": second}})], store=self.root / "pm-entities",
            acquisition_tool=upstream.fixture.fake_tool(toolchain), acquisition_toolchain=toolchain, audit_time=WHEN)
        export = self.upstream.export(); sources = json.loads((export / "source-fragment.json").read_bytes())["sources"]
        self.roots = {core.io.PHYSICAL_ROOT: export, core.io.GIT_ROOT: self.upstream.repository}
        self.plan = {"schema": core.PLAN_SCHEMA, "parents": sorted(sources, key=lambda s: s["source"]),
            "reviewed_parent_manifest_ids": {s["source"]: core.io.source_plan_contracts._source_manifest_from_plan(s, "fixture")["source_manifest_id"] for s in sources},
            "minimum_repositories": 2, "minimum_pages": 3, "minimum_links": 1}
        self.programs = {name: (core.ROOT / name).read_bytes() for name in core.TOOL_FILES}
        self.profile = {"files": [{"path": name, "sha256": core.sha(self.programs[name])} for name in core.TOOL_FILES], "policy": copy.deepcopy(core.POLICY)}
        self.profile["profile_id"] = core.profile_id(self.profile)
        registry = self.root / "planetmath-profiles.json"
        registry.write_bytes(core.canonical({"schema": core.PROFILE_SCHEMA, "current_profile": self.profile["profile_id"], "profiles": [self.profile]}))
        patch = mock.patch.object(core, "REGISTRY", registry); patch.start(); self.addCleanup(patch.stop)
        self.tool = {"schema": core.TOOL_SCHEMA, "profile_id": self.profile["profile_id"], "files": self.profile["files"],
            "python": {"version": "CPython 3.12.13 -I -S", "sha256": "b" * 64}, "gh": {"version": "gh version fixture", "sha256": "c" * 64}}
        self.repos = [listed("00_Foundations", 1), listed("01_Later", 2)]
        self.trees = {"00_Foundations": {"a.tex": ("100644", tex("CamelThing", "First title", "Target CamelThing Missing", "Alpha")),
                "b.tex": ("100644", tex("Target", "Caf\\'{e}", "CamelThing", body="The \\emph{first} paragraph has $x+y$ and an escaped \\% character.")),
                "invalid.tex": ("100644", b"\\pmtitle{No canonical}"), "LICENSE": ("100644", b"fixture license\n"), "link": ("120000", b"a.tex")},
            "01_Later": {"a.tex": ("100644", tex("CamelThing", "Ignored later title", "Target", "Beta", body="This later duplicate paragraph must not replace the first snippet.", kind="Definition")),
                "b.tex": ("100755", tex("cAmElThInG", "Case fallback", "CamelThing", "")), "UPPER.TEX": ("100644", tex("Ignored", "Upper extension"))}}
        self.heads = {name: ("a" if index == 0 else "b") * 40 for index, name in enumerate(self.trees)}
        self.tree_ids, self.raw, self.records = {}, {}, []
        self.add_response(core.listing_spec(1), core.artifact(self.repos))
        for repo in self.repos:
            name = repo["name"]
            target = self.root / "legacy" / "planetmath" / name; target.mkdir(parents=True)
            subprocess.run(["/usr/bin/git", "init", "-q", str(target)], check=True)
            for path, (mode, data) in self.trees[name].items():
                file = target / path; file.parent.mkdir(parents=True, exist_ok=True)
                if mode == "120000": file.symlink_to(data.decode())
                else: file.write_bytes(data); file.chmod(0o755 if mode == "100755" else 0o644)
            subprocess.run(["/usr/bin/git", "-C", str(target), "add", "--", *self.trees[name]], check=True)
            self.tree_ids[name] = subprocess.check_output(["/usr/bin/git", "-C", str(target), "write-tree"]).decode().strip()
            self.add_response(core.head_spec(repo), self.metadata(repo))
            self.add_response(core.archive_spec(repo, self.heads[name]), self.tar(self.trees[name]))
        self.parents = core.capture_parents(self.plan, self.roots)

    def metadata(self, repo):
        name = repo["name"]; prefix = "https://api.github.com/repos/planetmath/" + name; commit = self.heads[name]; tree = self.tree_ids[name]
        return core.artifact({"sha": commit, "url": prefix + "/commits/" + commit, "commit": {"url": prefix + "/git/commits/" + commit,
            "message": "Cafe\u0301 update", "tree": {"sha": tree, "url": prefix + "/git/trees/" + tree}}})

    def tar(self, files):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:gz") as archive:
            parent = tarfile.TarInfo("upstream-root"); parent.type = tarfile.DIRTYPE; archive.addfile(parent)
            for path, (mode, raw) in files.items():
                member = tarfile.TarInfo("upstream-root/" + path)
                if mode == "120000": member.type = tarfile.SYMTYPE; member.linkname = raw.decode(); archive.addfile(member)
                else:
                    member.size = len(raw); member.mode = 0o755 if mode == "100755" else 0o644; archive.addfile(member, io.BytesIO(raw))
        return output.getvalue()

    def add_response(self, spec, raw):
        self.raw[spec[0]] = raw; self.records.append(core.record(spec, raw))

    def capture(self, when=WHEN): return core.capture_files(self.plan, self.raw, self.records, self.tool, self.programs, when)
    def export(self, when=WHEN):
        return normalization.build_export({k: v for k, v in self.capture(when).items() if k != "manifest.json"}, self.profile, self.programs, self.roots, when)
    def publish(self): return core.archive.publish(self.export(), self.root / "pm-exports", core.EXPORT_SCHEMA)

    def test_full_capture_export_and_every_source_has_closed_lineage(self):
        capture = core.archive.publish(self.capture(), self.root / "pm-captures", core.CAPTURE_SCHEMA)
        core.verify_capture(capture)
        target = self.publish(); verified = normalization.verify_export(target, self.roots)
        self.assertEqual(verified["facts"]["repositories"], 2); self.assertEqual(verified["facts"]["requests"], 5)
        self.assertEqual(set(verified["source_manifest_ids"]), {core.LISTING_SOURCE, core.CHILD, "planetmath-00-source", "planetmath-01-source"})
        listing_id = verified["source_manifest_ids"][core.LISTING_SOURCE]
        for name in ("planetmath-00-source", "planetmath-01-source"):
            lineage = json.loads((target / ("evidence/" + name + ".json")).read_bytes())
            self.assertEqual(lineage["parent_source_manifest_ids"], [listing_id])
        self.assertFalse(any(p.is_symlink() for p in target.rglob("*")))
        self.assertEqual((target / "acquisition/raw/repo-00-archive").read_bytes(), self.raw["repo-00-archive"])

    def test_exact_legacy_main_parity_crosslist_first_fields_and_casefold_join(self):
        import planetmath
        actual = []; qmap = core.qid_map(self.parents[3])
        def emit(db, pages, links, meta): actual.append(core.pair.normalize_pair(db, pages, links, meta, self.programs["brain/ingest/common.py"]))
        with mock.patch.object(planetmath, "list_content_repos", return_value=sorted(self.trees)), mock.patch.object(planetmath, "sync_repo", side_effect=lambda name, age: self.heads[name]), \
                mock.patch.object(planetmath.common, "CACHE_DIR", self.root / "legacy"), mock.patch.object(planetmath.common, "qid_map", return_value=qmap), \
                mock.patch.object(planetmath.common, "emit", side_effect=emit), mock.patch.object(sys, "argv", ["planetmath.py"]): planetmath.main()
        expected = normalization.project(self.plan, self.trees, self.heads, qmap, self.programs)
        self.assertEqual(actual, [expected])
        meta, pages, links = expected; by_id = {p["id"]: p for p in pages}
        self.assertEqual((meta["n_tex"], meta["n_skipped"]), (5, 1))
        self.assertEqual(by_id["CamelThing"]["title"], "First title"); self.assertEqual(by_id["CamelThing"]["kind_hint"], "theorem")
        self.assertEqual(by_id["CamelThing"]["aliases"], ["Alpha", "Beta"])
        self.assertEqual(by_id["CamelThing"]["qid"], "Q1"); self.assertEqual(by_id["cAmElThInG"]["qid"], "Q2")
        self.assertTrue(any(r["dst"] == "Missing" for r in links))
        self.assertNotIn("fetched_at", meta)

    def test_full_listing_pagination_requires_terminal_page_and_unique_public_identities(self):
        rows = [listed("NonMSC" + str(i), 1000 + i) for i in range(98)] + self.repos
        walk = core.ListingWalk(self.plan); self.assertFalse(walk.accept(core.artifact(rows)))
        with self.assertRaises(core.io.ExportError): walk.repositories()
        self.assertTrue(walk.accept(b"[]")); self.assertEqual([r["name"] for r in walk.repositories()], sorted(self.trees))
        with self.assertRaises(core.io.ExportError): walk.accept(b"[]")
        for modified in ([self.repos[0], self.repos[0]], [{**self.repos[0], "private": True}, self.repos[1]],
            [{**self.repos[0], "owner": {"login": "fork", "type": "Organization"}}, self.repos[1]],
            [listed("00_Different", 3), *self.repos]):
            with self.subTest(modified=modified), self.assertRaises(core.io.ExportError): core.ListingWalk(self.plan).accept(core.artifact(modified))
        with self.assertRaisesRegex(core.io.ExportError, "floor"): core.ListingWalk(self.plan).accept(core.artifact(self.repos[:1]))

    def test_missing_added_reordered_and_wrong_default_branch_requests_fail(self):
        cases = [(self.raw, self.records[:-1]), (self.raw, [self.records[1], self.records[0], *self.records[2:]]),
            ({**self.raw, "unexpected": b"x"}, self.records)]
        wrong = copy.deepcopy(self.records); wrong[1]["request"] = core.request(core.head_spec({**self.repos[0], "default_branch": "other"})); cases.append((self.raw, wrong))
        for raw, records in cases:
            with self.subTest(records=records), self.assertRaises(core.io.ExportError): core.replay(self.plan, raw, records)

    def test_complete_git_tree_rejects_missing_bytes_modes_links_and_wrong_repo(self):
        repo = self.repos[0]; name = repo["name"]
        for trees in ({k: v for k, v in self.trees[name].items() if k != "LICENSE"},
            {**self.trees[name], "a.tex": ("100755", self.trees[name]["a.tex"][1])},
            {**self.trees[name], "link": ("120000", b"elsewhere")}):
            with self.assertRaises(core.archive.EvidenceError): core.tree_from_archive(self.tar(trees), self.tree_ids[name])
        changed = json.loads(self.metadata(repo)); changed["url"] = changed["url"].replace("planetmath", "attacker")
        with self.assertRaises(core.io.ExportError): core.head_identity(repo, core.artifact(changed))
        changed = json.loads(self.metadata(repo)); changed["commit"]["tree"]["url"] = "https://example.invalid/tree"
        with self.assertRaises(core.io.ExportError): core.head_identity(repo, core.artifact(changed))

    def test_resource_bounds_and_gzip_integrity_apply_before_tree_reconstruction(self):
        raw = self.raw["repo-00-archive"]
        with mock.patch.dict(core.POLICY, {"maximum_expanded_archive_bytes": 10}), mock.patch.object(core.archive, "extract_source", side_effect=AssertionError("late limit")):
            with self.assertRaisesRegex(core.io.ExportError, "expanded"): core.tree_from_archive(raw, self.tree_ids[self.repos[0]["name"]])
        with self.assertRaises((EOFError, OSError)): core.tree_from_archive(raw[:-4], self.tree_ids[self.repos[0]["name"]])
        with mock.patch.dict(core.POLICY, {"maximum_total_tree_files": 1}):
            with self.assertRaisesRegex(core.io.ExportError, "tree budget"): core.replay(self.plan, self.raw, self.records)

    def test_symlink_tex_is_rejected_and_ambient_cache_network_are_unavailable(self):
        changed = copy.deepcopy(self.trees); changed[self.repos[0]["name"]]["outside.tex"] = ("120000", b"../../private")
        with self.assertRaisesRegex(core.io.ExportError, "symlinks"): normalization.project(self.plan, changed, self.heads, {}, self.programs)
        changed = copy.deepcopy(self.trees); changed[self.repos[0]["name"]]["directory.tex/member.txt"] = ("100644", b"unused")
        with self.assertRaisesRegex(core.io.ExportError, "directories"):
            normalization.project(self.plan, changed, self.heads, {}, self.programs)
        with mock.patch("socket.socket", side_effect=AssertionError("network")), mock.patch.object(Path, "read_text", side_effect=AssertionError("loose file")):
            normalization.project(self.plan, self.trees, self.heads, core.qid_map(self.parents[3]), self.programs)

    def test_reviewed_crossref_scope_registry_and_parent_ids_are_required(self):
        changed = copy.deepcopy(self.plan); changed["reviewed_parent_manifest_ids"]["wikidata-crossrefs"] = "sha256:" + "f" * 64
        with self.assertRaises(core.io.ExportError): core.capture_parents(changed, self.roots)
        selected = dict(self.parents[3]); selected[("wikidata-crossrefs", "requested_qid_scope")] = core.canonical({"schema": core.io.SCOPE_SCHEMA, "qids": ["Q1"]})
        with self.assertRaisesRegex(core.io.ExportError, "scope"): core.qid_map(selected)
        selected = dict(self.parents[3]); selected[(core.io.CURATED_SOURCE, "source_registry")] = upstream.registry(planetmath="P999")
        with self.assertRaisesRegex(core.io.ExportError, "P7726"): core.qid_map(selected)

    def test_every_helper_preimage_is_required_and_audit_time_is_not_identity(self):
        for name in core.TOOL_FILES:
            with self.subTest(name=name), self.assertRaisesRegex(core.io.ExportError, "whole generation"):
                core.capture_files(self.plan, self.raw, self.records, self.tool, {**self.programs, name: self.programs[name] + b"\n"}, WHEN)
        first, second = self.export(), self.export("2026-09-09T00:00:00Z")
        for name in [n for n in first if n.startswith("source-manifests/")]:
            self.assertEqual(json.loads(first[name])["source_manifest_id"], json.loads(second[name])["source_manifest_id"])

    def test_rehashed_normalization_listing_or_native_tree_index_forgery_fails(self):
        for name in ("normalized/planetmath_pages.jsonl", "acquisition/listing.json", "acquisition/request-results.json"):
            files = {k: v for k, v in self.export().items() if k != "manifest.json"}; files[name] = b"{}"
            target = core.archive.publish(core.archive.manifest_files(files, core.EXPORT_SCHEMA), self.root / core.sha(name.encode()), core.EXPORT_SCHEMA)
            with self.assertRaises(core.io.ExportError): normalization.verify_export(target, self.roots)

    def test_acquire_stops_after_bad_head_and_retains_partial_private_body(self):
        plan = self.root / "planetmath-plan.json"; plan.write_bytes(core.canonical(self.plan))
        responses = [self.raw["listing-1"], cli.TransportFailure("fixture", b"partial head", 28)]
        with mock.patch.object(core.github, "require_startup"), mock.patch.object(cli, "runtime_identity", return_value=(self.tool, self.programs)), \
                mock.patch.object(cli.time, "sleep"), mock.patch.object(cli, "transport", side_effect=responses) as transport:
            with self.assertRaises(cli.TransportFailure): cli.acquire(plan, self.roots, self.root / "captures", Path("/usr/bin/git"))
        self.assertEqual(transport.call_count, 2)
        target = next((self.root / "captures-incomplete").iterdir())
        self.assertEqual((target / "raw/repo-00-head").read_bytes(), b"partial head")
        self.assertFalse((target / "receipts").exists()); self.assertFalse(json.loads((target / "failure.json").read_bytes())["authority"])

    def test_producer_success_publishes_then_independently_reopens_after_memory_release(self):
        plan = self.root / "successful-plan.json"; plan.write_bytes(core.canonical(self.plan))
        with mock.patch.object(core.github, "require_startup"), mock.patch.object(cli, "runtime_identity", return_value=(self.tool, self.programs)), \
                mock.patch.object(cli.time, "sleep"), mock.patch.object(cli, "transport", side_effect=[self.raw[row["object"]] for row in self.records]):
            capture = cli.acquire(plan, self.roots, self.root / "producer-captures", Path("/usr/bin/git"))
        target = cli.export(capture, self.roots, self.root / "producer-exports")
        result = normalization.verify_export(target, self.roots)
        self.assertEqual(result["facts"]["repositories"], 2)
        self.assertIn(core.CHILD, result["source_manifest_ids"])

    def test_real_transport_denies_ambient_settings_and_retains_eof_linger_body(self):
        spec = core.listing_spec(1); command = cli.command(spec, Path("/usr/bin/gh"))
        self.assertEqual(command[:6], ["/usr/bin/gh", "api", "--hostname", "github.com", "--method", "GET"])
        self.assertNotIn("--paginate", command); self.assertNotIn("--cache", command)
        self.assertNotIn("--retry", command)
        script = "import os;assert not any(k in os.environ for k in ['HTTPS_PROXY','GH_DEBUG','GH_BROWSER']);os.write(1,b'[]')"
        with mock.patch.object(cli, "command", return_value=[sys.executable, "-I", "-S", "-c", script]): self.assertEqual(cli.transport(spec, Path("/usr/bin/gh")), b"[]")
        script = "import os,time;os.write(1,b'partial');os.close(1);time.sleep(20)"
        with mock.patch.object(cli, "command", return_value=[sys.executable, "-I", "-S", "-c", script]):
            with self.assertRaises(cli.TransportFailure) as caught: cli.transport(spec, Path("/usr/bin/gh"))
        self.assertEqual(caught.exception.raw, b"partial"); self.assertNotEqual(caught.exception.exit_code, 0)

    def test_actual_interrupted_reader_preserves_received_body_and_numeric_exit(self):
        spec = core.listing_spec(1)
        original_read, reads = os.read, [0]
        def interrupted(fd, count):
            if count == 1024 * 1024:
                reads[0] += 1
                if reads[0] == 2: raise KeyboardInterrupt()
            return original_read(fd, count)
        script = "import os,time;os.write(1,b'partial');time.sleep(.1);os.write(1,b'x');time.sleep(20)"
        with mock.patch.object(cli, "command", return_value=[sys.executable, "-I", "-S", "-c", script]), mock.patch.object(cli.os, "read", side_effect=interrupted):
            with self.assertRaises(cli.TransportFailure) as caught: cli.transport(spec, Path("/usr/bin/gh"))
        self.assertEqual(caught.exception.raw, b"partial")
        self.assertNotEqual(caught.exception.exit_code, 0)

    def test_capture_replay_discards_full_trees_and_enforces_aggregate_response_bound(self):
        repos, trees, heads, tree_ids, specs = core.replay(self.plan, self.raw, self.records, retain_trees=False)
        self.assertEqual(trees, {}); self.assertEqual(heads, self.heads); self.assertEqual(tree_ids, self.tree_ids)
        with mock.patch.dict(core.POLICY, {"maximum_total_response_bytes": sum(map(len, self.raw.values())) - 1}):
            with self.assertRaisesRegex(core.io.ExportError, "response budget"): core.replay(self.plan, self.raw, self.records)
        historical = copy.deepcopy(self.profile)
        historical["policy"]["maximum_archive_bytes"] = 64 * 1024 * 1024
        historical["profile_id"] = core.profile_id(historical)
        core.REGISTRY.write_bytes(core.canonical({"schema": core.PROFILE_SCHEMA, "current_profile": historical["profile_id"], "profiles": [historical]}))
        with self.assertRaisesRegex(core.io.ExportError, "policy or generation"): core.profiles()

    def test_actual_v3_compiler_accepts_listing_repo_and_derived_source_layers(self):
        import test_compile_offline_pack_v2 as fixture
        target = self.publish(); fragment = json.loads((target / "source-fragment.json").read_bytes())
        compiler = fixture.OfflinePackCompilerTest(); compiler.setUp(); self.addCleanup(compiler.tearDown); compiler._upgrade_plan_v3()
        plan, inventory = compiler.plan, compiler.inventory
        inventory["roots"].append({"id": "planetmath_normalized", "kind": "external_tree"}); inventory["roots"].sort(key=lambda r: r["id"])
        for kind in ("pages", "links"):
            inventory["inputs"].append({"id": "external-" + kind, "class": "immutable_source_object", "cardinality": "many",
                "consumers": ["brain/replay.py"], "path_pattern": "*_" + kind + ".jsonl", "purpose": "PlanetMath source integration", "requirement": "required", "root": "planetmath_normalized"})
        inventory["inputs"].sort(key=lambda r: r["id"]); inventory["inventory_id"] = core.contracts.reducer_input_inventory_identity(inventory)
        plan["inventory_id"] = inventory["inventory_id"]; plan["sources"].extend(fragment["sources"]); plan["sources"].sort(key=lambda s: s["source"])
        plan["input_bindings"].extend(fragment["input_bindings"]); plan["input_bindings"].sort(key=lambda b: b["input_id"])
        compiler.plan_path.write_bytes(core.canonical(plan)); compiler.inventory_path.write_bytes(core.canonical(inventory))
        packed = fixture.compiler.compile_offline_pack_v2(compiler.plan_path.resolve(), compiler.inventory_path.resolve(), (compiler.base / "pm-pack").resolve(),
            roots={"repo": compiler.repo.resolve(), "external": compiler.external.resolve(), **self.roots, core.PHYSICAL_ROOT: target, "planetmath_normalized": target / "normalized"}, git_executable="/usr/bin/git")
        manifest, _ = core.contracts.load_canonical_json(packed.manifest_path)
        core.contracts.verify_offline_pack_files(manifest, packed.root, manifest_path=packed.manifest_path)


if __name__ == "__main__": unittest.main()
