"""Offline tests of the Literature Links converter on fixtures (uses only local git).

The fixture inputs go through the real plans producer (`scripts.tauceti_plans.build`) first, so
the converter reads a genuine sealed export. Nothing here needs the `literature_links` package.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from scripts.tauceti_plans import build as P  # noqa: E402
from scripts.tauceti_plans_ll import build as L  # noqa: E402

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
MATHLIB_DOCS = "a" * 40
ALPHA = "tauceti:TauCetiRoadmap/Alpha"
NESTED = "tauceti:TauCetiRoadmap/Alpha/Nested"
GAMMA = "tauceti:Completed/Gamma"
L0, L1, L2 = f"{ALPHA}#layer-0-base", f"{ALPHA}#layer-1-second", f"{ALPHA}#layer-2-third"
L21, L3 = f"{ALPHA}#layer-21-inner", f"{ALPHA}#layer-3-last"
MODULE = "TauCetiRoadmap.Alpha.Suggested"
MATHLIB, TAUCETI = L.LIBRARIES["mathlib"][0], L.LIBRARIES["tauceti"][0]
# The ends and location keys the Literature Links spec allows (docs/spec.md, "Fields").
ANN_KEYS = {"subject", "object", "relation", "status", "kind", "id", "note", "extensions", "$schema"}
END_KEYS = {"type", "uri", "location", "version", "sha256", "citation", "identifiers", "title", "role",
            "junk", "extensions"}


def _git_env(dest: str) -> dict:
    return {"PATH": os.environ.get("PATH", ""), "HOME": dest, "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "f", "GIT_AUTHOR_EMAIL": "f@example.invalid", "GIT_COMMITTER_NAME": "f",
            "GIT_COMMITTER_EMAIL": "f@example.invalid", "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
            "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z"}


def _commit(dest: str, message: str) -> None:
    for args in (["add", "-A"], ["-c", "commit.gpgsign=false", "commit", "-q", "-m", message]):
        subprocess.run(["git", "-C", dest, *args], check=True, env=_git_env(dest))


def _git_repo(src: str, dest: str) -> None:
    shutil.copytree(src, dest)
    subprocess.run(["git", "-C", dest, "init", "-q", "-b", "main"], check=True, env=_git_env(dest))
    _commit(dest, "fixture")


def make_export(root: str, tauceti_docs: str | None = None, trough_rev: str | None = None,
                edit_roadmap=None, extra_trough: tuple = ()) -> tuple[str, str]:
    """Fixture inputs and the plans export built from them; Mathlib has a docs commit, and Tau
    Ceti has one only when `tauceti_docs` is given.

    trough's `roadmaps.json` names the roadmap repo's first commit as its `rev` (or `trough_rev`).
    `edit_roadmap(repo)` makes a second roadmap commit after it, so the export is ahead of trough,
    and `extra_trough` adds roadmap entries to trough's file."""
    inputs = os.path.join(root, "inputs")
    os.makedirs(inputs)
    rm = os.path.join(inputs, "TauCetiRoadmap")
    _git_repo(os.path.join(FIX, "roadmap"), rm)
    first = P.GitTree(rm).commit
    if edit_roadmap:
        edit_roadmap(rm)
        _commit(rm, "later")
    trough_src = os.path.join(root, "trough-src")
    shutil.copytree(os.path.join(FIX, "trough"), trough_src)
    rj = os.path.join(trough_src, "data", "tauceti", "roadmaps.json")
    with open(rj, encoding="utf-8") as f:
        tj = json.load(f)
    tj["rev"] = first if trough_rev is None else trough_rev
    tj["roadmaps"] += list(extra_trough)
    with open(rj, "w", encoding="utf-8") as f:
        json.dump(tj, f)
    _git_repo(trough_src, os.path.join(inputs, "trough"))
    shutil.copy(os.path.join(FIX, "atlas-data.json"), inputs)
    shutil.copytree(os.path.join(FIX, "decls"), os.path.join(inputs, "decls"))
    pins = {k: {"fetched_at": "2026-11-18T00:00:00Z"} for k in P.URLS}
    for name in ("TauCetiRoadmap", "trough"):
        pins[name]["commit"] = P.GitTree(os.path.join(inputs, name)).commit
    for name, rel in (("atlas", "atlas-data.json"), ("mathlib_decls", "decls/mathlib.bmp"),
                      ("tauceti_decls", "decls/tauceti.bmp")):
        pins[name]["sha256"] = P.sha256_file(os.path.join(inputs, rel))
    pins["mathlib_decls"]["docs_commit"] = MATHLIB_DOCS
    if tauceti_docs:
        pins["tauceti_decls"]["docs_commit"] = tauceti_docs
    with open(os.path.join(inputs, "PINS.json"), "w") as f:
        json.dump({"inputs": pins}, f)
    export = os.path.join(root, "export")
    P.build(inputs, export)
    return export, inputs


def read_annotations(out: str) -> dict[str, list[dict]]:
    base = os.path.join(out, L.ANNOTATIONS)
    got = {}
    for dirpath, _dirs, files in os.walk(base):
        for fn in files:
            if fn.endswith(".jsonl"):
                with open(os.path.join(dirpath, fn), encoding="utf-8") as f:
                    got[os.path.relpath(os.path.join(dirpath, fn), base)] = [json.loads(x) for x in f if x.strip()]
    return got


def tree_bytes(root: str) -> dict[str, bytes]:
    out = {}
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            p = os.path.join(dirpath, fn)
            with open(p, "rb") as f:
                out[os.path.relpath(p, root)] = f.read()
    return out


class SectionRanges(unittest.TestCase):
    def test_nested_headings_fences_and_end_of_file(self):
        text = ("# T\n"            # 1
                "## A\n"           # 2
                "### A.1\n"        # 3
                "x\n"              # 4
                "#### A.1.a\n"     # 5
                "```\n"            # 6
                "## fenced\n"      # 7  (not a heading)
                "```\n"            # 8
                "### A.2\n"        # 9
                "## B\n"           # 10
                "y\n")             # 11
        r = L.section_ranges(text)
        self.assertEqual(r[1][:2], (1, 11))
        self.assertEqual(r[2][:2], (2, 9))      # A runs over its subsections up to B
        self.assertEqual(r[3][:2], (3, 8))      # A.1 holds A.1.a and the fence
        self.assertEqual(r[5][:2], (5, 8))
        self.assertEqual(r[9][:2], (9, 9))      # a heading right before the next is one line
        self.assertEqual(r[10][:2], (10, 11))   # the last section runs to the end of the file
        self.assertNotIn(7, r)

    def test_line_count(self):
        self.assertEqual(L.line_count("a\nb\n"), 2)
        self.assertEqual(L.line_count("a\nb"), 2)
        self.assertEqual(L.line_count("a\r\nb\r\n"), 2)
        with self.assertRaises(L.BuildError):
            L.line_count("a\rb\n")

    def test_module_name(self):
        self.assertEqual(L.module_of("TauCetiRoadmap/A/B/Suggested.lean"), "TauCetiRoadmap.A.B.Suggested")
        self.assertEqual(L.module_of("Completed/Gamma/Suggested.lean"), "Completed.Gamma.Suggested")

    def test_file_of(self):
        self.assertEqual(L.file_of(NESTED), "TauCetiRoadmap/Alpha.jsonl")
        self.assertEqual(L.file_of(GAMMA), "Completed/Gamma.jsonl")


class Build(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.export, cls.inputs = make_export(cls.tmp)
        cls.out = os.path.join(cls.tmp, "out")
        cls.manifest = L.build(cls.export, cls.inputs, cls.out)
        cls.files = read_annotations(cls.out)
        cls.commit = P.GitTree(os.path.join(cls.inputs, "TauCetiRoadmap")).commit
        cls.by_id = {}
        for anns in cls.files.values():
            for a in anns:
                cls.by_id.setdefault(a["id"], []).append(a)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def rel(self, owner, relation):
        return [a for a in self.by_id[owner] if a.get("relation") == relation]

    def test_layout_and_defaults(self):
        self.assertEqual(sorted(self.files), ["Completed/Gamma.jsonl", "TauCetiRoadmap/Alpha.jsonl"])
        with open(os.path.join(self.out, L.ANNOTATIONS, "_defaults.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"subject": {"type": "generic", "uri": L.ROADMAP_URI,
                                                        "version": self.commit}})
        self.assertTrue(os.path.exists(os.path.join(self.out, "manifest.json")))
        self.assertFalse(os.path.exists(os.path.join(self.out, L.ANNOTATIONS, "manifest.json")))
        # The nested roadmap's layer lives in its top-level roadmap's file.
        self.assertIn(f"{NESTED}#layer-0-n", {a["id"] for a in self.files["TauCetiRoadmap/Alpha.jsonl"]})

    def test_section_lines_of_layers(self):
        def lines(owner):
            (loc,) = {json.dumps(a["subject"][0]["location"]) for a in self.by_id[owner]}
            return json.loads(loc)
        p = "TauCetiRoadmap/Alpha/README.md"
        self.assertEqual(lines(L0), {"path": p, "lines": [5, 16]})    # holds ### and ####
        self.assertEqual(lines(L1), {"path": p, "lines": [17, 25]})   # a fenced `##` is no heading
        self.assertEqual(lines(L2), {"path": p, "lines": [26, 31]})   # holds Layer 2.1
        self.assertEqual(lines(L21), {"path": p, "lines": [28, 31]})
        self.assertEqual(lines(L3), {"path": p, "lines": [36, 38]})   # to the end of the file
        self.assertEqual(lines(ALPHA), {"path": p})                   # a roadmap: the whole README
        self.assertEqual({a["subject"][0]["title"] for a in self.by_id[L0]}, {"Layer 0: base"})

    def test_status_mapping(self):
        got = {o: {a.get("status") for a in self.by_id[o]} for o in (L0, L1, L2, L21, L3)}
        self.assertEqual(got, {L0: {"formalized"}, L1: {"partial"}, L2: {"not_formalized"},
                               L21: {None}, L3: {None}})
        self.assertTrue(all("status" not in a for a in self.by_id[ALPHA] + self.by_id[GAMMA]))

    def test_unlinked_layers(self):
        for owner in (L2, L21, L3, f"{GAMMA}#layer-0-g", f"{NESTED}#layer-0-n"):
            (a,) = self.by_id[owner]
            self.assertEqual(a["object"], [])
            self.assertNotIn("relation", a)
        (l2,) = self.by_id[L2]
        self.assertEqual(l2["status"], "not_formalized")   # an unlinked layer keeps its status
        self.assertEqual(self.manifest["counts"]["unlinked_layers"], 5)
        self.assertEqual(self.manifest["counts"]["layers"], 7)

    def test_related_match_objects(self):
        objs = lambda o: sorted((json.dumps(a["object"], sort_keys=True) for a in self.rel(o, "relatedMatch")))
        m = lambda n: [{"type": "lean", "uri": MATHLIB, "version": MATHLIB_DOCS, "location": {"declaration": n}}]
        t = lambda n: [{"type": "lean", "uri": TAUCETI, "location": {"declaration": n}}]   # no docs commit
        dump = lambda xs: sorted(json.dumps(x, sort_keys=True) for x in xs)
        self.assertEqual(objs(L0), dump([m("Nat.succ_le"), t("TauCeti.Foo.bar"), t("TauCeti.Foo.sorried")]))
        self.assertEqual(objs(ALPHA), dump([m("Nat.add_comm")]))
        self.assertEqual(objs(L1), [])        # `Foo.bar` is unqualified, not a link
        self.assertEqual(self.manifest["counts"]["objects_without_version"], 3)

    def test_has_part_suggested_targets(self):
        sug = lambda n: {"type": "lean", "uri": L.ROADMAP_URI, "version": self.commit,
                         "location": {"declaration": n, "module": MODULE}}
        got = lambda o: sorted(json.dumps(a["object"], sort_keys=True) for a in self.rel(o, "hasPart"))
        want = lambda *ns: sorted(json.dumps([sug(n)], sort_keys=True) for n in ns)
        self.assertEqual(got(L0), want("TauCetiRoadmap.Alpha.target_zero", "TauCeti.Foo.sorried"))
        # Its first occurrence is in the README, so the export's evidence path misses it.
        self.assertEqual(got(L1), want("TauCetiRoadmap.Alpha.target_one"))
        self.assertEqual(got(ALPHA), want("TauCetiRoadmap.Alpha.top"))   # the `## Notes` marker
        self.assertEqual(self.manifest["counts"]["suggested_rows_from_readme_evidence"], 1)

    def test_has_part_trough_results(self):
        got = sorted(json.dumps(a["object"], sort_keys=True) for a in self.rel(GAMMA, "hasPart"))
        want = sorted(json.dumps(x, sort_keys=True) for x in (
            [{"type": "lean", "uri": MATHLIB, "version": MATHLIB_DOCS, "location": {"declaration": "Nat.succ_le"}}],
            [{"type": "lean", "uri": TAUCETI, "location": {"declaration": "TauCeti.Foo.bar"}}]))
        self.assertEqual(got, want)
        for a in self.by_id[GAMMA]:
            self.assertEqual(a["subject"], [{"location": {"path": "Completed/Gamma/README.md"}, "title": "Gamma"}])

    def test_omitted_names_in_extensions(self):
        def ext(owner):
            carriers = [a for a in self.by_id[owner] if "extensions" in a]
            self.assertEqual(len(carriers), 1)        # once per subject, on its first annotation
            self.assertIs(carriers[0], self.by_id[owner][0])
            return carriers[0]["extensions"][L.EXT]
        self.assertEqual(ext(L0), {"planned": ["TauCetiRoadmap.Alpha.later"]})
        self.assertEqual(ext(L1), {"unqualified": ["Foo.bar"]})   # target_one is a link, not omitted
        self.assertEqual(ext(GAMMA), {"unresolved_results": ["No.such.decl"]})
        self.assertTrue(all("extensions" not in a for a in self.by_id[L2]))

    def test_annotation_shape(self):
        for anns in self.files.values():
            for a in anns:
                self.assertLessEqual(set(a), ANN_KEYS)
                self.assertEqual(len(a["subject"]), 1)
                self.assertLessEqual(set(a["subject"][0]), END_KEYS)
                self.assertNotIn("uri", a["subject"][0])          # from the defaults template
                self.assertEqual(bool(a["object"]), "relation" in a)
                for e in a["object"]:
                    self.assertLessEqual(set(e), END_KEYS)
                    self.assertEqual(e["type"], "lean")

    def test_no_prose(self):
        blob = b"".join(tree_bytes(self.out).values())
        for s in (b"SECRET", b"docstring", b"Still Layer 0", b"Runs to the end", b"sits in a subsection"):
            self.assertNotIn(s, blob)

    def test_manifest(self):
        m = self.manifest
        with open(os.path.join(self.export, "manifest.json"), encoding="utf-8") as f:
            src = json.load(f)
        self.assertEqual(m["source_export"]["logical_root"], src["logical_root"])
        for rel, info in m["files"].items():
            with open(os.path.join(self.out, rel), "rb") as f:
                self.assertEqual(L.sha256_bytes(f.read()), info["sha256"])
        self.assertEqual(m["counts"]["annotations/hasPart"], 6)
        self.assertIn("Literature Links (format)", {x["work"] for x in m["attribution"]})

    def test_deterministic(self):
        out2 = os.path.join(self.tmp, "out2")
        L.build(self.export, self.inputs, out2)
        self.assertEqual(tree_bytes(self.out), tree_bytes(out2))

    def test_refuses_out_in_git_tree(self):
        inside = os.path.join(self.inputs, "TauCetiRoadmap", "ll-out")
        with self.assertRaisesRegex(L.BuildError, "inside the git work tree"):
            L.build(self.export, self.inputs, inside)
        self.assertFalse(os.path.exists(inside))

    def test_refuses_nonempty_out(self):
        with self.assertRaisesRegex(L.BuildError, "not an empty directory"):
            L.build(self.export, self.inputs, self.out)

    def test_refuses_tampered_export(self):
        bad = os.path.join(self.tmp, "bad-export")
        shutil.copytree(self.export, bad)
        with open(os.path.join(bad, "layers.jsonl"), "ab") as f:
            f.write(b"\n")
        with self.assertRaisesRegex(L.BuildError, "layers.jsonl"):
            L.build(bad, self.inputs, os.path.join(self.tmp, "never"))


class TroughPins(unittest.TestCase):
    """trough read the roadmaps at its own rev. Its results attach only to the same roadmap at the
    export's commit; anything else is rejected with a reason, never re-attached by path."""

    def build(self, **kw):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        export, inputs = make_export(tmp, **kw)
        out = os.path.join(tmp, "out")
        manifest = L.build(export, inputs, out)
        anns = [a for f in read_annotations(out).values() for a in f]
        trough_links = [a for a in anns if a.get("id") == GAMMA and a.get("relation") == "hasPart"]
        return manifest, trough_links

    def test_same_roadmap_at_an_older_rev_attaches(self):
        def touch_other(repo):   # the export is ahead of trough, but Gamma is unchanged
            with open(os.path.join(repo, "TauCetiRoadmap", "Alpha", "README.md"), "a") as f:
                f.write("\nmore\n")
        manifest, links = self.build(edit_roadmap=touch_other)
        self.assertEqual(len(links), 2)
        self.assertEqual(manifest["rejected"], [])

    def test_a_changed_roadmap_at_the_same_path_is_rejected(self):
        def retitle(repo):       # a different roadmap now sits at Completed/Gamma
            p = os.path.join(repo, "Completed", "Gamma", "README.md")
            with open(p, encoding="utf-8") as f:
                text = f.read()
            with open(p, "w", encoding="utf-8") as f:
                f.write(text.replace("# Gamma", "# Delta", 1))
        manifest, links = self.build(edit_roadmap=retitle)
        self.assertEqual(links, [])
        self.assertEqual([(r["path"], r["reason"]) for r in manifest["rejected"]],
                         [("Completed/Gamma", "roadmap_changed_since_trough_rev")])
        self.assertEqual(manifest["counts"]["trough_rejected/roadmap_changed_since_trough_rev"], 1)

    def test_a_path_missing_at_the_export_commit_is_rejected(self):
        gone = {"id": "Gone", "path": "Completed/Gone", "title": "Gone", "names": ["Nat.succ_le"]}
        manifest, links = self.build(extra_trough=(gone,))
        self.assertEqual(len(links), 2)            # Gamma still attaches
        self.assertEqual([(r["path"], r["reason"]) for r in manifest["rejected"]],
                         [("Completed/Gone", "roadmap_not_at_export_commit")])

    def test_an_absent_or_malformed_trough_rev_is_rejected(self):
        manifest, links = self.build(trough_rev="0" * 40)
        self.assertEqual(links, [])
        self.assertEqual([r["reason"] for r in manifest["rejected"]], ["readme_not_at_trough_rev"])
        manifest, links = self.build(trough_rev="main")
        self.assertEqual(links, [])
        self.assertEqual([r["reason"] for r in manifest["rejected"]], ["trough_rev_unknown"])


class Versions(unittest.TestCase):
    def test_tauceti_docs_commit_becomes_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            export, inputs = make_export(tmp, tauceti_docs="b" * 40)
            m = L.build(export, inputs, os.path.join(tmp, "out"))
            self.assertEqual(m["counts"]["objects_without_version"], 0)
            anns = [a for x in read_annotations(os.path.join(tmp, "out")).values() for a in x]
            tc = [e for a in anns for e in a["object"] if e["uri"] == TAUCETI]
            self.assertTrue(tc and all(e["version"] == "b" * 40 for e in tc))


if __name__ == "__main__":
    unittest.main()
