"""Tests of the riemann-hypothesis declaration producer on its fixture (offline; local git only)."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from scripts.brain_producers import decls as D  # noqa: E402
from scripts.brain_producers.common import GitTree  # noqa: E402
from scripts.brain_producers.testing import read_rows, read_tree  # noqa: E402
from scripts.rh_decls import build as B  # noqa: E402
from scripts.rh_decls import fetch as F  # noqa: E402
from scripts.tauceti_plans.test_build import _git_repo  # noqa: E402

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "rh")
LIB = "decl:RiemannHypothesis:"
FOO = LIB + "Alpha.foo"
HELPER_BASIC = LIB + "_private.Alpha.Basic.0.Alpha.helper"
HELPER_OTHER = LIB + "_private.Alpha.Other.0.Alpha.helper"


def write_pins(inputs: str, commit: str | None, fetched_at: str = "2026-11-24T00:00:00Z") -> None:
    entry = {"fetched_at": fetched_at} | ({"commit": commit} if commit else {})
    with open(os.path.join(inputs, "PINS.json"), "w") as f:
        json.dump({"inputs": {"rh": entry}}, f)


def make_inputs(root: str, src: str = FIX) -> str:
    inputs = os.path.join(root, "inputs")
    os.makedirs(inputs)
    _git_repo(src, os.path.join(inputs, "rh"))
    write_pins(inputs, GitTree(os.path.join(inputs, "rh")).commit)
    return inputs


class BuildFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.inputs = make_inputs(cls.tmp)
        cls.commit = GitTree(os.path.join(cls.inputs, "rh")).commit
        cls.out = os.path.join(cls.tmp, "out")
        cls.manifest = B.build(cls.inputs, cls.out)
        cls.rows = {n: {r["id"]: r for r in read_rows(cls.out, n)} for n in B.FILES}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def test_two_builds_are_byte_identical_and_refetch_keeps_the_root(self):
        out2 = os.path.join(self.tmp, "out2")
        B.build(self.inputs, out2)
        self.assertEqual(read_tree(self.out), read_tree(out2))
        write_pins(self.inputs, self.commit, "2030-01-01T00:00:00Z")
        try:
            m = B.build(self.inputs, os.path.join(self.tmp, "out3"))
        finally:
            write_pins(self.inputs, self.commit)
        self.assertEqual(m["logical_root"], self.manifest["logical_root"])
        self.assertEqual(m["inputs"]["rh"]["fetched_at"], "2030-01-01T00:00:00Z")

    def test_a_declaration_carries_its_code_docstring_and_source(self):
        source = {"repo": B.REPO, "commit": self.commit, "path": "src/Alpha/Basic.lean", "line": 8}
        self.assertEqual(self.rows["names.jsonl"][FOO], {
            "id": FOO, "name": "Alpha.foo", "module": "Alpha.Basic", "lean_lib": "Alpha", "kind": "theorem",
            "private": False, "source": source})
        self.assertEqual(self.rows["decls.jsonl"][FOO], {
            "id": FOO, "name": "Alpha.foo", "source_name": "Alpha.foo", "module": "Alpha.Basic",
            "lean_lib": "Alpha", "kind": "theorem", "private": False, "line": 8, "signature": "theorem foo : 1 = 1 := rfl",
            "doc": "The **foo** lemma.", "refs": None, "source": source})

    def test_names_and_decls_are_the_same_declarations(self):
        self.assertEqual(sorted(self.rows["names.jsonl"]), sorted(self.rows["decls.jsonl"]))
        self.assertEqual(sorted(self.rows["decls.jsonl"]), sorted([
            FOO, HELPER_BASIC, HELPER_OTHER, LIB + "Alpha.bar", LIB + "Beta.Point", LIB + "Main.statement",
            LIB + "topLevel"]))

    def test_private_helpers_get_their_lean_names(self):
        decls = self.rows["decls.jsonl"]
        for did, module, line in ((HELPER_BASIC, "Alpha.Basic", 10), (HELPER_OTHER, "Alpha.Other", 3)):
            row = decls[did]
            self.assertEqual((row["private"], row["source_name"], row["module"], row["line"]),
                             (True, "Alpha.helper", module, line))

    def test_namespace_root_and_attribute(self):
        decls = self.rows["decls.jsonl"]
        self.assertEqual((decls[LIB + "topLevel"]["module"], decls[LIB + "topLevel"]["line"]), ("Alpha.Basic", 16))
        self.assertEqual((decls[LIB + "Alpha.bar"]["kind"], decls[LIB + "Alpha.bar"]["line"]), ("def", 12))
        self.assertEqual(decls[LIB + "Beta.Point"]["kind"], "structure")

    def test_a_library_lake_build_skips_is_not_read(self):
        """Challenge restates Main.statement with `sorry`; it is no default target, so it is not
        read, and Main's declaration is not made ambiguous by it."""
        main = self.rows["decls.jsonl"][LIB + "Main.statement"]
        self.assertEqual((main["module"], main["lean_lib"], main["doc"]), ("Main", "Main", "The main statement."))
        files = [r["candidate"] for r in self.rows["rejected.jsonl"].values() if r["reason"] == "outside_default_libs"]
        self.assertEqual(files, ["src/Challenge.lean"])

    def test_a_public_name_in_two_modules_is_rejected_not_guessed(self):
        self.assertNotIn(LIB + "Alpha.dup", self.rows["names.jsonl"])
        rej = [r for r in self.rows["rejected.jsonl"].values() if r["reason"] == "ambiguous_name"]
        self.assertEqual(len(rej), 1)
        self.assertEqual((rej[0]["candidate"], rej[0]["modules"]), ("Alpha.dup", ["Alpha.Basic", "Beta.Dup"]))

    def test_what_the_source_does_not_declare_is_skipped(self):
        names = {r["name"] for r in self.rows["names.jsonl"].values()}
        self.assertFalse(names & {"outside", "Alpha.commented", "Beta.Point.x"})
        self.assertNotIn("Alpha.notes", {r["module"] for r in self.rows["names.jsonl"].values()})

    def test_manifest(self):
        m = self.manifest
        self.assertEqual((m["schema"], m["library"]), ("wikilean.brain.rh-decls/v1", "RiemannHypothesis"))
        self.assertEqual(m["inputs"]["rh"]["commit"], self.commit)
        src_tree = subprocess.run(["git", "-C", os.path.join(self.inputs, "rh"), "rev-parse", "HEAD:src"],
                                  capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(m["inputs"]["rh"]["src_tree"], src_tree)
        self.assertEqual(sorted(m["reader"]), sorted(B.READER_FILES))
        self.assertIsNone(m["attribution"][0]["license"])
        self.assertEqual(m["lake"], {"src_dir": "src", "default_libs": ["Alpha", "Beta", "Main"],
                                     "other_libs": ["Challenge"]})
        c = m["counts"]
        self.assertEqual((c["files"], c["files_in_default_libs"], c["files_with_decls"], c["decls"],
                          c["decls_private"], c["decls_with_doc"]), (5, 4, 4, 7, 2, 2))
        self.assertEqual(c["decls_by_lean_lib"], {"Alpha": 5, "Beta": 1, "Main": 1})
        self.assertEqual(c["rejected_by_reason"], {"decl/ambiguous_name": 1, "file/outside_default_libs": 1})
        self.assertEqual(m["files"]["deps.jsonl"]["rows"], 0)
        self.assertEqual(self.rows["deps.jsonl"], {})


class BuildRefuses(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_a_head_other_than_the_pin_refuses(self):
        inputs = make_inputs(self.tmp)
        write_pins(inputs, "0" * 40)
        with self.assertRaisesRegex(D.BuildError, "does not match pinned"):
            B.build(inputs, os.path.join(self.tmp, "out"))

    def test_an_unpinned_checkout_refuses(self):
        inputs = make_inputs(self.tmp)
        write_pins(inputs, None)
        with self.assertRaisesRegex(D.BuildError, "carry no commit"):
            B.build(inputs, os.path.join(self.tmp, "out"))

    def test_an_out_inside_a_work_tree_refuses(self):
        inputs = make_inputs(self.tmp)
        with self.assertRaisesRegex(D.BuildError, "inside the git work tree"):
            B.build(inputs, os.path.join(inputs, "rh", "out"))

    def test_a_file_that_is_not_utf8_is_rejected_and_the_rest_built(self):
        src = os.path.join(self.tmp, "src-copy")
        shutil.copytree(FIX, src)
        with open(os.path.join(src, "src", "Alpha", "Bad.lean"), "wb") as f:
            f.write(b"theorem bad : True := trivial -- \xff\n")
        inputs = make_inputs(self.tmp, src)
        m = B.build(inputs, os.path.join(self.tmp, "out"))
        self.assertEqual(m["counts"]["rejected_by_reason"],
                         {"decl/ambiguous_name": 1, "file/not_utf8": 1, "file/outside_default_libs": 1})
        self.assertEqual(m["counts"]["decls"], 7)



class LakeConfig(unittest.TestCase):
    LIBS = [("A", [("submodules", "A")]), ("B", [("and_submodules", "B")]), ("C", [("one", "C")])]

    def test_globs_take_modules_as_lake_does(self):
        cases = {"A": None, "A.x": "A", "A.x.y": "A", "B": "B", "B.x": "B", "C": "C", "C.x": None, "AB": None}
        for module, lib in cases.items():
            self.assertEqual(B.lean_lib_of(module, self.LIBS), lib, module)

    def _config(self, text: str):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        src = os.path.join(tmp, "src-copy")
        shutil.copytree(FIX, src)
        with open(os.path.join(src, "lakefile.toml"), "w") as f:
            f.write(text)
        _git_repo(src, os.path.join(tmp, "repo"))
        return B.lake_config(GitTree(os.path.join(tmp, "repo")))

    def test_no_default_targets_reads_every_library_and_roots_default_to_the_name(self):
        prefix, libs, other = self._config('[[lean_lib]]\nname = "Alpha"\n[[lean_lib]]\nname = "Beta"\n'
                                           'globs = ["Beta.*"]\n')
        self.assertEqual((prefix, other), ("", []))
        self.assertEqual(libs, [("Alpha", [("one", "Alpha")]), ("Beta", [("and_submodules", "Beta")])])

    def test_a_library_with_its_own_src_dir_refuses(self):
        with self.assertRaisesRegex(D.BuildError, "its own srcDir"):
            self._config('srcDir = "src"\n[[lean_lib]]\nname = "Alpha"\nsrcDir = "other"\n')

    def test_a_src_dir_outside_the_repository_refuses(self):
        with self.assertRaisesRegex(D.BuildError, "not a folder inside"):
            self._config('srcDir = "../x"\n[[lean_lib]]\nname = "Alpha"\n')


class LocalOpener(F.Opener):
    """The real clone, pointed at a local repository instead of GitHub."""

    def __init__(self, url: str):
        self.url = url

    def clone(self, url, dest, sparse, commit=None):
        super().clone(self.url, dest, sparse, commit)


class Fetch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.remote = os.path.join(self.tmp, "remote")
        _git_repo(FIX, self.remote)
        self.first = GitTree(self.remote).commit
        env = {"PATH": os.environ.get("PATH", ""), "HOME": self.remote, "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_AUTHOR_NAME": "f", "GIT_AUTHOR_EMAIL": "f@example.invalid", "GIT_COMMITTER_NAME": "f",
               "GIT_COMMITTER_EMAIL": "f@example.invalid", "GIT_AUTHOR_DATE": "2026-01-02T00:00:00Z",
               "GIT_COMMITTER_DATE": "2026-01-02T00:00:00Z"}
        with open(os.path.join(self.remote, "src", "Later.lean"), "w") as f:
            f.write("theorem later : True := trivial\n")
        for args in (["config", "uploadpack.allowFilter", "true"],
                     ["config", "uploadpack.allowReachableSHA1InWant", "true"],
                     ["add", "."], ["-c", "commit.gpgsign=false", "commit", "-q", "-m", "later"]):
            subprocess.run(["git", "-C", self.remote, *args], check=True, env=env)
        self.second = GitTree(self.remote).commit
        self.opener = LocalOpener("file://" + self.remote)
        self.inputs = os.path.join(self.tmp, "inputs")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def pins(self) -> dict:
        with open(os.path.join(self.inputs, "PINS.json")) as f:
            return json.load(f)["inputs"]

    def pins_file(self, commit: str) -> str:
        path = os.path.join(self.tmp, "want.json")
        with open(path, "w") as f:
            json.dump({"inputs": {"rh": {"commit": commit}}}, f)
        return path

    def test_newest_then_pinned_then_build(self):
        os.makedirs(self.inputs)
        with open(os.path.join(self.inputs, "PINS.json"), "w") as f:
            json.dump({"inputs": {"trough": {"commit": "a" * 40}}}, f)
        F.fetch(self.inputs, opener=self.opener)
        self.assertEqual(self.pins()["rh"]["commit"], self.second)
        self.assertEqual(self.pins()["trough"], {"commit": "a" * 40})  # another producer's pin is kept
        rh = os.path.join(self.inputs, "rh")
        self.assertFalse(os.path.exists(os.path.join(rh, "tools", "Script.lean")))  # only src/ checked out
        self.assertEqual(subprocess.run(["git", "-C", rh, "rev-parse", "--is-shallow-repository"],
                                        capture_output=True, text=True).stdout.strip(), "true")
        F.fetch(self.inputs, self.pins_file(self.first), opener=self.opener)
        self.assertEqual(self.pins()["rh"]["commit"], self.first)
        m = B.build(self.inputs, os.path.join(self.tmp, "out"))
        self.assertEqual((m["inputs"]["rh"]["commit"], m["counts"]["decls"]), (self.first, 7))

    def test_a_pin_the_remote_lacks_fails_and_leaves_rh_unpinned(self):
        F.fetch(self.inputs, opener=self.opener)
        with self.assertRaisesRegex(F.FetchError, "could not check out"):
            F.fetch(self.inputs, self.pins_file("1" * 40), opener=self.opener)
        self.assertFalse(os.path.exists(os.path.join(self.inputs, "PINS.json")))

    def test_pins_without_a_commit_fail(self):
        path = os.path.join(self.tmp, "empty.json")
        with open(path, "w") as f:
            json.dump({"inputs": {"rh": {}}}, f)
        with self.assertRaisesRegex(F.FetchError, "name no commit"):
            F.fetch(self.inputs, path, opener=self.opener)


if __name__ == "__main__":
    unittest.main()
