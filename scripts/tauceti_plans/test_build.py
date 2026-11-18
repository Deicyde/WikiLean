"""End-to-end tests of `build` on the fixtures (offline; uses only local git)."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from scripts.tauceti_plans import build as B  # noqa: E402

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
ALPHA = "tauceti:TauCetiRoadmap/Alpha"
BETA = "tauceti:TauCetiRoadmap/Beta"
GAMMA = "tauceti:Completed/Gamma"
L0 = f"{ALPHA}#layer-0-abhyankars-lemma"
L1 = f"{ALPHA}#layer-1--second"
L2 = f"{ALPHA}#layer-2-third"


def _git_repo(src: str, dest: str) -> None:
    shutil.copytree(src, dest)
    env = {"PATH": os.environ.get("PATH", ""), "HOME": dest, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "f", "GIT_AUTHOR_EMAIL": "f@example.invalid", "GIT_COMMITTER_NAME": "f",
           "GIT_COMMITTER_EMAIL": "f@example.invalid", "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
           "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z"}
    for args in (["init", "-q", "-b", "main"], ["add", "."],
                 ["-c", "commit.gpgsign=false", "commit", "-q", "-m", "fixture"]):
        subprocess.run(["git", "-C", dest, *args], check=True, env=env)


def make_inputs(root: str, fetched_at: str = "2026-11-18T00:00:00Z") -> str:
    inputs = os.path.join(root, "inputs")
    os.makedirs(inputs)
    _git_repo(os.path.join(FIX, "roadmap"), os.path.join(inputs, "TauCetiRoadmap"))
    _git_repo(os.path.join(FIX, "trough"), os.path.join(inputs, "trough"))
    shutil.copy(os.path.join(FIX, "atlas-data.json"), inputs)
    shutil.copytree(os.path.join(FIX, "decls"), os.path.join(inputs, "decls"))
    write_pins(inputs, fetched_at)
    return inputs


def write_pins(inputs: str, fetched_at: str) -> None:
    pins = {k: {"fetched_at": fetched_at} for k in B.URLS}
    with open(os.path.join(inputs, "PINS.json"), "w") as f:
        json.dump({"inputs": pins}, f)


def read_rows(out: str, name: str) -> list[dict]:
    with open(os.path.join(out, name), encoding="utf-8") as f:
        return [json.loads(line) for line in f]


class BuildFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.inputs = make_inputs(cls.tmp)
        cls.out = os.path.join(cls.tmp, "out")
        cls.manifest = B.build(cls.inputs, cls.out)
        cls.rows = {n: read_rows(cls.out, n) for n in B.FILES}
        cls.rejected = cls.rows["rejected.jsonl"]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def by_id(self, name):
        return {r["id"]: r for r in self.rows[name]}

    def rejected_where(self, **kw):
        return [r for r in self.rejected if all(r.get(k) == v for k, v in kw.items())]

    # -- determinism and the manifest

    def test_two_builds_are_byte_identical(self):
        out2 = os.path.join(self.tmp, "out2")
        B.build(self.inputs, out2)
        for name in B.FILES + ["manifest.json"]:
            with open(os.path.join(self.out, name), "rb") as a, open(os.path.join(out2, name), "rb") as b:
                self.assertEqual(a.read(), b.read(), name)

    def test_canonical_jsonl(self):
        for name in B.FILES:
            with open(os.path.join(self.out, name), "rb") as f:
                raw = f.read()
            self.assertNotIn(b"\r", raw)
            ids = []
            for line in raw.decode("utf-8").splitlines():
                obj = json.loads(line)
                self.assertEqual(line, json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")))
                ids.append(obj["id"])
            self.assertEqual(ids, sorted(ids), name)

    def test_logical_root_ignores_clocks(self):
        inputs2 = os.path.join(self.tmp, "inputs-later")
        shutil.copytree(self.inputs, inputs2)
        write_pins(inputs2, "2027-01-01T12:34:56Z")
        out3 = os.path.join(self.tmp, "out3")
        m3 = B.build(inputs2, out3)
        self.assertEqual(m3["inputs"]["atlas"]["fetched_at"], "2027-01-01T12:34:56Z")
        self.assertNotEqual(m3, self.manifest)
        self.assertEqual(m3["logical_root"], self.manifest["logical_root"])
        self.assertEqual(B.logical_root(m3), m3["logical_root"])

    def test_manifest_pins_and_files(self):
        m = self.manifest
        self.assertEqual(m["schema"], "wikilean.brain.plans/v1")
        self.assertRegex(m["inputs"]["TauCetiRoadmap"]["commit"], r"^[0-9a-f]{40}$")
        self.assertEqual(m["inputs"]["trough"]["tauceti_rev"], "cd41bffcb343151b3e58c89392e206b11fe78c1c")
        self.assertEqual(m["inputs"]["atlas"]["exported_at"], "2026-11-18T04:04:16Z")
        self.assertRegex(m["inputs"]["mathlib_decls"]["sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual({a["license"] for a in m["attribution"]}, {"Apache-2.0"})
        for name in B.FILES:
            self.assertEqual(m["files"][name]["rows"], len(self.rows[name]))

    def test_out_inside_git_work_tree_refuses(self):
        inside = os.path.join(self.inputs, "TauCetiRoadmap", "export")
        with self.assertRaisesRegex(B.BuildError, "inside the git work tree"):
            B.build(self.inputs, inside)
        self.assertFalse(os.path.exists(inside))

    def test_sha_mismatch_refuses(self):
        inputs2 = os.path.join(self.tmp, "inputs-bad")
        shutil.copytree(self.inputs, inputs2)
        with open(os.path.join(inputs2, "PINS.json"), "w") as f:
            json.dump({"inputs": {"atlas": {"sha256": "0" * 64}}}, f)
        with self.assertRaises(B.BuildError):
            B.build(inputs2, os.path.join(self.tmp, "out-bad"))

    def test_no_prose_copied(self):
        for name in B.FILES + ["manifest.json"]:
            with open(os.path.join(self.out, name), encoding="utf-8") as f:
                raw = f.read()
            for phrase in ("SECRET PROSE", "must not be copied", "Base material", "Near misses"):
                self.assertNotIn(phrase, raw, name)

    # -- roadmaps, layers, states

    def test_roadmaps(self):
        r = self.by_id("roadmaps.jsonl")
        self.assertEqual(sorted(r), [GAMMA, ALPHA, BETA])
        self.assertEqual(r[ALPHA]["title"], "Alpha theory")
        self.assertEqual(r[ALPHA]["origin"], "active")
        self.assertEqual(r[GAMMA]["origin"], "completed")
        self.assertEqual(r[ALPHA]["topic"], "math.NT")
        self.assertIsNone(r[GAMMA]["topic"])
        self.assertEqual(r[BETA]["source"]["path"], "TauCetiRoadmap/Beta/README.md")

    def test_anchors_and_atlas_ids(self):
        lay = self.by_id("layers.jsonl")
        self.assertEqual(sorted(k for k in lay if k.startswith(ALPHA)), sorted([L0, L1, L2]))
        self.assertEqual(lay[L0]["key"], "Layer 0")
        self.assertEqual(lay[L0]["title"], "Layer 0: Abhyankar's lemma")
        self.assertEqual(lay[L0]["source"]["line"], 10)
        # the atlas slug disagrees (`abhyankar-s`): ours is GitHub's, the atlas id is kept beside it
        self.assertEqual(lay[L0]["atlas_id"], f"{ALPHA}#layer-0-abhyankar-s-lemma")
        self.assertIsNone(lay[L1]["atlas_id"])  # agrees
        self.assertNotIn(f"{ALPHA}#0-1-a-subsection", lay)

    def test_layer_ordinals(self):
        lay = self.by_id("layers.jsonl")
        self.assertEqual([lay[x]["ordinal"] for x in (L0, L1, L2)], [0, 1, 2])
        self.assertEqual(lay[f"{BETA}#layer-0-base"]["ordinal"], 0)

    def test_states_and_unassessed_fallback(self):
        lay = self.by_id("layers.jsonl")
        self.assertEqual(lay[L0]["state"], "done")
        self.assertEqual(lay[L1]["state"], "partial")
        self.assertEqual(lay[L0]["status_source"]["path"], "TauCetiRoadmap/Alpha/STATUS.md")
        self.assertEqual(lay[L0]["status_source"]["line"], 2)
        self.assertFalse(lay[L0]["status_readme_current"])
        self.assertEqual(lay[L0]["status_tauceti_rev"], "abcdef")
        self.assertEqual(lay[L2]["state"], "unassessed")  # no entry in the comment
        self.assertIsNone(lay[L2]["status_source"])
        self.assertEqual(lay[f"{BETA}#layer-0-base"]["state"], "unassessed")  # no STATUS.md
        self.assertEqual(len(self.rejected_where(kind="coverage", reason="coverage_no_matching_layer",
                                                 layer_key="Layer 9")), 1)

    # -- edges

    def test_edges_reverified(self):
        e = self.by_id("edges.jsonl")
        # Consumer -> prerequisite: Alpha (and its Layer 1) build on Beta.
        self.assertEqual(sorted(e), sorted([f"depends:{ALPHA}->{BETA}", f"depends:{L1}->{BETA}#layer-0-base"]))
        rm = e[f"depends:{ALPHA}->{BETA}"]
        self.assertEqual((rm["source"], rm["target"]), (ALPHA, BETA))
        self.assertEqual(rm["evidence"]["line"], 7)
        self.assertEqual(rm["more_evidence"], [])
        self.assertEqual(rm["via"], "atlas")
        st = e[f"depends:{L1}->{BETA}#layer-0-base"]
        self.assertEqual(st["evidence"]["line"], 26)  # the match line inside the quoted block

    def test_edges_rejected(self):
        def reason(src, tgt):
            got = [r["reason"] for r in self.rejected if r["kind"] == "edge"
                   and r["atlas_source"] == src and r["atlas_target"] == tgt]
            self.assertEqual(len(got), 1, (src, tgt))
            return got[0]
        self.assertEqual(reason(GAMMA, ALPHA), "line_does_not_name_target")  # `GammaRay` is not Gamma
        self.assertEqual(reason(GAMMA, BETA), "evidence_line_missing")
        self.assertEqual(reason("tauceti:TauCetiRoadmap/Nope", ALPHA), "endpoint_not_at_pin")
        self.assertEqual(reason(BETA, GAMMA), "no_evidence")
        self.assertEqual(reason(f"{ALPHA}#layer-0-abhyankar-s-lemma", L1), "no_evidence_line")
        dropped = self.rejected_where(kind="edge_evidence", source=ALPHA, target=BETA)
        self.assertEqual([r["evidence"]["line"] for r in dropped], [8])

    def test_shared_folder_name_needs_path_or_link(self):
        shared = {"IntegralLattices"}
        here = "TauCetiRoadmap/X/README.md"
        f = B._names_folder
        self.assertFalse(f("**Completed IntegralLattices** Layer 1", "TauCetiRoadmap/IntegralLattices", here, shared))
        self.assertTrue(f("see Completed/IntegralLattices", "Completed/IntegralLattices", here, shared))
        self.assertTrue(f("[L](../IntegralLattices/README.md)", "TauCetiRoadmap/IntegralLattices", here, shared))
        self.assertFalse(f("[L](../../Completed/IntegralLattices/)", "TauCetiRoadmap/IntegralLattices", here, shared))
        self.assertTrue(f("uses Beta.", "TauCetiRoadmap/Beta", here, shared))

    # -- declaration references

    def test_four_resolutions(self):
        d = self.by_id("decl_refs.jsonl")
        self.assertEqual(d[f"{L0}::Nat.succ_le"]["resolution"], "mathlib")
        self.assertEqual(d[f"{L0}::Nat.succ_le"]["resolved_by"], ["mathlib_decls"])
        self.assertEqual(self.manifest["resolved_by"]["mathlib_decls"]["path"], "decls/mathlib.bmp")
        self.assertEqual(d[f"{L0}::TauCeti.Foo.bar"]["resolution"], "tauceti")
        self.assertEqual(d[f"{L0}::TauCeti.Foo.sorried"]["resolution"], "stated")
        self.assertEqual(d[f"{L0}::TauCeti.Foo.sorried"]["resolved_by"], ["tauceti_decls", "trough_deps"])
        for near in ("Nat.Succ_le", "Nat.succ_le_iff", "TauCeti.Foo.ba"):
            self.assertEqual(d[f"{L0}::{near}"]["resolution"], "planned", near)
            self.assertIsNone(d[f"{L0}::{near}"]["resolved_by"])

    def test_scoped_and_relative_names(self):
        d = self.by_id("decl_refs.jsonl")
        via = d[f"{ALPHA}::Foo.bar"]  # `open TauCeti in` in a README code block
        self.assertEqual((via["resolution"], via["via_namespace"], via["resolved_name"]),
                         ("tauceti", "TauCeti", "TauCeti.Foo.bar"))
        un = d[f"{BETA}#layer-0-base::Foo.bar"]  # Beta declares no scope
        self.assertEqual((un["resolution"], un["candidates"], un["candidates_total"],
                          un["candidate_libraries"]), ("unqualified", ["TauCeti.Foo.bar"], 1, ["tauceti"]))
        self.assertIsNone(un["resolved_by"])
        self.assertEqual(self.rejected_where(kind="decl_ref", candidate="hf.ne'")[0]["reason"], "local_term")
        self.assertNotIn(f"{ALPHA}::hf.ne'", d)

    def test_decl_attachment(self):
        d = self.by_id("decl_refs.jsonl")
        self.assertEqual(d[f"{L0}::Nat.pred"]["layer"], L0)  # subsection of Layer 0
        self.assertIsNone(d[f"{ALPHA}::Nat.add_comm"]["layer"])  # after the layers
        self.assertEqual(d[f"{ALPHA}::Nat.add_comm"]["roadmap"], ALPHA)
        self.assertNotIn(f"{L0}::Inside.fence", d)
        rej = {r["candidate"]: r["reason"] for r in self.rejected if r["kind"] == "decl_ref"}
        self.assertEqual(rej["Mathlib.Data.Nat"], "module_name")
        self.assertEqual(rej["Notes.lean"], "file_name")
        self.assertEqual(rej["TauCetiRoadmap.Beta"], "roadmap_module")

    def test_suggested_lean_namespaces(self):
        d = self.by_id("decl_refs.jsonl")
        self.assertEqual(d[f"{L0}::TauCetiRoadmap.Alpha.foo"]["evidence"]["path"],
                         "TauCetiRoadmap/Alpha/Suggested.lean")
        self.assertEqual(d[f"{L1}::TauCetiRoadmap.Alpha.Inner.bar"]["resolution"], "planned")
        self.assertEqual(d[f"{L1}::Nat.succ_le"]["resolution"], "mathlib")  # `_root_.`
        self.assertIn(f"{ALPHA}::top_level", d)  # its marker names no README layer
        names = {r["name"] for r in d.values()}
        self.assertFalse({"hidden", "alsoHidden", "TauCetiRoadmap.Alpha.hidden"} & names)
        self.assertEqual(len(self.rejected_where(kind="lean_marker", reason="marker_layer_not_found")), 1)

    # -- xrefs

    def test_xrefs(self):
        x = self.by_id("xrefs.jsonl")
        self.assertEqual(sorted(x), sorted([
            f"{L0}::xref:stacks:0A1B", f"{L2}::xref:stacks:0B3C", f"{L2}::xref:stacks:0C4D",
            f"{L0}::lit:2101.01234", f"{L0}::lit:0704.0001"]))
        self.assertEqual(x[f"{L0}::lit:2101.01234"]["evidence"]["line"], 15)
        reasons = {(r["candidate"], r["reason"]) for r in self.rejected if r["kind"] == "xref"}
        self.assertEqual(reasons, {("2019", "looks_like_year"), ("math/0601001", "old_style_arxiv_id")})


class ResolverTest(unittest.TestCase):
    def setUp(self):
        self.r = B.Resolver({"Nat.succ_le", "A.B.x", "A.B.y", "P.z", "Q.z", "Exact.w", "Q.Exact.w"}
                            | {f"N{i}.amb" for i in range(10)}, {"TauCeti.Foo.bar"})

    def test_unique_suffix_is_unqualified(self):
        got = self.r.resolve("Foo.bar")
        self.assertEqual(got, {"resolution": "unqualified", "candidates": ["TauCeti.Foo.bar"],
                               "candidates_total": 1, "candidate_libraries": ["tauceti"]})

    def test_ambiguous_suffix_capped(self):
        got = self.r.resolve("amb")
        self.assertEqual(got["candidates_total"], 10)
        self.assertEqual(got["candidates"], sorted(f"N{i}.amb" for i in range(10))[:8])

    def test_exact_beats_scope_and_suffix(self):
        self.assertEqual(self.r.resolve("Exact.w", {"Q"}), {"resolution": "mathlib"})

    def test_via_namespace_with_parents(self):
        scopes = B.with_parents({"A.B"})
        self.assertEqual(scopes, {"A", "A.B"})
        self.assertEqual(self.r.resolve("B.x", scopes)["via_namespace"], "A")
        self.assertEqual(self.r.resolve("x", scopes)["via_namespace"], "A.B")

    def test_open_line_gives_scope(self):
        from scripts.tauceti_plans import parse
        found = parse.code_scopes(["open scoped TauCeti Other in", "namespace A.B -- c"])
        self.assertEqual(found, {"TauCeti", "Other", "A.B"})
        found2 = set()
        parse.lean_scan("namespace A\nnamespace B\nopen P hiding q\nend B\nend A\n", found2)
        self.assertEqual(found2, {"A", "A.B", "P"})
        got = self.r.resolve("Foo.bar", B.with_parents(found))
        self.assertEqual((got["resolution"], got["via_namespace"]), ("tauceti", "TauCeti"))

    def test_two_scope_matches_are_unqualified(self):
        got = self.r.resolve("z", {"P", "Q"})
        self.assertEqual((got["resolution"], got["candidates"]), ("unqualified", ["P.z", "Q.z"]))

    def test_local_term(self):
        self.assertEqual(self.r.resolve("hf.ne'"), {"resolution": "local_term"})

    def test_partial_component_stays_planned(self):
        self.assertEqual(self.r.resolve("ucc_le"), {"resolution": "planned"})
        for near in ("Nat.Succ_le", "Nat.succ_le_iff", "TauCeti.Foo.ba"):
            self.assertEqual(self.r.resolve(near), {"resolution": "planned"}, near)

    def test_declared_names_resolve_only_exactly(self):
        self.assertEqual(self.r.resolve("Foo.bar", exact_only=True), {"resolution": "planned"})


if __name__ == "__main__":
    unittest.main()
