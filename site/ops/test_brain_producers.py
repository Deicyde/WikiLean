#!/usr/bin/env python3
"""Offline tests of site/ops/brain_producers.py: run, stage and check.

`run` drives a fake producer module (its `fetch` writes PINS.json, its `build` copies a real
plans export the producer built from its fixtures), inside a throwaway git checkout, so no
test touches the network or the repository.

Run: python3 site/ops/test_brain_producers.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

import brain_producers as bp  # noqa: E402
from scripts.tauceti_plans import build as producer  # noqa: E402
from scripts.tauceti_plans.test_build import make_inputs, write_pins  # noqa: E402


def setUpModule() -> None:
    global TMP, EXPORT_A, EXPORT_B
    TMP = Path(tempfile.mkdtemp(prefix="brain-producers-test-"))
    EXPORT_A = TMP / "export-a"
    producer.build(make_inputs(str(TMP / "a")), str(EXPORT_A))
    # A second, different export: the same inputs with one roadmap's title edited.
    inputs_b = Path(make_inputs(str(TMP / "b")))
    readme = inputs_b / "TauCetiRoadmap" / "TauCetiRoadmap" / "Beta" / "README.md"
    readme.write_text(readme.read_text().replace("# Roadmap: Beta", "# Roadmap: Beta, revised", 1))
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(TMP), "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "f", "GIT_AUTHOR_EMAIL": "f@example.invalid",
           "GIT_COMMITTER_NAME": "f", "GIT_COMMITTER_EMAIL": "f@example.invalid"}
    subprocess.run(["git", "-C", str(inputs_b / "TauCetiRoadmap"), "-c", "commit.gpgsign=false",
                    "commit", "-qam", "edit"], check=True, env=env)
    write_pins(str(inputs_b), "2026-11-18T00:00:00Z")
    EXPORT_B = TMP / "export-b"
    producer.build(str(inputs_b), str(EXPORT_B))


def tearDownModule() -> None:
    for dirpath, dirs, files in os.walk(TMP):
        os.chmod(dirpath, 0o755)
    shutil.rmtree(TMP, ignore_errors=True)


def root_of(export: Path) -> str:
    return json.loads((export / "manifest.json").read_text())["logical_root"]


FAKE_MAIN = textwrap.dedent('''
    import json, os, shutil, sys
    cmd, args = sys.argv[1], sys.argv[2:]
    inputs = args[args.index("--inputs") + 1]
    if cmd == "fetch":
        if os.environ.get("FAKE_FETCH_FAILS"):
            sys.exit(3)
        os.makedirs(inputs, exist_ok=True)
        with open(os.path.join(inputs, "PINS.json"), "w") as f:
            json.dump({"inputs": {}}, f)
        print("{}")
    elif cmd == "build":
        out = args[args.index("--out") + 1]
        shutil.copytree(os.environ["FAKE_EXPORT"], out)
        print("{}")
''')


class Harness(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(dir=TMP))
        self.repo = self.tmp / "repo"
        (self.repo / "fakeprod").mkdir(parents=True)
        (self.repo / "fakeprod" / "__init__.py").write_text("")
        (self.repo / "fakeprod" / "__main__.py").write_text(FAKE_MAIN)
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(self.tmp),
               "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "f",
               "GIT_AUTHOR_EMAIL": "f@example.invalid", "GIT_COMMITTER_NAME": "f",
               "GIT_COMMITTER_EMAIL": "f@example.invalid"}
        for args in (["init", "-q"], ["add", "."],
                     ["-c", "commit.gpgsign=false", "commit", "-qm", "fake"]):
            subprocess.run(["git", "-C", str(self.repo), *args], check=True, env=env)
        self.store = self.tmp / "store"   # TMP is outside any git work tree
        spec = {**bp.PRODUCERS["tauceti_plans"], "module": "fakeprod", "code": ("fakeprod",)}
        self.patch = mock.patch.dict(bp.PRODUCERS, {"tauceti_plans": spec})
        self.patch.start()
        self.exports = self.store / "exports" / "tauceti_plans"

    def tearDown(self) -> None:
        self.patch.stop()

    def run_producer(self, export: Path, *, keep: int = 8, fail_fetch: bool = False) -> int:
        env = {"FAKE_EXPORT": str(export)}
        if fail_fetch:
            env["FAKE_FETCH_FAILS"] = "1"
        with mock.patch.dict(os.environ, env), redirect_stdout(StringIO()), \
                redirect_stderr(StringIO()):
            return bp.run("tauceti_plans", repo=self.repo, store=self.store,
                          python=sys.executable, keep=keep)

    def record(self, name: str) -> dict:
        return json.loads((self.exports / name).read_text())


@unittest.skipUnless(sys.version_info[:2] == (3, 12), "run requires a CPython 3.12 producer")
class Run(Harness):
    def test_a_run_publishes_a_sealed_export_and_points_current_at_it(self) -> None:
        self.assertEqual(self.run_producer(EXPORT_A), 0)
        root = root_of(EXPORT_A)
        self.assertEqual(os.readlink(self.exports / "current"), root)
        published = self.exports / root
        self.assertTrue((published / "PRODUCER.json").exists())
        self.assertTrue((published / "PINS.json").exists())
        meta = json.loads((published / "PRODUCER.json").read_text())
        self.assertEqual(meta["logical_root"], root)
        self.assertEqual(len(meta["code_commit"]), 40)
        self.assertFalse(os.access(published / "manifest.json", os.W_OK))
        success = self.record("last_success.json")
        self.assertEqual((success["root"], success["published"]), (root, True))

    def test_the_same_root_twice_publishes_nothing_new(self) -> None:
        self.run_producer(EXPORT_A)
        self.assertEqual(self.run_producer(EXPORT_A), 0)
        self.assertIs(self.record("last_success.json")["published"], False)

    def test_a_failed_fetch_is_recorded_and_keeps_current(self) -> None:
        self.run_producer(EXPORT_A)
        self.assertEqual(self.run_producer(EXPORT_B, fail_fetch=True), 1)
        failure = self.record("last_failure.json")
        self.assertEqual(failure["stage"], "fetch")
        self.assertEqual(os.readlink(self.exports / "current"), root_of(EXPORT_A))

    def test_producer_code_that_differs_from_head_is_refused(self) -> None:
        (self.repo / "fakeprod" / "__init__.py").write_text("# edited\n")
        self.assertEqual(self.run_producer(EXPORT_A), 1)
        self.assertEqual(self.record("last_failure.json")["stage"], "checkout")

    def test_a_store_inside_a_git_work_tree_is_refused(self) -> None:
        self.store = self.repo / "store"
        self.assertEqual(self.run_producer(EXPORT_A), 2)

    def test_retention_keeps_current_and_pinned_roots(self) -> None:
        self.run_producer(EXPORT_A)
        self.run_producer(EXPORT_B, keep=1)
        self.assertFalse((self.exports / root_of(EXPORT_A)).exists())
        self.run_producer(EXPORT_A)
        (self.exports / "PINNED").write_text(root_of(EXPORT_B) + "\n")
        self.run_producer(EXPORT_A, keep=1)
        self.assertTrue((self.exports / root_of(EXPORT_B)).exists())

    def test_an_export_that_does_not_verify_is_never_published(self) -> None:
        broken = self.tmp / "broken"
        shutil.copytree(EXPORT_A, broken)
        (broken / "layers.jsonl").write_text("{}\n")
        self.assertEqual(self.run_producer(broken), 1)
        self.assertEqual(self.record("last_failure.json")["stage"], "verify")
        self.assertFalse((self.exports / "current").exists())


class Stage(Harness):
    def publish(self, export: Path) -> None:
        """What a successful `run` leaves behind, without running one."""
        root = root_of(export)
        target = self.exports / root
        shutil.copytree(export, target)
        (target / "PRODUCER.json").write_text("{}")
        (target / "PINS.json").write_text("{}")
        bp._seal(target)
        link = self.exports / "current"
        link.unlink(missing_ok=True)
        os.symlink(root, link)

    def stage(self, into: Path) -> int:
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            return bp.stage_export("tauceti_plans", store=self.store, into=into)

    def test_nothing_published_keeps_whatever_is_staged(self) -> None:
        into = self.tmp / "data" / "tauceti_plans"
        self.assertEqual(self.stage(into), 0)
        self.assertFalse(into.exists())

    def test_stage_copies_exactly_the_export_files_and_then_swaps(self) -> None:
        into = self.tmp / "data" / "tauceti_plans"
        self.publish(EXPORT_A)
        self.assertEqual(self.stage(into), 0)
        self.assertEqual(sorted(p.name for p in into.iterdir()),
                         sorted(bp.PRODUCERS["tauceti_plans"]["files"]))
        self.assertEqual(bp.plans_layer.load_export(into).logical_root, root_of(EXPORT_A))
        mtime = into.stat().st_mtime_ns
        self.assertEqual(self.stage(into), 0)          # same root: untouched
        self.assertEqual(into.stat().st_mtime_ns, mtime)
        self.publish(EXPORT_B)
        self.assertEqual(self.stage(into), 0)
        self.assertEqual(bp.plans_layer.load_export(into).logical_root, root_of(EXPORT_B))
        leftovers = [p.name for p in into.parent.iterdir() if p.name != "tauceti_plans"]
        self.assertEqual(leftovers, [])

    def test_a_published_export_that_does_not_verify_leaves_the_staged_one(self) -> None:
        into = self.tmp / "data" / "tauceti_plans"
        self.publish(EXPORT_A)
        self.stage(into)
        self.publish(EXPORT_B)
        target = self.exports / root_of(EXPORT_B)
        os.chmod(target, 0o755)
        os.chmod(target / "edges.jsonl", 0o644)
        (target / "edges.jsonl").write_text("{}\n")
        self.assertEqual(self.stage(into), 1)
        self.assertEqual(bp.plans_layer.load_export(into).logical_root, root_of(EXPORT_A))

    def resealed(self, name: str, edit) -> Path:
        """A copy of EXPORT_A whose hashes and root are right but whose rows `edit` broke."""
        d = self.tmp / name
        shutil.copytree(EXPORT_A, d)
        rows = [json.loads(x) for x in (d / "xrefs.jsonl").read_text().splitlines()]
        (d / "xrefs.jsonl").write_bytes(bp.plans_layer.jsonl_bytes(edit(rows)))
        manifest = json.loads((d / "manifest.json").read_text())
        data = (d / "xrefs.jsonl").read_bytes()
        manifest["files"]["xrefs.jsonl"] = {"bytes": len(data), "rows": data.count(b"\n"),
                                            "sha256": bp.plans_layer.hashlib.sha256(data).hexdigest()}
        manifest["logical_root"] = bp.plans_layer.logical_root(manifest)
        (d / "manifest.json").write_text(json.dumps(manifest))
        return d

    def test_an_export_the_build_would_refuse_is_never_staged(self) -> None:
        into = self.tmp / "data" / "tauceti_plans"
        self.publish(EXPORT_A)
        self.stage(into)
        bad = self.resealed("bad-system", lambda rows: [{**rows[0], "system": "zbmath"},
                                                        *rows[1:]])
        self.publish(bad)
        self.assertEqual(self.stage(into), 1)
        self.assertEqual(bp.plans_layer.load_export(into).logical_root, root_of(EXPORT_A))

    def test_an_invalid_staged_export_is_moved_aside_not_left_for_the_build(self) -> None:
        into = self.tmp / "data" / "tauceti_plans"
        shutil.copytree(EXPORT_A, into)
        (into / "stray.txt").write_text("x")
        self.assertEqual(self.stage(into), 1)        # no export published to replace it
        self.assertFalse(into.exists())
        aside = [p.name for p in into.parent.iterdir()]
        self.assertEqual(len(aside), 1)
        self.assertTrue(aside[0].startswith(".tauceti_plans.invalid-"))

    def test_leftovers_of_an_interrupted_stage_are_cleaned_up_and_a_hidden_file_kept(self) -> None:
        into = self.tmp / "data" / "tauceti_plans"
        self.publish(EXPORT_A)
        self.stage(into)
        (into / ".DS_Store").write_bytes(b"\0")
        for name in (".tauceti_plans.staging-x", ".tauceti_plans.old-y"):
            (into.parent / name).mkdir()
        self.assertEqual(self.stage(into), 0)
        self.assertEqual(sorted(p.name for p in into.parent.iterdir()), ["tauceti_plans"])
        self.assertTrue((into / ".DS_Store").exists())


class Check(Harness):
    def write(self, name: str, utc: str, **extra) -> None:
        self.exports.mkdir(parents=True, exist_ok=True)
        (self.exports / name).write_text(json.dumps({"utc": utc, **extra}))

    def check(self, hours: float, now: str) -> int:
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            return bp.check("tauceti_plans", store=self.store, max_age_hours=hours,
                            now=bp._epoch(now))

    def test_never_succeeded_is_stale(self) -> None:
        self.assertEqual(self.check(504, "2026-11-20T00:00:00Z"), 1)

    def test_a_recent_success_is_fresh_and_an_old_one_is_stale(self) -> None:
        self.write("last_success.json", "2026-11-19T00:00:00Z", root="r")
        self.assertEqual(self.check(504, "2026-11-20T00:00:00Z"), 0)
        self.assertEqual(self.check(12, "2026-11-20T00:00:00Z"), 1)

    def test_a_failure_after_the_last_success_is_failing(self) -> None:
        self.write("last_success.json", "2026-11-19T00:00:00Z", root="r")
        self.write("last_failure.json", "2026-11-19T06:00:00Z", stage="fetch")
        self.assertEqual(self.check(504, "2026-11-20T00:00:00Z"), 1)


if __name__ == "__main__":
    unittest.main()
