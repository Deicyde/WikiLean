"""Offline tests of the declaration producers' `fetch`: every network call goes to a fake
opener; git clones come from local fixture repositories. Nothing here touches the network."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from scripts.brain_producers import fetch_decls as F  # noqa: E402
from scripts.brain_producers.common import git, sha256_bytes  # noqa: E402
from scripts.brain_producers.testing import FIX, TAUCETI_REV  # noqa: E402
from scripts.mathlib_decls import fetch as F3  # noqa: E402
from scripts.tauceti_decls import fetch as F2  # noqa: E402
from scripts.tauceti_plans.build import URLS  # noqa: E402
from scripts.tauceti_plans.test_build import _git_repo  # noqa: E402

MATHLIB_SHA = "a" * 40


def _read(rel):
    with open(os.path.join(FIX, rel), "rb") as f:
        return f.read()


class FakeOpener(F.DeclsOpener):
    def __init__(self, remote, tauceti=None):
        self.remote, self.seen = remote, []
        self.pages = {
            URLS["mathlib_decls"]: _read("decls/mathlib.bmp"),
            URLS["tauceti_decls"]: tauceti if tauceti is not None else _read("decls/tauceti.bmp"),
            # Each .bmp's first entry's docLink, resolved against the docs root.
            "https://leanprover-community.github.io/mathlib4_docs/Mathlib/Foo.html":
                f'<a href="https://github.com/leanprover-community/mathlib4/blob/{MATHLIB_SHA}/Mathlib/Foo.lean">'
                f'source</a>'.encode(),
            "https://taucetiproject.github.io/TauCeti/docs/not-a-doc-link":
                f'<a href="https://github.com/TauCetiProject/TauCeti/blob/{TAUCETI_REV}/TauCeti/A.lean">'
                f'source</a>'.encode(),
        }

    def get(self, url):
        self.seen.append(url)
        if url not in self.pages:
            raise AssertionError(f"unexpected GET {url}")
        return self.pages[url]

    def clone(self, url, dest, sparse=None, commit=None):
        assert url == URLS["trough"], url
        super().clone("file://" + self.remote, dest, sparse, commit)  # local: no network


def _no_network(*a, **k):
    raise AssertionError("the network was touched")


@mock.patch.object(urllib.request, "urlopen", _no_network)
class FetchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.remote = os.path.join(self.tmp, "remote", "trough")
        _git_repo(os.path.join(FIX, "trough"), self.remote)
        subprocess.run(["git", "-C", self.remote, "config", "uploadpack.allowFilter", "true"], check=True)
        self.inputs = os.path.join(self.tmp, "inputs")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def pins_json(self, inputs=None):
        with open(os.path.join(inputs or self.inputs, "PINS.json")) as f:
            return json.load(f)["inputs"]

    def test_newest_state_writes_pins(self):
        op = FakeOpener(self.remote)
        F2.fetch(self.inputs, opener=op)
        pins = self.pins_json()
        self.assertEqual(set(pins), {"trough", "tauceti_decls"})
        self.assertEqual(pins["trough"]["commit"], git(self.remote, "rev-parse", "HEAD").decode().strip())
        self.assertEqual(pins["tauceti_decls"]["docs_commit"], TAUCETI_REV)
        self.assertEqual(pins["tauceti_decls"]["sha256"], sha256_bytes(_read("decls/tauceti.bmp")))
        self.assertTrue(os.path.exists(os.path.join(self.inputs, "trough", "data", "tauceti", "deps.json")))
        self.assertNotIn(URLS["mathlib_decls"], op.seen)

    def test_shared_inputs_keep_other_pins_and_widen_the_sparse_checkout(self):
        os.makedirs(self.inputs)
        with open(os.path.join(self.inputs, "PINS.json"), "w") as f:
            json.dump({"inputs": {"TauCetiRoadmap": {"commit": "f" * 40}, "trough": {"commit": "0" * 40}}}, f)
        F2.fetch(self.inputs, opener=FakeOpener(self.remote))
        F3.fetch(self.inputs, opener=FakeOpener(self.remote))
        pins = self.pins_json()
        self.assertEqual(set(pins), {"TauCetiRoadmap", "trough", "tauceti_decls", "mathlib_decls"})
        self.assertEqual(pins["TauCetiRoadmap"], {"commit": "f" * 40})
        self.assertNotEqual(pins["trough"]["commit"], "0" * 40)
        self.assertEqual(pins["mathlib_decls"]["docs_commit"], MATHLIB_SHA)
        listed = git(os.path.join(self.inputs, "trough"), "sparse-checkout", "list").decode().split()
        self.assertEqual(sorted(listed), ["data/mathlib", "data/tauceti"])
        for lib in ("tauceti/deps.json", "mathlib/concept-deps.json"):
            self.assertTrue(os.path.exists(os.path.join(self.inputs, "trough", "data", lib)))

    def test_pinned_fetch_matches(self):
        F3.fetch(self.inputs, opener=FakeOpener(self.remote))
        saved = os.path.join(self.tmp, "PINS.json")
        shutil.copy(os.path.join(self.inputs, "PINS.json"), saved)
        again = os.path.join(self.tmp, "again")
        F3.fetch(again, pins_file=saved, opener=FakeOpener(self.remote))
        self.assertEqual(self.pins_json(again)["trough"]["commit"], self.pins_json()["trough"]["commit"])

    def test_refetch_follows_the_default_branch(self):
        F2.fetch(self.inputs, opener=FakeOpener(self.remote))
        old = git(self.remote, "rev-parse", "HEAD").decode().strip()
        env = {"PATH": os.environ.get("PATH", ""), "HOME": self.tmp, "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_AUTHOR_NAME": "f", "GIT_AUTHOR_EMAIL": "f@example.invalid", "GIT_COMMITTER_NAME": "f",
               "GIT_COMMITTER_EMAIL": "f@example.invalid", "GIT_AUTHOR_DATE": "2026-01-02T00:00:00Z",
               "GIT_COMMITTER_DATE": "2026-01-02T00:00:00Z"}
        for args in (["branch", "aaa-side", old],
                     ["-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", "next"]):
            subprocess.run(["git", "-C", self.remote, *args], check=True, env=env)
        F2.fetch(self.inputs, opener=FakeOpener(self.remote))
        self.assertEqual(self.pins_json()["trough"]["commit"], git(self.remote, "rev-parse", "HEAD").decode().strip())

    def test_sha_mismatch_names_input_keeps_bytes_and_leaves_no_pins(self):
        F2.fetch(self.inputs, opener=FakeOpener(self.remote))
        saved = os.path.join(self.tmp, "PINS.json")
        shutil.copy(os.path.join(self.inputs, "PINS.json"), saved)
        with self.assertRaisesRegex(F.FetchError, "^tauceti_decls: sha256"):
            F2.fetch(self.inputs, pins_file=saved, opener=FakeOpener(self.remote, tauceti=b'{"declarations":{}}'))
        self.assertFalse(os.path.exists(os.path.join(self.inputs, "PINS.json")))
        with open(os.path.join(self.inputs, "decls", "tauceti.bmp"), "rb") as f:
            self.assertEqual(f.read(), _read("decls/tauceti.bmp"))

    def test_a_failed_fetch_keeps_the_other_producers_pins(self):
        """A failed fetch unpins its own two inputs and nothing else: producer 1's entries in a
        shared PINS.json survive, because re-acquiring them costs another trip to the network."""
        os.makedirs(self.inputs)
        p1 = {"TauCetiRoadmap": {"commit": "f" * 40}, "atlas": {"sha256": "1" * 64},
              "mathlib_decls": {"sha256": "2" * 64}}
        with open(os.path.join(self.inputs, "PINS.json"), "w") as f:
            json.dump({"inputs": dict(p1, trough={"commit": "3" * 40},
                                      tauceti_decls={"sha256": "4" * 64})}, f)
        txt = os.path.join(self.tmp, "PINS.txt")
        with open(txt, "w") as f:
            f.write(f"trough {'0' * 40}\n")
        with self.assertRaisesRegex(F.FetchError, "^trough: "):
            F2.fetch(self.inputs, pins_file=txt, opener=FakeOpener(self.remote))
        self.assertEqual(self.pins_json(), p1)  # its own two are gone, producer 1's three are not

    def test_unknown_commit_fails_and_cli_exits_nonzero(self):
        from scripts.brain_producers import cli
        txt = os.path.join(self.tmp, "PINS.txt")
        with open(txt, "w") as f:
            f.write(f"trough {'0' * 40}\n")
        with self.assertRaisesRegex(F.FetchError, "^trough: "):
            F3.fetch(self.inputs, pins_file=txt, opener=FakeOpener(self.remote))
        self.assertFalse(os.path.exists(os.path.join(self.inputs, "PINS.json")))
        with mock.patch.object(F, "DeclsOpener", lambda: FakeOpener(self.remote)), mock.patch("sys.stderr"):
            self.assertEqual(cli.main("scripts.mathlib_decls", ["fetch", "--inputs", self.inputs, "--pins", txt]), 1)


if __name__ == "__main__":
    unittest.main()
