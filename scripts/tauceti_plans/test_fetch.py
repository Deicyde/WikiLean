"""Offline tests of `fetch`: every network call goes to a fake opener; git clones come from
local fixture repositories. Nothing here touches the network."""

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

from scripts.tauceti_plans import fetch as F  # noqa: E402
from scripts.tauceti_plans.build import URLS  # noqa: E402
from scripts.tauceti_plans.canon import git, sha256_bytes  # noqa: E402
from scripts.tauceti_plans.test_build import FIX, _git_repo  # noqa: E402

MATHLIB_SHA = "a" * 40
TAUCETI_SHA = "cd41bffcb343151b3e58c89392e206b11fe78c1c"


def _read(rel):
    with open(os.path.join(FIX, rel), "rb") as f:
        return f.read()


class FakeOpener(F.Opener):
    def __init__(self, repos, atlas=b'{"meta":{}}'):
        self.repos, self.seen = repos, []
        self.pages = {
            URLS["atlas"]: b'<html><script id="atlas-data" type="application/json">' + atlas + b"</script>",
            URLS["mathlib_decls"]: _read("decls/mathlib.bmp"),
            URLS["tauceti_decls"]: _read("decls/tauceti.bmp"),
            "https://leanprover-community.github.io/mathlib4_docs/Mathlib/A.html":
                f'<a href="https://github.com/leanprover-community/mathlib4/blob/{MATHLIB_SHA}/Mathlib/A.lean">'
                f'source</a><a href="https://github.com/other/repo/blob/{"b" * 40}/x">x</a>'.encode(),
            "https://taucetiproject.github.io/TauCeti/docs/TauCeti/Foo.html":
                f'<a href="https://github.com/TauCetiProject/TauCeti/blob/{TAUCETI_SHA}/TauCeti/Foo.lean#L3">'
                f'source</a>'.encode(),
        }

    def get(self, url):
        self.seen.append(url)
        if url not in self.pages:
            raise AssertionError(f"unexpected GET {url}")
        return self.pages[url]

    def clone(self, url, dest, sparse=None, commit=None):
        super().clone("file://" + self.repos[url], dest, sparse, commit)  # local: no network


def _no_network(*a, **k):
    raise AssertionError("the network was touched")


@mock.patch.object(urllib.request, "urlopen", _no_network)
class FetchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repos = {}
        for name, src in (("TauCetiRoadmap", "roadmap"), ("trough", "trough")):
            path = os.path.join(self.tmp, "remote", name)
            _git_repo(os.path.join(FIX, src), path)
            self.repos[URLS[name]] = path
        self.inputs = os.path.join(self.tmp, "inputs")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def pins_json(self):
        with open(os.path.join(self.inputs, "PINS.json")) as f:
            return json.load(f)["inputs"]

    def test_newest_state_writes_pins(self):
        op = FakeOpener(self.repos)
        F.fetch(self.inputs, opener=op)
        pins = self.pins_json()
        head = git(self.repos[URLS["TauCetiRoadmap"]], "rev-parse", "HEAD").decode().strip()
        self.assertEqual(pins["TauCetiRoadmap"]["commit"], head)
        self.assertEqual(pins["mathlib_decls"]["docs_commit"], MATHLIB_SHA)  # not other/repo's sha
        self.assertEqual(pins["tauceti_decls"]["docs_commit"], TAUCETI_SHA)
        self.assertEqual(pins["atlas"]["sha256"], sha256_bytes(b'{"meta":{}}'))
        self.assertIn("https://taucetiproject.github.io/TauCeti/docs/TauCeti/Foo.html", op.seen)
        self.assertTrue(os.path.exists(os.path.join(self.inputs, "trough", "data", "tauceti", "deps.json")))

    def test_refetch_follows_the_default_branch(self):
        F.fetch(self.inputs, opener=FakeOpener(self.repos))
        remote = self.repos[URLS["TauCetiRoadmap"]]
        old = git(remote, "rev-parse", "HEAD").decode().strip()
        env = {"PATH": os.environ.get("PATH", ""), "HOME": self.tmp, "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_AUTHOR_NAME": "f", "GIT_AUTHOR_EMAIL": "f@example.invalid", "GIT_COMMITTER_NAME": "f",
               "GIT_COMMITTER_EMAIL": "f@example.invalid", "GIT_AUTHOR_DATE": "2026-01-02T00:00:00Z",
               "GIT_COMMITTER_DATE": "2026-01-02T00:00:00Z"}
        for args in (["branch", "aaa-side", old],  # sorts before main
                     ["-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", "next"]):
            subprocess.run(["git", "-C", remote, *args], check=True, env=env)
        new = git(remote, "rev-parse", "HEAD").decode().strip()
        self.assertNotEqual(old, new)
        F.fetch(self.inputs, opener=FakeOpener(self.repos))  # a re-fetch: HEAD is detached now
        self.assertEqual(self.pins_json()["TauCetiRoadmap"]["commit"], new)

    def test_pinned_fetch_matches(self):
        F.fetch(self.inputs, opener=FakeOpener(self.repos))
        saved = os.path.join(self.tmp, "PINS.json")
        shutil.copy(os.path.join(self.inputs, "PINS.json"), saved)
        again = os.path.join(self.tmp, "again")
        F.fetch(again, pins_file=saved, opener=FakeOpener(self.repos))
        self.inputs = again
        self.assertEqual(self.pins_json()["atlas"]["sha256"], sha256_bytes(b'{"meta":{}}'))

    def test_sha_mismatch_names_input_and_leaves_no_pins(self):
        F.fetch(self.inputs, opener=FakeOpener(self.repos))
        saved = os.path.join(self.tmp, "PINS.json")
        shutil.copy(os.path.join(self.inputs, "PINS.json"), saved)
        with self.assertRaisesRegex(F.FetchError, "^atlas: sha256"):
            F.fetch(self.inputs, pins_file=saved, opener=FakeOpener(self.repos, atlas=b'{"meta":{"x":1}}'))
        self.assertFalse(os.path.exists(os.path.join(self.inputs, "PINS.json")))

    def test_pins_txt_with_unknown_commit_fails(self):
        txt = os.path.join(self.tmp, "PINS.txt")
        with open(txt, "w") as f:
            f.write(f"fetched_at 2026-11-18T00:00:00Z\nTauCetiRoadmap {'0' * 40}\ntrough {'0' * 40}\n")
        with self.assertRaisesRegex(F.FetchError, "^TauCetiRoadmap: "):
            F.fetch(self.inputs, pins_file=txt, opener=FakeOpener(self.repos))
        self.assertFalse(os.path.exists(os.path.join(self.inputs, "PINS.json")))

    def test_cli_exits_nonzero_on_mismatch(self):
        from scripts.tauceti_plans import __main__ as M
        txt = os.path.join(self.tmp, "PINS.txt")
        with open(txt, "w") as f:
            f.write(f"TauCetiRoadmap {'0' * 40}\ntrough {'0' * 40}\n")
        with mock.patch.object(F, "Opener", lambda: FakeOpener(self.repos)), \
                mock.patch("sys.stderr"):
            self.assertEqual(M.main(["fetch", "--inputs", self.inputs, "--pins", txt]), 1)


if __name__ == "__main__":
    unittest.main()
