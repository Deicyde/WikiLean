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
FOO = LIB + "Development.foo"
HELPER_BASIC = LIB + "_private.Development.Basic.0.Development.helper"
HELPER_OTHER = LIB + "_private.Development.Other.0.Development.helper"
EARLY = LIB + "_private.Development.Other.0.Development.early"


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
        source = {"repo": B.REPO, "commit": self.commit, "path": "src/Development/Basic.lean", "line": 12}
        self.assertEqual(self.rows["names.jsonl"][FOO], {
            "id": FOO, "name": "Development.foo", "module": "Development.Basic", "lean_lib": "Development",
            "kind": "theorem", "private": False, "source": source})
        self.assertEqual(self.rows["decls.jsonl"][FOO], {
            "id": FOO, "name": "Development.foo", "source_name": "Development.foo", "module": "Development.Basic",
            "lean_lib": "Development", "kind": "theorem", "private": False, "line": 12,
            "signature": "theorem foo : 1 = 1", "doc": "The **foo** lemma.", "refs": None, "source": source})

    def test_names_and_decls_are_the_same_declarations(self):
        self.assertEqual(sorted(self.rows["names.jsonl"]), sorted(self.rows["decls.jsonl"]))
        self.assertEqual(sorted(self.rows["decls.jsonl"]), sorted([
            FOO, HELPER_BASIC, HELPER_OTHER, EARLY, LIB + "Development.bar", LIB + "Development.baz",
            LIB + "Development.even", LIB + "Development.odd", LIB + "Development.afterEnd",
            LIB + "Development.shown", LIB + "Development.late", LIB + "Formalism.Point",
            LIB + "riemannHypothesis", LIB + "topLevel"]))

    def test_private_helpers_get_their_lean_names(self):
        decls = self.rows["decls.jsonl"]
        for did, module, line in ((HELPER_BASIC, "Development.Basic", 14), (HELPER_OTHER, "Development.Other", 7)):
            row = decls[did]
            self.assertEqual((row["private"], row["source_name"], row["module"], row["line"]),
                             (True, "Development.helper", module, line))

    def test_in_a_module_a_declaration_outside_a_public_section_is_private(self):
        """Every RH file is a `module`. Before Other.lean's `public section`, `early` is private
        and `shown` is public because it says so; in Formalism/Dup.lean, which is not a module,
        only `private` would make a declaration private."""
        decls = self.rows["decls.jsonl"]
        self.assertEqual((decls[EARLY]["private"], decls[EARLY]["source_name"], decls[EARLY]["line"]),
                         (True, "Development.early", 5))
        self.assertNotIn(LIB + "Development.early", decls)
        for name in ("Development.shown", "Development.late", "Formalism.Point", "riemannHypothesis"):
            self.assertFalse(decls[LIB + name]["private"], name)

    def test_an_end_closes_its_own_scope(self):
        """`noncomputable section` and `mutual` are scopes, so their `end`s leave the declarations
        after them in `Development`. A reader that knows only a bare `section` would close the
        namespace at the first `end` and put `afterEnd` at the root."""
        decls = self.rows["decls.jsonl"]
        self.assertEqual([decls[LIB + "Development." + n]["line"] for n in ("baz", "even", "odd", "afterEnd")],
                         [26, 31, 34, 40])
        self.assertNotIn(LIB + "afterEnd", decls)

    def test_namespace_root_and_attribute(self):
        decls = self.rows["decls.jsonl"]
        self.assertEqual((decls[LIB + "topLevel"]["module"], decls[LIB + "topLevel"]["line"]),
                         ("Development.Basic", 20))
        self.assertEqual((decls[LIB + "Development.bar"]["kind"], decls[LIB + "Development.bar"]["line"]), ("def", 16))
        self.assertEqual(decls[LIB + "Formalism.Point"]["kind"], "structure")

    def test_a_library_lake_build_skips_is_not_read(self):
        """Challenge restates riemannHypothesis with `sorry`; it is no default target, so it is not
        read, and Main's declaration is not made ambiguous by it."""
        main = self.rows["decls.jsonl"][LIB + "riemannHypothesis"]
        self.assertEqual((main["module"], main["lean_lib"], main["doc"]), ("Main", "Main", "The headline claim, proved."))
        files = [r["candidate"] for r in self.rows["rejected.jsonl"].values() if r["reason"] == "outside_default_libs"]
        self.assertEqual(files, ["src/Challenge.lean"])

    def test_a_public_name_in_two_modules_is_rejected_not_guessed(self):
        self.assertNotIn(LIB + "Development.dup", self.rows["names.jsonl"])
        rej = [r for r in self.rows["rejected.jsonl"].values() if r["reason"] == "ambiguous_name"]
        self.assertEqual(len(rej), 1)
        self.assertEqual((rej[0]["candidate"], rej[0]["modules"]), ("Development.dup", ["Development.Basic", "Formalism.Dup"]))

    def test_what_the_source_does_not_declare_is_skipped(self):
        names = {r["name"] for r in self.rows["names.jsonl"].values()}
        self.assertFalse(names & {"outside", "Development.commented", "Formalism.Point.x"})
        self.assertNotIn("Development.notes", {r["module"] for r in self.rows["names.jsonl"].values()})

    def test_manifest(self):
        m = self.manifest
        self.assertEqual((m["schema"], m["library"]), ("wikilean.brain.rh-decls/v1", "RiemannHypothesis"))
        self.assertEqual(m["inputs"]["rh"]["commit"], self.commit)
        src_tree = subprocess.run(["git", "-C", os.path.join(self.inputs, "rh"), "rev-parse", "HEAD:src"],
                                  capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(m["inputs"]["rh"]["src_tree"], src_tree)
        self.assertEqual(sorted(m["reader"]), sorted(B.READER_FILES))
        self.assertIsNone(m["attribution"][0]["license"])
        self.assertEqual(m["lake"], {"src_dir": "src", "default_libs": ["Development", "Formalism", "Main"],
                                     "other_libs": ["Challenge"]})
        c = m["counts"]
        self.assertEqual((c["files"], c["files_in_default_libs"], c["files_with_decls"], c["decls"],
                          c["decls_private"], c["decls_with_doc"]), (5, 4, 4, 14, 3, 3))
        self.assertEqual(c["decls_by_lean_lib"], {"Development": 12, "Formalism": 1, "Main": 1})
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
        with open(os.path.join(src, "src", "Development", "Bad.lean"), "wb") as f:
            f.write(b"theorem bad : True := trivial -- \xff\n")
        inputs = make_inputs(self.tmp, src)
        m = B.build(inputs, os.path.join(self.tmp, "out"))
        self.assertEqual(m["counts"]["rejected_by_reason"],
                         {"decl/ambiguous_name": 1, "file/not_utf8": 1, "file/outside_default_libs": 1})
        self.assertEqual(m["counts"]["decls"], 14)


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

    def test_roots_default_to_the_name_and_empty_globs_take_nothing(self):
        prefix, libs, other = self._config(
            'defaultTargets = ["Development", "Formalism", "Main"]\n[[lean_lib]]\nname = "Development"\n'
            '[[lean_lib]]\nname = "Formalism"\nglobs = ["Formalism.*"]\n[[lean_lib]]\nname = "Main"\nglobs = []\n'
            '[[lean_lib]]\nname = "Other"\n')
        self.assertEqual((prefix, other), ("", ["Other"]))
        self.assertEqual(libs, [("Development", [("one", "Development")]),
                                ("Formalism", [("and_submodules", "Formalism")]), ("Main", [])])

    def test_no_default_targets_refuses(self):
        """Lake's default is no targets, so `lake build` alone builds nothing."""
        with self.assertRaisesRegex(D.BuildError, "names no defaultTargets"):
            self._config('[[lean_lib]]\nname = "Development"\n')

    def test_a_library_with_its_own_src_dir_refuses(self):
        with self.assertRaisesRegex(D.BuildError, "its own srcDir"):
            self._config('srcDir = "src"\ndefaultTargets = ["Development"]\n[[lean_lib]]\nname = "Development"\n'
                         'srcDir = "other"\n')

    def test_a_src_dir_outside_the_repository_refuses(self):
        with self.assertRaisesRegex(D.BuildError, "not a folder inside"):
            self._config('srcDir = "../x"\n[[lean_lib]]\nname = "Development"\n')


def names(source: str) -> list[tuple[str, bool]]:
    """(Lean's name, private) for each declaration `scan` reads, in file order."""
    return [(lean, private) for _, _, lean, private, _ in B.scan(source.strip("\n").splitlines())]


class Scan(unittest.TestCase):
    """Each expected name is the one Lean 4.37 gives (checked by compiling the same text;
    `alias` with Batteries). Several come from patterns in riemann-hypothesis and Mathlib."""

    def test_end_closes_one_scope_per_part_of_its_name(self):
        # RH RingEquivTransportsVarietyPairData.lean: `end C` after `namespace A.B.C`, then reopened
        self.assertEqual(names("""
namespace G.C.M
theorem a : True := trivial
end M
end G.C
namespace G.C.M
theorem b : True := trivial
end G.C.M
theorem c : True := trivial
"""), [("G.C.M.a", False), ("G.C.M.b", False), ("c", False)])
        # Mathlib Topology/FiberBundle/Trivialization.lean
        self.assertEqual(names("""
namespace Bundle.Pretrivialization
def p : Nat := 0
end Pretrivialization
namespace Trivialization
def t : Nat := 0
end Bundle.Trivialization
def top : Nat := 0
"""), [("Bundle.Pretrivialization.p", False), ("Bundle.Trivialization.t", False), ("top", False)])

    def test_a_dotted_end_closes_scopes_opened_one_at_a_time(self):
        self.assertEqual(names("""
namespace Topology
section IsInducing
namespace IsInducing
theorem inside : True := trivial
end IsInducing.IsInducing
theorem outside : True := trivial
end Topology
namespace A
namespace B
theorem x : True := trivial
end A.B
theorem y : True := trivial
"""), [("Topology.IsInducing.inside", False), ("Topology.outside", False), ("A.B.x", False), ("y", False)])

    def test_a_dotted_section_opens_one_scope_per_part(self):
        self.assertEqual(names("""
namespace N
section S.T
def x : Nat := 1
end S.T
def y : Nat := 2
end N
namespace P
section S.T
def w : Nat := 0
end T
end S
def z : Nat := 3
end P
"""), [("N.x", False), ("N.y", False), ("P.w", False), ("P.z", False)])

    def test_public_scopes_in_a_module(self):
        self.assertEqual(names("""
module
namespace M
def i : Nat := 9
public section
def j : Nat := 10
end
end M
namespace Q.R
public section
def a : Nat := 1
end
def b : Nat := 2
end R
@[expose] public section A.B
def c : Nat := 3
end B
def d : Nat := 4
end A
def e : Nat := 5
end Q
"""), [("M.i", True), ("M.j", False), ("Q.R.a", False), ("Q.R.b", True), ("Q.c", False), ("Q.d", False),
       ("Q.e", True)])

    def test_root_in_an_alias_pair_and_a_held_public_before_section(self):
        self.assertEqual(names("""
module
namespace N
public section
theorem t : True ↔ True := Iff.rfl
alias ⟨_root_.mpRoot, mprN⟩ := t
end
public
section
def x : Nat := 1
end
end N
"""), [("N.t", False), ("mpRoot", False), ("N.mprN", False), ("N.x", False)])

    def test_guillemet_names(self):
        self.assertEqual(names("""
namespace Arxiv.«0911.2077»
theorem c : True := trivial
end Arxiv.«0911.2077»
theorem d : True := trivial
namespace N
def «foo bar» : Nat := 1
def «exists» : Nat := 2
end N
"""), [("Arxiv.«0911.2077».c", False), ("d", False), ("N.«foo bar»", False), ("N.exists", False)])

    def test_declaration_forms(self):
        decls = B.scan("""
module

public import Batteries.Tactic.Alias

namespace N
theorem base : True := trivial
alias al := base
theorem iffThm : True ↔ True := Iff.rfl
alias ⟨mpPriv, mprPriv⟩ := iffThm
public section
alias pubAl := base
alias ⟨mpAl, mprAl⟩ := iffThm
alias ⟨_, onlyMpr⟩ := iffThm
instance (priority := high) instPrio : Inhabited Nat := ⟨0⟩
instance (priority := low) : Inhabited Bool := ⟨true⟩
class inductive CI : Prop
  | mk
class abbrev CA (α : Type) := Inhabited α
public meta def m2 : Nat := 2
open Nat in theorem openIn : True := trivial
end
meta def m1 : Nat := 1
public
theorem splitPublic : True := trivial
private
theorem splitPrivate : True := trivial
end N
""".strip("\n").splitlines())
        self.assertEqual([(lean, private, kind) for _, _, lean, private, kind in decls], [
            ("N.base", True, "theorem"), ("N.al", True, "alias"), ("N.iffThm", True, "theorem"),
            ("N.mpPriv", True, "alias"), ("N.mprPriv", True, "alias"), ("N.pubAl", False, "alias"),
            ("N.mpAl", False, "alias"), ("N.mprAl", False, "alias"), ("N.onlyMpr", False, "alias"),
            ("N.instPrio", False, "instance"), ("N.CI", False, "class"), ("N.CA", False, "class"),
            ("N.m2", False, "def"), ("N.openIn", False, "theorem"), ("N.m1", True, "def"),
            ("N.splitPublic", False, "theorem"), ("N.splitPrivate", True, "theorem")])
        self.assertEqual([start for start, _, lean, _, _ in decls if lean.startswith("N.split")], [22, 24])


class Signature(unittest.TestCase):
    TEXT = """
/-- A doc. -/
theorem foo : 1 = 1 := rfl
theorem multi (a b : Nat)
    (h : a = b) : -- the hypothesis
    b = a := by
  exact h.symm
def dflt (x : Nat := 0) : Nat := x
structure P where
  x : Nat
def even : Nat → Bool
  | 0 => true
  | n + 1 => even n
abbrev X := Nat
alias al := foo
opaque op : Nat
theorem next : True :=
  trivial
theorem lt : let x := 1; x = 1 := rfl
theorem absy (x : Int) (h : 0 ≤ x) :
    |x| = x := by
  simp [h]
theorem card (s : Finset Nat) :
    #s ≤ #s := le_rfl
theorem openTerm (a b : Nat) :
    open Classical in if a = b then True else True := by
  split <;> trivial
theorem openLine (a b : Nat) :
    open Classical in
    if a = b then True else True := by
  split <;> trivial
theorem absFun (y : Int) :
    |(fun x => x) y| = |y| := rfl
def evenB : Nat → Bool
  | 0 => true
  | n + 1 => !evenB n
private
theorem splitSig (a : Nat) : a = a := rfl
inductive Two
  | a
  | b
"""

    def test_the_header_stops_before_the_body_and_the_next_declaration(self):
        rows = {r["fq"]: r for r in B.file_decls("src/T.lean", "src/", self.TEXT)}
        self.assertEqual({n: r["signature"] for n, r in rows.items()}, {
            "foo": "theorem foo : 1 = 1",
            "multi": "theorem multi (a b : Nat)\n    (h : a = b) :\n    b = a",
            "dflt": "def dflt (x : Nat := 0) : Nat",
            "P": "structure P",
            "even": "def even : Nat → Bool",
            "X": "abbrev X",
            "al": "alias al := foo",
            "op": "opaque op : Nat",
            "next": "theorem next : True",
            "lt": "theorem lt : let x := 1; x = 1",
            "absy": "theorem absy (x : Int) (h : 0 ≤ x) :\n    |x| = x",
            "card": "theorem card (s : Finset Nat) :\n    #s ≤ #s",
            "openTerm": "theorem openTerm (a b : Nat) :\n    open Classical in if a = b then True else True",
            "openLine": "theorem openLine (a b : Nat) :\n    open Classical in\n    if a = b then True else True",
            "absFun": "theorem absFun (y : Int) :\n    |(fun x => x) y| = |y|",
            "evenB": "def evenB : Nat → Bool",
            "splitSig": "private\ntheorem splitSig (a : Nat) : a = a",
            "Two": "inductive Two"})
        self.assertEqual((rows["foo"]["doc"], rows["foo"]["line"], rows["al"]["kind"]), ("A doc.", 3, "alias"))


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
        self.assertEqual((m["inputs"]["rh"]["commit"], m["counts"]["decls"]), (self.first, 14))

    def test_a_pins_file_that_is_not_a_mapping_is_unreadable_not_a_crash(self):
        os.makedirs(self.inputs)
        with open(os.path.join(self.inputs, "PINS.json"), "w") as f:
            json.dump({"inputs": ["rh"]}, f)
        F.fetch(self.inputs, opener=self.opener)
        self.assertEqual(list(self.pins()), ["rh"])

    def test_a_blob_outside_src_is_an_error_not_a_lazy_fetch(self):
        """fetch checks out `src/` only, so a lakefile whose srcDir is elsewhere names files whose
        blobs were never fetched: build must stop, not fetch them from the remote."""
        with open(os.path.join(self.remote, "lakefile.toml"), "w") as f:
            f.write('srcDir = "tools"\ndefaultTargets = ["Script"]\n[[lean_lib]]\nname = "Script"\n')
        env = {"PATH": os.environ.get("PATH", ""), "HOME": self.remote, "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_AUTHOR_NAME": "f", "GIT_AUTHOR_EMAIL": "f@example.invalid", "GIT_COMMITTER_NAME": "f",
               "GIT_COMMITTER_EMAIL": "f@example.invalid"}
        for args in (["add", "."], ["-c", "commit.gpgsign=false", "commit", "-q", "-m", "tools"]):
            subprocess.run(["git", "-C", self.remote, *args], check=True, env=env)
        F.fetch(self.inputs, opener=self.opener)
        out = os.path.join(self.tmp, "out")
        with self.assertRaisesRegex(D.BuildError, "tools/Script.lean"):
            B.build(self.inputs, out)
        self.assertFalse(os.path.exists(out))

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
