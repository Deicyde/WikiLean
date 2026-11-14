#!/usr/bin/env python3
"""Tests for contribute mode: the work-repo transport, the claim, the sandbox, the result.

New code. Everything runs against local git repositories in a temporary directory — a
bare "remote", a coordinator's clone and two contributors' clones — so the whole
transport is exercised without a network and without a Mac:

* **publish → claim → results → collect**, end to end, with a sealed record set that
  ``scripts/articles/verify_submission.py`` accepts on both sides;
* **the claim no two machines can share**: a second machine's claim on the same package
  is refused, and it is refused for the right reason — the test also pushes the first
  machine's own commit a second time, which git answers "Everything up-to-date" and exit
  0, so the read-back and not the lease is what settles it;
* **the per-call sandbox**, including the macOS path fix: ``settings_json`` names every
  root by its real path, which is checked over a symlinked directory standing in for
  ``/var`` → ``/private/var``, and ``bridge.sh`` writes one settings file per call from
  the template, by ``pwd -P``, and exits 78 rather than run an agent unsandboxed;
* **what never travels**: a rendered view in a package, or article text in a result,
  by name; and **what does**, since 2026-11-13: a source's fetched bytes, its candidates
  and the inventory run, each checked against the plan's pins on both machines, and the
  envelope that carries a package through the claims API.

What only a real Mac can prove is docs/PIPELINE.md section 9, and nothing here claims
otherwise: these tests cover the transport, not Claude Code's own sandbox.

Run: python3 scripts/pipeline/contribute/test_contribute.py
"""
from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
PIPELINE = HERE.parent
ROOT = PIPELINE.parents[1]
for _path in (str(HERE), str(PIPELINE)):
    sys.path.insert(0, _path)

import collect_results  # noqa: E402
import envelope  # noqa: E402
import package as package_mod  # noqa: E402
import publish_package  # noqa: E402
import results as results_mod  # noqa: E402
import run_package  # noqa: E402
import sandbox  # noqa: E402
import seal as seal_mod  # noqa: E402
import tally as tally_mod  # noqa: E402
import work as work_mod  # noqa: E402
import workdir as workdir_mod  # noqa: E402
import workrepo  # noqa: E402

FIXTURE_SET = PIPELINE / "fixtures" / "recordset"
BRIDGE = HERE / "bridge.sh"
RECEIVED_AT = "2026-11-06T00:00:00Z"
GIT_IDENTITY = {"GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
                "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
                "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}


def quiet(call, *args, **kwargs):
    """Run a command, keeping its chatter out of the test output."""
    with io.StringIO() as sink, redirect_stdout(sink):
        return call(*args, **kwargs)


class WorkRepoCase(unittest.TestCase):
    """A bare remote, a coordinator's clone and two contributors', in a temporary directory."""

    def setUp(self):
        self.environ = dict(os.environ)
        os.environ.update(GIT_IDENTITY)
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(self._cleanup)
        self.remote = self.tmp / "work.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.remote)],
                       check=True, capture_output=True)
        self.coordinator = self._clone("coordinator")
        # a first commit, so the clone has a branch the packages live on
        (self.coordinator / "README.md").write_text("the work repo\n", encoding="utf-8")
        workrepo.commit_paths(self.coordinator, ["README.md"], "work repo")
        workrepo.push(self.coordinator, "main:main")

    def _cleanup(self):
        os.environ.clear()
        os.environ.update(self.environ)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _clone(self, name: str) -> Path:
        path = self.tmp / name
        subprocess.run(["git", "clone", "-q", str(self.remote), str(path)],
                       check=True, capture_output=True)
        return path

    def contributor(self, name: str) -> Path:
        clone = self._clone(name)
        workrepo.git(clone, "fetch", "-q", "origin")
        workrepo.git(clone, "checkout", "-q", "-B", "main", "origin/main")
        return clone

    # -- a plan to publish ------------------------------------------------------------

    def plan(self, run_id: str = "loc-7") -> Path:
        """A synthetic planned run: placeholder-rooted, and no article text in it."""
        plan = self.tmp / "plan" / run_id
        (plan / "tasks").mkdir(parents=True, exist_ok=True)
        (plan / "run.json").write_text(json.dumps(
            {"run_id": run_id, "mode": "locate",
             "root": f"{package_mod.ROOT_PLACEHOLDER}/{run_id}",
             "gate": f"{package_mod.ROOT_PLACEHOLDER}/{run_id}-gate"}, indent=1) + "\n",
            encoding="utf-8")
        (plan / "tasks" / "t1.json").write_text(
            json.dumps({"task": "t1", "passages": ["In-text #1a2b3c4d"]}, indent=1) + "\n",
            encoding="utf-8")
        return plan

    def sources_file(self) -> Path:
        path = self.tmp / "sources.jsonl"
        path.write_text((FIXTURE_SET / "sources.jsonl").read_text(encoding="utf-8"),
                        encoding="utf-8")
        return path

    def universe_file(self) -> Path:
        path = self.tmp / "universe.json"
        path.write_text((FIXTURE_SET / "universe.json").read_text(encoding="utf-8"),
                        encoding="utf-8")
        return path

    def publish(self, run_id: str = "loc-7", **over) -> dict:
        fields = {"run_id": run_id, "sources": self.sources_file(),
                  "universe": self.universe_file(), "repo": self.coordinator,
                  "max_cost": 20.0, "tasks": ["t1"]}
        fields.update(over)
        if "plan" not in fields:   # setdefault would rebuild the plan and undo the test's edit
            fields["plan"] = self.plan(run_id)
        manifest = quiet(publish_package.publish, **fields)
        workrepo.push(self.coordinator, "main:main")
        return manifest

    def documents(self, task: str = "t1") -> Path:
        out = self.tmp / "documents" / task
        out.mkdir(parents=True, exist_ok=True)
        (out / "enwiki-1001-2002.json").write_text(
            json.dumps({"task": task, "passages": []}, indent=1) + "\n", encoding="utf-8")
        return self.tmp / "documents"


class PublishTests(WorkRepoCase):
    def test_a_package_is_written_committed_and_readable(self):
        manifest = self.publish()
        directory = package_mod.package_dir(self.coordinator, "loc-7")
        self.assertTrue((directory / package_mod.PACKAGE_JSON).is_file())
        self.assertEqual(manifest["mode"], "locate")
        self.assertEqual(len(manifest["sources"]), 2)
        self.assertEqual(package_mod.read(directory)["package"], "loc-7")
        self.assertEqual(package_mod.available(self.coordinator), ["loc-7"])

    def test_the_package_pins_each_source_and_carries_no_bytes(self):
        manifest = self.publish()
        for pin in manifest["sources"]:
            self.assertTrue(pin["html_sha256"] and pin["layer_sha256"])
        directory = package_mod.package_dir(self.coordinator, "loc-7")
        self.assertEqual(package_mod.refused_names(list(directory.rglob("*"))), [])

    def test_a_plan_carrying_article_text_is_refused(self):
        plan = self.plan("loc-8")
        (plan / "layer.txt").write_text("the article", encoding="utf-8")
        with self.assertRaises(package_mod.PackageProblem) as caught:
            self.publish("loc-8", plan=plan)
        self.assertIn("article text", str(caught.exception))

    def test_a_plan_naming_this_machine_is_refused(self):
        plan = self.plan("loc-9")
        (plan / "run.json").write_text(json.dumps({"root": str(Path.home() / "ws")}),
                                       encoding="utf-8")
        with self.assertRaises(package_mod.PackageProblem) as caught:
            self.publish("loc-9", plan=plan)
        self.assertIn("names this machine's", str(caught.exception))

    def test_a_changed_package_file_is_refused_when_it_is_read(self):
        self.publish()
        directory = package_mod.package_dir(self.coordinator, "loc-7")
        target = directory / "run.json"
        target.write_text(target.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        with self.assertRaises(package_mod.PackageProblem) as caught:
            package_mod.read(directory)
        self.assertIn("sha256 does not match", str(caught.exception))

    def test_a_run_id_that_is_not_one_is_refused(self):
        for bad in ("", "a/b", "_draft", "x" * 65, None):
            with self.subTest(run_id=bad), self.assertRaises(package_mod.PackageProblem):
                package_mod.check_run_id(bad)


class ClaimTests(WorkRepoCase):
    """The claim no two machines can share."""

    def claim(self, clone: Path, machine: str, workdir: str, **over) -> str:
        fields = {"repo": clone, "workdir": self.tmp / workdir, "machine": machine,
                  "execute": True}
        fields.update(over)
        return quiet(run_package.claim, **fields)

    def test_one_machine_claims_and_the_next_is_refused(self):
        self.publish()
        one, two = self.contributor("mac-one"), self.contributor("mac-two")
        self.assertEqual(self.claim(one, "mac-one", "w1"), "loc-7")
        with self.assertRaises(run_package.RunProblem) as caught:
            self.claim(two, "mac-two", "w2")
        self.assertIn("claimed already", str(caught.exception))

    def test_a_named_package_another_machine_holds_is_refused(self):
        self.publish()
        one, two = self.contributor("mac-one"), self.contributor("mac-two")
        self.claim(one, "mac-one", "w1")
        with self.assertRaises(run_package.RunProblem) as caught:
            self.claim(two, "mac-two", "w2", package="loc-7")
        self.assertIn("already pushed", str(caught.exception))

    def test_the_claim_commit_is_this_machines_own(self):
        self.publish()
        one = self.contributor("mac-one")
        self.claim(one, "mac-one", "w1")
        branch = f"{workrepo.CLAIM_PREFIX}/loc-7"
        sha = workrepo.remote_sha(one, branch, fetch_first=True)
        payload = json.loads(workrepo.file_at(one, sha, workrepo.CLAIM_FILE))
        self.assertEqual(payload["machine_id"], "mac-one")
        self.assertEqual(payload["package"], "loc-7")
        self.assertEqual(len(payload["nonce"]), 32)
        held = run_package.held(self.tmp / "w1")
        self.assertEqual(held["claim_commit"], sha)

    def test_two_claims_of_one_package_are_two_commits(self):
        # The nonce is the mechanism: no two machines produce the same claim commit, and
        # a push of a commit the remote already holds is answered "Everything
        # up-to-date" with exit 0, so a shared commit would double-book the package.
        self.publish()
        one = self.contributor("mac-one")
        first = run_package.claim_payload("loc-7", "mac-one")
        second = run_package.claim_payload("loc-7", "mac-two")
        self.assertNotEqual(first["nonce"], second["nonce"])
        a = workrepo.claim_commit(one, "claim/a", json.dumps(first, sort_keys=True), "a")
        b = workrepo.claim_commit(one, "claim/b", json.dumps(second, sort_keys=True), "b")
        self.assertNotEqual(a, b)

    def test_a_re_push_of_the_same_commit_is_up_to_date_and_exit_zero(self):
        # This is the defect the nonce and the read-back exist for: git's exit status is
        # not the answer, so the command reads the ref back and compares it.
        self.publish()
        one = self.contributor("mac-one")
        self.claim(one, "mac-one", "w1")
        branch = f"{workrepo.CLAIM_PREFIX}/loc-7"
        again = workrepo.push_claim(one, branch, expect_absent=False)
        self.assertEqual(again.returncode, 0)
        self.assertIn("up-to-date", (again.stdout + again.stderr).lower())

    def test_the_claim_lease_overwrites_nothing(self):
        self.publish()
        one, two = self.contributor("mac-one"), self.contributor("mac-two")
        self.claim(one, "mac-one", "w1")
        branch = f"{workrepo.CLAIM_PREFIX}/loc-7"
        held = workrepo.remote_sha(one, branch, fetch_first=True)
        rival = workrepo.claim_commit(
            two, branch, json.dumps(run_package.claim_payload("loc-7", "mac-two")), "rival")
        refused = workrepo.push_claim(two, branch, expect_absent=True)
        self.assertNotEqual(refused.returncode, 0)
        self.assertNotEqual(rival, held)
        self.assertEqual(workrepo.remote_sha(one, branch, fetch_first=True), held)

    def test_a_take_over_commits_on_top_and_fast_forwards(self):
        self.publish()
        one, two = self.contributor("mac-one"), self.contributor("mac-two")
        self.claim(one, "mac-one", "w1")
        self.assertEqual(self.claim(two, "mac-two", "w2", package="loc-7", take_over=True),
                         "loc-7")
        branch = f"{workrepo.CLAIM_PREFIX}/loc-7"
        sha = workrepo.remote_sha(two, branch, fetch_first=True)
        payload = json.loads(workrepo.file_at(two, sha, workrepo.CLAIM_FILE))
        self.assertEqual(payload["machine_id"], "mac-two")
        # on top of the claim it took over: an ordinary fast-forward, nothing rewritten
        self.assertEqual(workrepo.git(two, "rev-list", "--count", sha).stdout.strip(), "2")

    def test_a_dry_run_claims_nothing(self):
        self.publish()
        one = self.contributor("mac-one")
        self.assertEqual(self.claim(one, "mac-one", "w1", execute=False), "loc-7")
        self.assertEqual(workrepo.remote_sha(one, f"{workrepo.CLAIM_PREFIX}/loc-7",
                                             fetch_first=True), "")
        self.assertEqual(run_package.held(self.tmp / "w1"), {})

    def test_a_second_invocation_keeps_the_package_this_workdir_holds(self):
        self.publish()
        one = self.contributor("mac-one")
        self.claim(one, "mac-one", "w1")
        self.assertEqual(self.claim(one, "mac-one", "w1"), "loc-7")

    def test_the_clone_stays_on_its_own_branch(self):
        self.publish()
        one = self.contributor("mac-one")
        self.claim(one, "mac-one", "w1")
        self.assertEqual(workrepo.current_branch(one), "main")

    def test_a_machine_id_is_a_path_and_a_branch_component(self):
        self.assertEqual(workrepo.machine_id("Jack's MacBook Pro"), "jack-s-macbook-pro")
        self.assertEqual(workrepo.machine_id("mac-1"), "mac-1")
        # "" is not "a bad id" but "none given", which falls back to the environment
        # and then to the host name; these are ids nothing can be made of.
        for bad in ("/", "..", "---", "   "):
            with self.subTest(given=bad), self.assertRaises(workrepo.WorkRepoProblem):
                workrepo.machine_id(bad)


class ResultTests(WorkRepoCase):
    def send(self, clone: Path, machine: str, workdir: str, **over) -> dict:
        fields = {"repo": clone, "workdir": self.tmp / workdir, "machine": machine,
                  "package": "loc-7", "documents": self.documents(), "execute": True,
                  "received_at": RECEIVED_AT}
        fields.update(over)
        return quiet(run_package.send_results, **fields)

    def test_a_result_travels_with_its_receipt(self):
        self.publish()
        one = self.contributor("mac-one")
        quiet(run_package.claim, repo=one, workdir=self.tmp / "w1", machine="mac-one",
              execute=True)
        sent = self.send(one, "mac-one", "w1")
        self.assertEqual(sent["documents"], 1)
        self.assertEqual(sent["receipt"]["machine_id"], "mac-one")
        self.assertEqual(sent["receipt"]["tasks"], {"merged": ["t1"], "owed": []})
        self.assertEqual(sent["receipt"]["schema"], results_mod.RESULT_SCHEMA)
        # the result is on its own branch, and the clone is back on the one it was on,
        # so the working tree no longer holds it
        self.assertEqual(workrepo.current_branch(one), "main")
        self.assertFalse(Path(sent["path"]).exists())
        workrepo.fetch(one, "+refs/heads/*:refs/remotes/origin/*")
        self.assertEqual(workrepo.branches(one, f"{workrepo.RESULT_PREFIX}/loc-7/*"),
                         [f"{workrepo.RESULT_PREFIX}/loc-7/mac-one"])

    def test_a_result_carrying_article_text_is_refused(self):
        self.publish()
        one = self.contributor("mac-one")
        quiet(run_package.claim, repo=one, workdir=self.tmp / "w1", machine="mac-one",
              execute=True)
        host = self.tmp / "host"
        host.mkdir()
        (host / "layer.txt").write_text("the article", encoding="utf-8")
        with self.assertRaises(results_mod.ResultProblem) as caught:
            self.send(one, "mac-one", "w1", host=host)
        self.assertIn("article text", str(caught.exception))

    def test_a_partial_result_names_the_tasks_it_owes(self):
        self.publish("loc-7", tasks=["t1", "t2"])
        one = self.contributor("mac-one")
        quiet(run_package.claim, repo=one, workdir=self.tmp / "w1", machine="mac-one",
              execute=True)
        sent = self.send(one, "mac-one", "w1")
        self.assertEqual(sent["receipt"]["tasks"]["owed"], ["t2"])

    def test_a_sealed_set_is_checked_against_the_intake_contract_before_it_is_pushed(self):
        self.publish()
        one = self.contributor("mac-one")
        quiet(run_package.claim, repo=one, workdir=self.tmp / "w1", machine="mac-one",
              execute=True)
        sent = self.send(one, "mac-one", "w1", record_set=FIXTURE_SET)
        submission = sent["submission"]
        self.assertTrue(submission["accepted"])
        self.assertEqual(submission["verifier"], "scripts/articles/verify_submission.py/1")
        self.assertEqual(submission["set_id"], "pipeline-fixture")
        self.assertEqual(submission["status_rule"], 3)
        self.assertEqual(submission["records"], 11)

    def test_a_broken_sealed_set_is_refused_with_the_contracts_own_reason(self):
        self.publish()
        one = self.contributor("mac-one")
        quiet(run_package.claim, repo=one, workdir=self.tmp / "w1", machine="mac-one",
              execute=True)
        broken = self.tmp / "broken-set"
        shutil.copytree(FIXTURE_SET, broken)
        target = broken / "sources.jsonl"
        target.write_bytes(target.read_bytes() + b"\n")
        with self.assertRaises(results_mod.ResultProblem) as caught:
            self.send(one, "mac-one", "w1", record_set=broken)
        self.assertIn("intake: refused", str(caught.exception))
        self.assertIn("must not be retried", str(caught.exception))

    def test_a_dry_run_pushes_nothing(self):
        self.publish()
        one = self.contributor("mac-one")
        quiet(run_package.claim, repo=one, workdir=self.tmp / "w1", machine="mac-one",
              execute=True)
        self.send(one, "mac-one", "w1", execute=False)
        workrepo.fetch(one, "+refs/heads/*:refs/remotes/origin/*")
        self.assertEqual(workrepo.branches(one, f"{workrepo.RESULT_PREFIX}/loc-7/*"), [])


class CollectTests(WorkRepoCase):
    def run_one(self, machine: str, workdir: str, *, record_set: Path | None = None) -> None:
        clone = self.contributor(machine)
        quiet(run_package.claim, repo=clone, workdir=self.tmp / workdir, machine=machine,
              package="loc-7", take_over=True, execute=True)
        quiet(run_package.send_results, repo=clone, workdir=self.tmp / workdir,
              machine=machine, package="loc-7", documents=self.documents(),
              record_set=record_set, execute=True, received_at=RECEIVED_AT)

    def test_the_coordinator_collects_every_machines_result(self):
        self.publish()
        self.run_one("mac-one", "w1")
        self.run_one("mac-two", "w2", record_set=FIXTURE_SET)
        out = self.tmp / "collected"
        receipt = quiet(collect_results.collect, repo=self.coordinator, run_id="loc-7",
                        out=out, fetch=True, received_at=RECEIVED_AT)
        self.assertEqual(sorted(receipt["machines"]), ["mac-one", "mac-two"])
        self.assertTrue((out / results_mod.DOCUMENTS / "t1" /
                         "enwiki-1001-2002.json").is_file())
        self.assertTrue(receipt["machines"]["mac-two"]["submission"]["accepted"])
        self.assertTrue((out / results_mod.RECORD_SET / "mac-two" / "SEAL.json").is_file())
        # the coordinator's clone never moved
        self.assertEqual(workrepo.current_branch(self.coordinator), "main")

    def test_a_result_that_does_not_match_its_receipt_is_refused(self):
        self.publish()
        self.run_one("mac-one", "w1")
        out = self.tmp / "collected"
        quiet(collect_results.collect, repo=self.coordinator, run_id="loc-7", out=out,
              fetch=True, received_at=RECEIVED_AT)
        # Put the fetched result in the coordinator's working tree, where result_dirs
        # takes it as it stands, and change one byte of it.
        fetched = next((out / ".fetched").rglob(results_mod.RESULT_JSON)).parent
        tree = self.coordinator / workrepo.RESULTS / "loc-7" / "mac-one"
        shutil.copytree(fetched, tree)
        target = next((tree / results_mod.DOCUMENTS).rglob("*.json"))
        target.write_text(target.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        with self.assertRaises(collect_results.CollectProblem) as caught:
            quiet(collect_results.collect, repo=self.coordinator, run_id="loc-7",
                  out=self.tmp / "again", received_at=RECEIVED_AT)
        self.assertIn("does not match its RESULT.json", str(caught.exception))

    def test_collecting_a_run_with_no_result_says_so(self):
        self.publish()
        with self.assertRaises(collect_results.CollectProblem) as caught:
            quiet(collect_results.collect, repo=self.coordinator, run_id="loc-7",
                  out=self.tmp / "collected", fetch=True)
        self.assertIn("no result for loc-7", str(caught.exception))

    def test_an_archive_member_that_leaves_the_destination_is_refused(self):
        # A tar is untrusted input even when git wrote it.
        dest = self.tmp / "dest"
        dest.mkdir()
        self.assertEqual(workrepo.safe_member(dest, "a/b.json"), (dest / "a/b.json").resolve())
        for name in ("../escape.json", "a/../../escape.json"):
            with self.subTest(name=name), self.assertRaises(workrepo.WorkRepoProblem):
                workrepo.safe_member(dest, name)


class MiddleTests(WorkRepoCase):
    """Contribute mode's middle: a real planned root, a stub worker, the gate, the harvest.

    The synthetic source and documents are the gate suite's
    (``scripts/pipeline/gate/test_gate.py``) and the planner is stage 10's, so this is
    the real plan → work → gate → harvest path with a local command where the model
    would be. No model is called.
    """

    def real_plan(self, run_id: str = "loc-7") -> tuple[Path, Path]:
        """``(plan root, the workspace it was planned from)``, with no article text in it."""
        for path in (str(PIPELINE / "run"), str(PIPELINE / "gate"),
                     str(ROOT / "site")):
            if path not in sys.path:
                sys.path.insert(0, path)
        import test_run as run_tests  # noqa: PLC0415 — the stage-10 suite's workspace

        workspace = run_tests.Workspace(self.tmp / "ws")
        plan_root = self.tmp / "plan" / run_id
        import plan as plan_mod  # noqa: PLC0415

        plan_mod.plan(run_id=run_id, mode="locate", out=plan_root,
                      inventory=workspace.corpus / run_tests.fixture.INV_RUN,
                      inventory_run=run_tests.fixture.INV_RUN,
                      universe=workspace.universe, manifest=workspace.manifest,
                      sources=workspace.sources, candidates=workspace.candidates,
                      index=workspace.index, per_task=8, render_prompts=True,
                      # what the package NAMES; the contributor fills these three
                      sources_as="../sources", candidates_as="../candidates",
                      index_as="../index/decls.jsonl")
        return plan_root, workspace

    def fill_inputs(self, root: Path, workspace) -> None:
        """What the contributor refetches and builds, into the names the plan gives them.

        The package carries none of this: the sources are article text, and the index
        is a map of a Lean library. Here they are the stage-10 suite's synthetic ones.
        """
        shutil.copytree(workspace.sources, root / "sources", dirs_exist_ok=True)
        shutil.copytree(workspace.candidates, root / "candidates", dirs_exist_ok=True)
        (root / "index").mkdir(exist_ok=True)
        shutil.copyfile(workspace.index, root / "index" / "decls.jsonl")

    def stub_worker(self, root: Path, task: str, document: dict) -> str:
        """A local command that writes ``document`` where the worker would, and nothing else."""
        import test_gate as fixture  # noqa: PLC0415

        out = root / "work" / task / "findings" / task / f"{fixture.SOURCE}.json"
        script = self.tmp / f"stub-{task}.py"
        script.write_text(
            "import json, pathlib\n"
            f"path = pathlib.Path({str(out)!r})\n"
            "path.parent.mkdir(parents=True, exist_ok=True)\n"
            f"path.write_text({json.dumps(document, sort_keys=True)!r}, encoding='utf-8')\n",
            encoding="utf-8")
        return f"{sys.executable} {script}"

    def task_document(self, root: Path, task: str, run_id: str = "loc-7") -> dict:
        """The gate suite's good document, cut down to the passages this task was handed."""
        import test_gate as fixture  # noqa: PLC0415

        document = fixture.good_locate_document()
        document["run_id"] = run_id
        document["task_id"] = task
        handed = {p["item"] for p in json.loads(
            (root / "tasks" / f"{task}.json").read_text())["documents"][0]["passages"]}
        document["passages"] = [p for p in document["passages"] if p["item"] in handed]
        return document

    def test_a_real_plan_carries_no_path_of_the_coordinators(self):
        plan_root, _workspace = self.real_plan()
        self.publish(run_id="loc-7", plan=plan_root, tasks=["t1"])
        directory = package_mod.package_dir(self.coordinator, "loc-7")
        manifest = package_mod.read(directory)
        self.assertIn("gate/verify_locate.py", manifest["files"])
        self.assertNotIn("sources/", "".join(manifest["files"]))

    def test_claim_then_a_stub_worker_then_gate_harvest_and_results(self):
        plan_root, workspace = self.real_plan()
        self.publish(run_id="loc-7", plan=plan_root, tasks=["t1"])
        clone = self.contributor("mac-one")
        workdir = self.tmp / "w1"
        package = quiet(run_package.claim, repo=clone, workdir=workdir, machine="mac-one",
                        package="loc-7", execute=True)
        self.assertEqual(package, "loc-7")

        # before the inputs are in place, the middle says which are missing, stages
        # nothing and harvests nothing: a contributor learns that from one line, not
        # from a task of model time
        done = quiet(run_package.run_stage, repo=clone, workdir=workdir, package="loc-7")
        self.assertTrue(done["missing_inputs"])
        self.assertEqual(done["staged"], {})
        self.assertEqual(done["receipt"]["actual"]["count"], 0)

        # the contributor fills what the plan names and the package cannot carry
        root = done["root"]
        self.fill_inputs(root, workspace)
        self.assertEqual(work_mod.missing_inputs(root), [])

        # a stub worker writes the task's document, through the same agent entry a model
        # call takes, after the runner has staged the task's own working directory
        task = work_mod.tasks_of(root)[0]
        command = self.stub_worker(root, task, self.task_document(root, task))
        done = quiet(run_package.run_stage, repo=clone, workdir=workdir, package="loc-7",
                     worker_command=command, reviewer=False)
        self.assertEqual(done["rejected"], [], done)
        self.assertEqual(done["passed"], [task])
        self.assertTrue(done["receipt"]["complete"], done["receipt"])
        self.assertEqual(done["receipt"]["actual"]["count"], 1)
        # the worker left an envelope, unpriced because no model was called
        self.assertEqual(done["receipt"]["cost"]["unpriced_calls"], 1)
        self.assertEqual(done["receipt"]["cost"]["usd"], 0.0)

        # and the result travels back with that corpus
        corpus = workdir / work_mod.CORPUS_DIR / "loc-7"
        result = quiet(run_package.send_results, repo=clone, workdir=workdir,
                       machine="mac-one", package="loc-7", documents=corpus,
                       execute=True, received_at=RECEIVED_AT)
        self.assertTrue(result["documents"])
        collected = quiet(collect_results.collect, repo=self.coordinator, run_id="loc-7",
                          out=self.tmp / "collected", fetch=True, received_at=RECEIVED_AT)
        self.assertEqual(sorted(collected["machines"]), ["mac-one"])

    def test_a_task_the_gate_rejects_is_not_harvested(self):
        plan_root, workspace = self.real_plan()
        self.publish(run_id="loc-7", plan=plan_root, tasks=["t1"])
        clone = self.contributor("mac-one")
        workdir = self.tmp / "w1"
        quiet(run_package.claim, repo=clone, workdir=workdir, machine="mac-one",
              package="loc-7", execute=True)
        root = work_mod.materialise(package_mod.package_dir(clone, "loc-7"), workdir)
        self.fill_inputs(root, workspace)
        task = work_mod.tasks_of(root)[0]
        document = self.task_document(root, task)
        # break rule 6: two locations with one rank
        document["passages"][1]["locations"][1]["rank"] = 1
        command = self.stub_worker(root, task, document)
        records = quiet(work_mod.run_tasks, root, worker_command=command,
                        reviewer=False, max_attempts=1)
        self.assertEqual([r["task_id"] for r in records], [task])
        self.assertFalse(records[0]["gate"]["ok"])
        self.assertEqual(records[0]["promoted"], [])
        passed = [r["task_id"] for r in records if r["gate"]["ok"]]
        self.assertEqual(passed, [])
        receipt = quiet(work_mod.harvest, root, workdir, "loc-7", passed)
        self.assertEqual(receipt["actual"]["count"], 0)


class StagingTests(MiddleTests):
    """The contributor stages its **own** working directories, after refetching.

    A package deliberately leaves the coordinator's staged directories behind — they
    hold its copy of the article text — so the contributor's side has to build them
    from the pages it refetched. This is that step, and the free check over it.
    """

    def ready(self, run_id: str = "loc-7"):
        """``(clone, workdir, root)``: a claimed package with every input in place."""
        plan_root, workspace = self.real_plan(run_id)
        self.publish(run_id=run_id, plan=plan_root, tasks=["t1"])
        clone = self.contributor("mac-one")
        workdir = self.tmp / "w1"
        quiet(run_package.claim, repo=clone, workdir=workdir, machine="mac-one",
              package=run_id, execute=True)
        root = work_mod.materialise(package_mod.package_dir(clone, run_id), workdir)
        self.fill_inputs(root, workspace)
        return clone, workdir, root

    def test_the_package_carries_no_staged_working_directory(self):
        plan_root, _workspace = self.real_plan()
        # the coordinator's plan staged its own, out of its own rendered sources
        self.assertTrue(any((plan_root / "work").rglob("STAGED.json")))
        manifest = self.publish(run_id="loc-7", plan=plan_root, tasks=["t1"])
        self.assertEqual([rel for rel in manifest["files"] if rel.startswith("work/")],
                         [])

    def test_staging_builds_each_task_s_directory_from_this_machine_s_files(self):
        _clone, _workdir, root = self.ready()
        staged = quiet(work_mod.stage_tasks, root)
        self.assertTrue(staged)
        for task, record in staged.items():
            work = root / "work" / task
            self.assertTrue((work / workdir_mod.STAGED_NAME).is_file())
            self.assertTrue((work / "task.json").is_file())
            for rel in record["files"]:
                self.assertTrue((work / rel).is_file(), rel)
            # every rendered view the prompts name, by name and never globbed
            for sid in record["sources"]:
                for name in workdir_mod.WORKER_VIEWS:
                    self.assertTrue((work / "sources" / sid / name).is_file(), name)
            # and the one directory the worker may write in
            self.assertTrue(workdir_mod.findings_dir(root, task).is_dir())

    def test_a_staged_file_is_this_machine_s_own_bytes(self):
        _clone, _workdir, root = self.ready()
        staged = quiet(work_mod.stage_tasks, root)
        task, record = next(iter(staged.items()))
        sid = record["sources"][0]
        self.assertEqual((root / "work" / task / "sources" / sid / "layer.txt")
                         .read_text(encoding="utf-8"),
                         (root / "sources" / sid / "layer.txt").read_text(encoding="utf-8"))

    def test_the_free_check_says_every_path_the_prompts_name_is_staged(self):
        _clone, _workdir, root = self.ready()
        quiet(work_mod.stage_tasks, root)
        got = quiet(work_mod.check_prompts, root)
        self.assertTrue(got)
        for task, answer in got.items():
            self.assertTrue(answer["ok"], f"{task}: {answer['missing']}")
            self.assertTrue(answer["present"])

    def test_the_free_check_names_a_path_staging_did_not_put_there(self):
        _clone, _workdir, root = self.ready()
        quiet(work_mod.stage_tasks, root)
        task = work_mod.tasks_of(root)[0]
        (root / "prompts" / "worker.md").write_text(
            "Read `sources/<source-id>/nowhere.json` and write your document.\n",
            encoding="utf-8")
        got = quiet(work_mod.check_prompts, root, tasks=[task])
        self.assertFalse(got[task]["ok"])
        self.assertTrue(any(name.endswith("nowhere.json")
                            for name in got[task]["missing"]))

    def test_a_missing_input_stages_nothing_and_runs_nothing(self):
        plan_root, _workspace = self.real_plan()
        self.publish(run_id="loc-7", plan=plan_root, tasks=["t1"])
        clone = self.contributor("mac-one")
        workdir = self.tmp / "w1"
        quiet(run_package.claim, repo=clone, workdir=workdir, machine="mac-one",
              package="loc-7", execute=True)
        done = quiet(run_package.run_stage, repo=clone, workdir=workdir, package="loc-7")
        self.assertTrue(done["missing_inputs"])
        self.assertEqual(done["staged"], {})
        self.assertEqual(done["records"], [])
        self.assertEqual(done["receipt"]["actual"]["count"], 0)
        # and it says what to go and get, by name
        self.assertTrue(any("rendered source" in line for line in done["missing_inputs"]))

    def test_a_stopped_rerun_leaves_an_earlier_harvest_s_receipt_alone(self):
        """A run that harvests nothing must not write over a harvest it did not make.

        The failure this guards: a contributor finishes a paid run, then loses an
        input -- moves the workdir, clears the refetched sources -- and runs the
        command again. A harvest of no documents over a corpus that holds some writes
        a receipt saying `complete: false` and lists every document already there as
        unexpected, and `--stage seal` then refuses the corpus. The documents survive;
        the receipt that vouches for them does not, and the only way back is to pay
        for the tasks again.
        """
        clone, workdir, root = self.ready()
        task = work_mod.tasks_of(root)[0]
        command = self.stub_worker(root, task, self.task_document(root, task))
        done = quiet(run_package.run_stage, repo=clone, workdir=workdir,
                     package="loc-7", worker_command=command, reviewer=False)
        self.assertTrue(done["harvested"])
        self.assertTrue(done["receipt"]["complete"])
        receipt = workdir / work_mod.CORPUS_DIR / "loc-7" / "RECEIPT.json"
        before = receipt.read_bytes()

        # the contributor loses a refetched source and runs it again
        shutil.rmtree(root / "sources")
        again = quiet(run_package.run_stage, repo=clone, workdir=workdir,
                      package="loc-7", worker_command=command, reviewer=False)
        self.assertTrue(again["missing_inputs"])
        self.assertFalse(again["harvested"])
        self.assertEqual(receipt.read_bytes(), before)
        # and what it reports is the harvest that is really there, not an empty one
        self.assertTrue(again["receipt"]["complete"])
        self.assertEqual(again["receipt"]["actual"]["count"], 1)

    def test_a_stopped_first_run_reports_an_empty_receipt_and_writes_none(self):
        plan_root, _workspace = self.real_plan()
        self.publish(run_id="loc-7", plan=plan_root, tasks=["t1"])
        clone = self.contributor("mac-one")
        workdir = self.tmp / "w1"
        quiet(run_package.claim, repo=clone, workdir=workdir, machine="mac-one",
              package="loc-7", execute=True)
        done = quiet(run_package.run_stage, repo=clone, workdir=workdir, package="loc-7")
        self.assertFalse(done["harvested"])
        self.assertEqual(done["receipt"]["actual"]["count"], 0)
        self.assertFalse((workdir / work_mod.CORPUS_DIR / "loc-7" / "RECEIPT.json")
                         .exists())

    def test_a_dry_run_stages_and_checks_and_calls_no_model(self):
        clone, workdir, root = self.ready()
        with io.StringIO() as sink, redirect_stdout(sink):
            done = run_package.run_stage(repo=clone, workdir=workdir, package="loc-7")
            printed = sink.getvalue()
        self.assertIn("DRY RUN", printed)
        self.assertTrue(done["staged"])
        self.assertTrue(all(answer["ok"] for answer in done["prompts"].values()))
        # every envelope says it was a dry run, and nothing was promoted
        envelopes = sorted((root / "usage").rglob("*.json"))
        self.assertTrue(envelopes)
        for path in envelopes:
            self.assertTrue(json.loads(path.read_text())["dry_run"])
        self.assertEqual(done["passed"], [])
        self.assertFalse(any((root / "findings").rglob("*.json")))


#: A body the frozen renderer turns into a source a contributor can stage: the shipped
#: package's raw ``action=parse`` response carries it.
RAW_BODY = ('<div class="mw-parser-output"><p>A <b>sprocket</b> is a gadget that turns a '
            'widget.</p><p>Every sprocket is finite, and every finite gadget is a '
            'sprocket.</p></div>')


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ShippedCase(WorkRepoCase):
    """A package that ships its sources' fetched bytes, their candidates and the inventory.

    Jack's ruling of 2026-11-13: the bytes travel, because a refetch of an old revision
    can render another layer than the pinned one. The publisher renders each source and
    holds it to the plan's pins before shipping it; the contributor renders it again and
    makes the gate's comparisons before it stages anything.
    """

    def shipped_plan(self, run_id: str = "loc-7"):
        """``(plan root, workspace, sources.jsonl, raw)``: a plan over a real rendering."""
        for path in (str(PIPELINE / "run"), str(PIPELINE / "gate"), str(ROOT / "site")):
            if path not in sys.path:
                sys.path.insert(0, path)
        import plan as plan_mod  # noqa: PLC0415
        import render_web  # noqa: PLC0415
        import test_run as run_tests  # noqa: PLC0415

        workspace = run_tests.Workspace(self.tmp / "ws" / run_id)
        self.sid, self.inventory_run = run_tests.fixture.SOURCE, run_tests.fixture.INV_RUN
        raw = json.dumps({"parse": {"revid": 1000000007, "text": {"*": RAW_BODY}}},
                         sort_keys=True).encode("utf-8")
        source = workspace.sources / self.sid
        shutil.rmtree(source)
        source.mkdir()
        (source / package_mod.RAW_FILE).write_bytes(raw)
        rendering = render_web.render(render_web.canonical_body(raw))
        render_web.write_rendering(source, rendering)
        entry = json.loads(workspace.manifest.read_text(encoding="utf-8"))
        entry.update(raw_sha256=sha(raw), html_sha256=rendering.html_sha256,
                     layer_sha256=rendering.layer_sha256, renderer=render_web.RENDERER)
        workspace.manifest.write_text(json.dumps(entry) + "\n", encoding="utf-8")
        sources = self.tmp / f"sources-{run_id}.jsonl"
        sources.write_text(json.dumps(entry) + "\n", encoding="utf-8")
        (workspace.candidates / f"{self.sid}.json").write_text(json.dumps(
            {"source_id": self.sid, "source_sha": rendering.html_sha256,
             "index": {"sha256": sha(workspace.index.read_bytes())},
             "curated": [], "lexical": []}), encoding="utf-8")
        plan_root = self.tmp / "plan" / run_id
        plan_mod.plan(run_id=run_id, mode="locate", out=plan_root,
                      inventory=workspace.corpus / self.inventory_run,
                      inventory_run=self.inventory_run,
                      universe=workspace.universe, manifest=workspace.manifest,
                      sources=workspace.sources, candidates=workspace.candidates,
                      index=workspace.index, per_task=8, render_prompts=True,
                      sources_as="../sources", candidates_as="../candidates",
                      index_as="../index/decls.jsonl")
        return plan_root, workspace, sources, raw

    def shipped_fields(self, plan_root: Path, workspace, sources: Path) -> dict:
        return {"plan": plan_root, "sources": sources, "universe": workspace.universe,
                "sources_dir": workspace.sources, "candidates_dir": workspace.candidates,
                "inventory_dir": workspace.corpus / self.inventory_run}

    def publish_shipped(self, run_id: str = "loc-7"):
        plan_root, workspace, sources, raw = self.shipped_plan(run_id)
        manifest = self.publish(run_id, **self.shipped_fields(plan_root, workspace, sources))
        return manifest, workspace, raw

    def claimed(self):
        """A contributor's clone and workdir holding the shipped package, claimed."""
        manifest, workspace, raw = self.publish_shipped()
        clone = self.contributor("mac-one")
        workdir = self.tmp / "w1"
        quiet(run_package.claim, repo=clone, workdir=workdir, machine="mac-one",
              package="loc-7", execute=True)
        return clone, workdir, workspace


class ShippedTests(ShippedCase):
    def test_a_package_ships_each_sources_bytes_its_candidates_and_the_inventory(self):
        manifest, workspace, raw = self.publish_shipped()
        files = manifest["files"]
        self.assertEqual(files[package_mod.raw_path(self.sid)], sha(raw))
        self.assertIn(package_mod.candidates_path(self.sid), files)
        self.assertIn(f"inputs/inventory/{self.inventory_run}/RECEIPT.json", files)
        self.assertEqual(manifest["sources"][0]["raw_sha256"], sha(raw))
        # still no rendered view: the contributor renders the bytes
        self.assertFalse([rel for rel in files if rel.endswith("layer.txt")])
        directory = package_mod.package_dir(self.coordinator, "loc-7")
        self.assertEqual(package_mod.read(directory)["files"], files)

    def test_the_publisher_refuses_bytes_that_do_not_reproduce_the_pins(self):
        plan_root, workspace, sources, raw = self.shipped_plan()
        fields = self.shipped_fields(plan_root, workspace, sources)
        other = raw.replace(b"widget", b"gizmo")
        (workspace.sources / self.sid / package_mod.RAW_FILE).write_bytes(other)
        with self.assertRaises(package_mod.PackageProblem) as caught:
            self.publish("loc-7", **fields)
        self.assertIn("not the bytes the sourceset recorded", str(caught.exception))
        # the entry names the new bytes, and they render to another layer than the plan's
        entry = json.loads(sources.read_text(encoding="utf-8"))
        entry["raw_sha256"] = sha(other)
        sources.write_text(json.dumps(entry) + "\n", encoding="utf-8")
        with self.assertRaises(package_mod.PackageProblem) as caught:
            self.publish("loc-7", **fields)
        self.assertIn("layer_sha256 renders to", str(caught.exception))

    def test_the_publisher_refuses_candidates_of_another_rendering(self):
        plan_root, workspace, sources, _raw = self.shipped_plan()
        path = workspace.candidates / f"{self.sid}.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["source_sha"] = "0" * 64
        path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaises(package_mod.PackageProblem) as caught:
            self.publish("loc-7", **self.shipped_fields(plan_root, workspace, sources))
        self.assertIn("another rendering", str(caught.exception))

    def test_the_publisher_refuses_an_inventory_that_is_not_a_complete_gated_harvest(self):
        plan_root, workspace, sources, _raw = self.shipped_plan()
        receipt = workspace.corpus / self.inventory_run / "RECEIPT.json"
        receipt.write_text(json.dumps({"complete": False, "gate": {"ok": True}}),
                           encoding="utf-8")
        with self.assertRaises(package_mod.PackageProblem) as caught:
            self.publish("loc-7", **self.shipped_fields(plan_root, workspace, sources))
        self.assertIn("not complete", str(caught.exception))

    def test_a_package_carries_no_view_and_nothing_under_index_or_work(self):
        manifest, _workspace, _raw = self.publish_shipped()
        directory = package_mod.package_dir(self.coordinator, "loc-7")
        for rel in (f"sources/{self.sid}/layer.txt", "work/t1/stray.json",
                    "index/decls.jsonl", "candidates/other.json"):
            with self.subTest(rel=rel):
                path = directory / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"x")
                named = {**manifest, "files": {**manifest["files"], rel: sha(b"x")}}
                self.assertTrue([p for p in package_mod.check(directory, named)
                                 if p.startswith(rel)], rel)
                # and unnamed, it is found wherever it sits, work/ included
                self.assertIn(f"{rel}: not in package.json",
                              package_mod.check(directory, manifest))
                path.unlink()
        # bytes for some sources and not others is not a package
        more = copy.deepcopy(manifest)
        more["sources"].append({**more["sources"][0], "source_id": "enwiki-1-2"})
        self.assertIn("the package ships fetched bytes for 1 of its 2 source(s)",
                      package_mod.check(directory, more))

    def test_the_contributor_renders_the_bytes_and_stages_without_a_refetch(self):
        clone, workdir, workspace = self.claimed()
        done = quiet(run_package.run_stage, repo=clone, workdir=workdir, package="loc-7")
        root = done["root"]
        self.assertEqual(done["refused"], [])
        for name in workdir_mod.WORKER_VIEWS:
            self.assertTrue((root / "sources" / self.sid / name).is_file(), name)
        self.assertTrue((root / "candidates" / f"{self.sid}.json").is_file())
        # the inventory is where the seal reads it
        self.assertTrue((workdir / work_mod.CORPUS_DIR / self.inventory_run /
                         "RECEIPT.json").is_file())
        # what is left is the declaration index, which travels as a bundle of its own
        self.assertEqual(len(done["missing_inputs"]), 1, done["missing_inputs"])
        self.assertIn("declaration index", done["missing_inputs"][0])
        self.assertEqual(done["staged"], {})
        (root / "index").mkdir(exist_ok=True)
        shutil.copyfile(workspace.index, root / "index" / "decls.jsonl")
        done = quiet(run_package.run_stage, repo=clone, workdir=workdir, package="loc-7")
        self.assertEqual((done["refused"], done["missing_inputs"]), ([], []))
        self.assertTrue(done["staged"])

    def test_an_index_that_is_not_the_pinned_one_stops_the_run_before_staging(self):
        clone, workdir, _workspace = self.claimed()
        root = work_mod.materialise(package_mod.package_dir(clone, "loc-7"), workdir)
        (root / "index").mkdir(exist_ok=True)
        (root / "index" / "decls.jsonl").write_text("{}\n", encoding="utf-8")
        done = quiet(run_package.run_stage, repo=clone, workdir=workdir, package="loc-7")
        self.assertTrue([line for line in done["refused"] if "declaration index" in line],
                        done["refused"])
        self.assertEqual(done["staged"], {})
        self.assertFalse(done["harvested"])

    def test_bytes_that_do_not_render_to_the_pins_leave_no_view(self):
        clone, workdir, _workspace = self.claimed()
        directory = package_mod.package_dir(clone, "loc-7")
        root = work_mod.materialise(directory, workdir)
        manifest = copy.deepcopy(package_mod.read(directory))
        manifest["sources"][0]["layer_sha256"] = "0" * 64
        problems = work_mod.render_sources(root, manifest, log=lambda *_: None)
        self.assertTrue(problems and "layer_sha256 renders to" in problems[0], problems)
        self.assertFalse((root / "sources" / self.sid / "layer.txt").exists())

    def test_an_inventory_already_there_with_other_bytes_is_never_overwritten(self):
        clone, workdir, _workspace = self.claimed()
        mine = workdir / work_mod.CORPUS_DIR / self.inventory_run
        mine.mkdir(parents=True)
        (mine / "RECEIPT.json").write_text("{}", encoding="utf-8")
        done = quiet(run_package.run_stage, repo=clone, workdir=workdir, package="loc-7")
        self.assertTrue([line for line in done["refused"] if "move it aside" in line])
        self.assertEqual((mine / "RECEIPT.json").read_text(encoding="utf-8"), "{}")


class EnvelopeTests(ShippedCase):
    """The claims API's transport: one canonical file, checked before anything is written."""

    def envelope_of_the_package(self) -> tuple[bytes, dict]:
        manifest, _workspace, _raw = self.publish_shipped()
        data = envelope.encode(package_mod.package_dir(self.coordinator, "loc-7"))
        return data, manifest

    def test_an_envelope_round_trips_and_its_bytes_name_one_package(self):
        data, manifest = self.envelope_of_the_package()
        self.assertEqual(data, envelope.encode(package_mod.package_dir(self.coordinator,
                                                                       "loc-7")))
        run_id, files = envelope.decode(data)
        self.assertEqual(run_id, "loc-7")
        self.assertEqual(set(files), set(manifest["files"]) | {package_mod.PACKAGE_JSON})
        target, read_back = envelope.unpack(data, self.tmp / "unpacked")
        self.assertEqual(target, self.tmp / "unpacked" / "loc-7")
        self.assertEqual(read_back["files"], manifest["files"])
        for rel, blob in files.items():
            self.assertEqual((target / rel).read_bytes(), blob, rel)
        # the same envelope again finds its package in place and changes nothing
        self.assertEqual(envelope.unpack(data, self.tmp / "unpacked")[0], target)

    def test_an_envelope_is_refused_before_anything_lands(self):
        data, manifest = self.envelope_of_the_package()
        value = json.loads(data)

        def changed(edit, named=None) -> bytes:
            other = copy.deepcopy(value)
            edit(other["files"])
            if named is not None:
                carried = json.loads(base64.b64decode(other["files"]["package.json"]))
                carried["files"].update(named)
                other["files"]["package.json"] = base64.b64encode(
                    json.dumps(carried).encode("utf-8")).decode("ascii")
            return envelope.canonical(other)

        stray = base64.b64encode(b"x").decode("ascii")
        view = f"sources/{self.sid}/layer.txt"
        cases = {
            "not canonical": json.dumps(value, indent=1).encode("utf-8"),
            "another key": envelope.canonical({**value, "note": "hello"}),
            "another schema": envelope.canonical({**value, "schema": "other/1"}),
            "another run": envelope.canonical({**value, "run_id": "loc-8"}),
            "a key twice": data.replace(b'{"files":', b'{"files":{},"files":', 1),
            "outside the package": changed(lambda f: f.__setitem__("../escape", stray)),
            "carried, not named": changed(lambda f: f.__setitem__("tasks/extra.json", stray)),
            "not base64": changed(lambda f: f.__setitem__("run.json", "%%%")),
            "other bytes": changed(lambda f: f.__setitem__("run.json", stray)),
            "a rendered view": changed(lambda f: f.__setitem__(view, stray),
                                       named={view: sha(b"x")}),
        }
        packages = self.tmp / "refused"
        for name, bad in cases.items():
            with self.subTest(name), self.assertRaises(package_mod.PackageProblem):
                envelope.unpack(bad, packages)
        self.assertEqual(list(packages.iterdir()) if packages.exists() else [], [])

    def test_a_directory_holding_another_package_is_never_overwritten(self):
        data, _manifest = self.envelope_of_the_package()
        held = self.tmp / "unpacked" / "loc-7"
        held.mkdir(parents=True)
        (held / "notes.txt").write_text("mine", encoding="utf-8")
        with self.assertRaises(package_mod.PackageProblem):
            envelope.unpack(data, self.tmp / "unpacked")
        self.assertEqual((held / "notes.txt").read_text(encoding="utf-8"), "mine")


class PublishApiTests(ShippedCase):
    """``publish_package.py --envelope``, ``--register`` and ``--set-state``."""

    COMMIT = "a" * 40
    CREATED = "2026-11-14T00:00:00Z"

    def bundle(self, workspace, name: str, **override) -> Path:
        """An index bundle of the workspace's universe, holding the index the gate pins."""
        out = self.tmp / "bundle" / name
        (out / "index").mkdir(parents=True, exist_ok=True)
        data = workspace.index.read_bytes()
        (out / "index" / "decls.jsonl").write_bytes(data)
        universe = json.loads(workspace.universe.read_text(encoding="utf-8"))
        manifest = {"schema": "wikilean.pipeline.index-bundle/2",
                    "universe": {"id": universe.get("id"),
                                 "universe_sha256": override.get(
                                     "universe_sha256", universe.get("universe_sha256"))},
                    "index": {"path": "index/decls.jsonl",
                              "sha256": override.get("index_sha256", sha(data))},
                    "files": {"index/decls.jsonl": {"sha256": sha(data), "bytes": len(data)}}}
        (out / "MANIFEST.json").write_text(json.dumps(manifest, indent=1, sort_keys=True)
                                           + "\n", encoding="utf-8")
        return out

    def envelope_fields(self, run_id: str = "loc-7", bundle: dict | None = None) -> dict:
        plan_root, workspace, sources, _raw = self.shipped_plan(run_id)
        return {"run_id": run_id, "plan": plan_root, "sources": sources,
                "universe": workspace.universe, "out": self.tmp / "out" / f"{run_id}.json",
                "sources_dir": workspace.sources, "candidates_dir": workspace.candidates,
                "inventory_dir": workspace.corpus / self.inventory_run,
                "bundle_dir": self.bundle(workspace, run_id, **(bundle or {})),
                "producer_commit": self.COMMIT, "created": self.CREATED, "max_cost": 20.0}

    def test_an_envelope_names_its_bundle_and_campaign_and_registers_eleven_fields(self):
        fields = self.envelope_fields()
        manifest = publish_package.publish_envelope(**fields)
        data = fields["out"].read_bytes()
        self.assertEqual(manifest["envelope"]["sha256"], sha(data))
        # the same inputs write the same bytes, so a package's sha is a function of them
        self.assertEqual(publish_package.publish_envelope(**fields)["envelope"]["sha256"],
                         sha(data))
        self.assertEqual(manifest["seal"], {"campaign": "run:loc-7", "unchallenged": True})
        self.assertEqual(manifest["tasks"], work_mod.tasks_of(fields["plan"]))
        body = publish_package.registration(data)
        self.assertEqual(sorted(body), sorted(
            ["run_id", "envelope_sha256", "bytes", "bundle_sha256", "universe_sha256",
             "index_sha256", "campaign", "mode", "tasks", "sources", "max_cost"]))
        bundle = (fields["bundle_dir"] / "MANIFEST.json").read_bytes()
        self.assertEqual((body["bundle_sha256"], body["bytes"], body["sources"]),
                         (sha(bundle), len(data), 1))
        self.assertEqual(body["index_sha256"],
                         sha(fields["bundle_dir"].joinpath("index", "decls.jsonl")
                             .read_bytes()))
        self.assertEqual(body["tasks"], len(manifest["tasks"]))

    def test_a_bundle_of_another_universe_or_index_is_refused(self):
        for run_id, override in (("loc-8", {"universe_sha256": "e" * 64}),
                                 ("loc-9", {"index_sha256": "f" * 64})):
            with self.subTest(override=override):
                fields = self.envelope_fields(run_id, bundle=override)
                with self.assertRaises(package_mod.PackageProblem) as caught:
                    publish_package.publish_envelope(**fields)
                self.assertIn("index bundle", str(caught.exception))
                self.assertFalse(fields["out"].exists())

    def test_register_sends_the_body_as_the_pipeline_bearer_and_dry_runs_by_default(self):
        fields = self.envelope_fields()
        publish_package.publish_envelope(**fields)
        import contrib_transport  # noqa: PLC0415 — publish_package put scripts/articles on the path

        sent = []

        def fake_call(base, path, token, payload=None, **_kw):
            sent.append((base, path, token, payload))
            return {"ok": True, "package": {"run_id": payload.get("run_id")}}

        with patch.object(contrib_transport, "call", fake_call):
            dry = quiet(publish_package.register, fields["out"])
            self.assertTrue(dry["dry_run"])
            self.assertEqual(sent, [])
            os.environ.pop("PIPELINE_TOKEN", None)
            with self.assertRaises(package_mod.PackageProblem):
                publish_package.register(fields["out"], execute=True)
            os.environ["PIPELINE_TOKEN"] = "pipeline-test-token"
            publish_package.register(fields["out"], base="http://127.0.0.1:9", execute=True)
            publish_package.set_state("loc-7", "retired", base="http://127.0.0.1:9",
                                      execute=True)
        self.assertEqual([(path, token) for _base, path, token, _body in sent],
                         [("/api/packages", "pipeline-test-token"),
                          ("/api/packages/loc-7/state", "pipeline-test-token")])
        self.assertEqual(sent[0][3], publish_package.registration(fields["out"].read_bytes()))
        self.assertEqual(sent[1][3], {"state": "retired"})
        with self.assertRaises(package_mod.PackageProblem):
            publish_package.set_state("loc-7", "deleted", execute=True)

    def test_the_command_line_writes_an_envelope_and_registers_it_as_a_dry_run(self):
        fields = self.envelope_fields()
        argv = ["--run-id", "loc-7", "--plan", str(fields["plan"]),
                "--sources", str(fields["sources"]), "--universe", str(fields["universe"]),
                "--sources-dir", str(fields["sources_dir"]),
                "--candidates-dir", str(fields["candidates_dir"]),
                "--inventory-corpus", str(fields["inventory_dir"]),
                "--bundle-dir", str(fields["bundle_dir"]),
                "--producer-commit", self.COMMIT, "--created", self.CREATED,
                "--envelope", str(fields["out"])]
        with io.StringIO() as sink, redirect_stdout(sink):
            self.assertEqual(publish_package.main(argv), 0)
            printed = sink.getvalue()
        self.assertIn("their fetched bytes shipped", printed)
        self.assertIn(sha(fields["out"].read_bytes()), printed)
        with io.StringIO() as sink, redirect_stdout(sink):
            self.assertEqual(publish_package.main(["--register", str(fields["out"])]), 0)
            self.assertIn('"campaign": "run:loc-7"', sink.getvalue())
        # an envelope without a producer commit is refused: a contributor checks the
        # package's gate code against it
        self.assertEqual(quiet(publish_package.main,
                               argv[:argv.index("--producer-commit")]
                               + argv[argv.index("--created"):]), 2)


class SealTests(MiddleTests):
    """``--stage seal``: the command PIPELINE.md section 10 said was missing.

    Stage 8 and stage 6 on the contributor's machine, over the run it just finished,
    and then the intake contract over what came out — all offline, nothing posted.
    """

    def real_plan(self, run_id: str = "loc-7"):
        plan_root, workspace = super().real_plan(run_id)
        self.workspace = workspace
        return plan_root, workspace

    def publish(self, run_id: str = "loc-7", **over):
        """Publish with the stage-10 workspace's own universe and sourceset pins.

        The seal pins the universe the plan froze and the sourceset the run was planned
        against, and both come from the package, so the package has to carry the ones
        this plan was made from rather than an unrelated fixture's.
        """
        over.setdefault("universe", self.workspace.universe)
        over.setdefault("sources", self.workspace.manifest)
        return super().publish(run_id, **over)

    def finished_run(self, run_id: str = "loc-7"):
        """``(clone, workdir)`` after a stub worker's document has been gated and harvested."""
        plan_root, workspace = self.real_plan(run_id)
        self.publish(run_id=run_id, plan=plan_root, tasks=["t1"])
        clone = self.contributor("mac-one")
        workdir = self.tmp / "w1"
        quiet(run_package.claim, repo=clone, workdir=workdir, machine="mac-one",
              package=run_id, execute=True)
        root = work_mod.materialise(package_mod.package_dir(clone, run_id), workdir)
        self.fill_inputs(root, workspace)
        task = work_mod.tasks_of(root)[0]
        command = self.stub_worker(root, task, self.task_document(root, task, run_id))
        done = quiet(run_package.run_stage, repo=clone, workdir=workdir,
                     package=run_id, worker_command=command, reviewer=False)
        self.assertEqual(done["passed"], [task], done)
        # the inventory the plan was cut from: on a real machine it is the
        # contributor's own stage-9b run over the same pinned revisions, which lands in
        # the same corpus. Here it is the stage-10 suite's, copied in.
        import test_run as run_tests  # noqa: PLC0415

        shutil.copytree(workspace.corpus / run_tests.fixture.INV_RUN,
                        workdir / work_mod.CORPUS_DIR / run_tests.fixture.INV_RUN)
        self.root = root
        return clone, workdir

    def write_challenge(self, workdir: Path, root: Path, *, run_id: str = "chl-7",
                        challenged: str = "loc-7") -> str:
        """A synthetic challenge run of this contributor's own, under <workdir>/runs/.

        Nothing leaves this pipeline without a challenge value, so a set is sealed
        after stage 9d. This is the smallest honest stand-in for one: every record the
        locate run produced, upheld. It lives where a contributor's own runs live.
        """
        import keys  # noqa: PLC0415
        import test_gate as fixture  # noqa: PLC0415
        import vocab  # noqa: PLC0415

        document = self.task_document(root, work_mod.tasks_of(root)[0], challenged)
        universe = json.loads(
            (self.workspace.universe).read_text(encoding="utf-8"))
        commits = {str(lib["id"]): str(lib["commit"])
                   for lib in universe["libraries"]}
        records, verdicts = [], []
        for passage in document["passages"]:
            for location in passage.get("locations") or []:
                key = keys.location_key(fixture.SOURCE, passage["item"],
                                        location["library"],
                                        commits[location["library"]],
                                        location["declaration"])
                records.append({"key": key, "run_id": challenged,
                                "source_id": fixture.SOURCE, "item": passage["item"],
                                "target": "location", "location_key": key,
                                "planned": "required",
                                "relation": location["relation"],
                                "library": location["library"],
                                "declaration": location["declaration"]})
                verdicts.append({"item": passage["item"], "target": "location",
                                 "location_key": key, "action": "confirm",
                                 "witness": "The declaration says it.",
                                 "instantiation": location.get("instantiation")
                                 or "at the gadget of the passage"})
            if passage.get("coverage") is not None:
                records.append({
                    "key": tally_mod.record_key("coverage", fixture.SOURCE,
                                                passage["item"], None),
                    "run_id": challenged, "source_id": fixture.SOURCE,
                    "item": passage["item"], "target": "coverage",
                    "planned": "required",
                    "parts": [row["location_key"] for row in records
                              if row["item"] == passage["item"]
                              and row["target"] == "location"]})
                verdicts.append({"item": passage["item"], "target": "coverage",
                                 "action": "confirm",
                                 "witness": "Both clauses are stated."})
            if passage.get("outcome") == "absent":
                records.append({
                    "key": tally_mod.record_key("absence", fixture.SOURCE,
                                                passage["item"], None),
                    "run_id": challenged, "source_id": fixture.SOURCE,
                    "item": passage["item"], "target": "absence",
                    "planned": "required"})
                verdicts.append({
                    "item": passage["item"], "target": "absence", "action": "uphold",
                    "witness": "Nothing in the universe states it.",
                    "queries": list((passage.get("receipt") or {}).get("queries") or [])})
        runs = Path(workdir) / seal_mod.RUNS_DIR / run_id
        runs.mkdir(parents=True, exist_ok=True)
        plan = json.loads((root / "run.json").read_text(encoding="utf-8"))
        (runs / "run.json").write_text(json.dumps({
            "run_id": run_id, "mode": vocab.MODE_CHALLENGE,
            "locate": {**plan["locate"], "mode": vocab.MODE_CHALLENGE,
                       "challenged_runs": [challenged]}}, indent=1) + "\n",
            encoding="utf-8")
        (runs / "challenge-plan.json").write_text(json.dumps({
            "schema": vocab.CHALLENGE_PLAN_SCHEMA, "run_id": run_id,
            "challenged_runs": [challenged], "records": records}, indent=1) + "\n",
            encoding="utf-8")
        task = f"{run_id}-t1"
        out = Path(workdir) / work_mod.CORPUS_DIR / run_id / task
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{fixture.SOURCE}.json").write_text(json.dumps({
            "schema": vocab.DOCUMENT_SCHEMA, "mode": vocab.MODE_CHALLENGE,
            "run_id": run_id, "task_id": task, "source_id": fixture.SOURCE,
            "source_sha": document["source_sha"],
            "layer_sha256": document["layer_sha256"],
            "renderer": document["renderer"], "universe": document["universe"],
            "universe_sha256": document["universe_sha256"],
            "verdicts": verdicts}, indent=1) + "\n", encoding="utf-8")
        (Path(workdir) / work_mod.CORPUS_DIR / run_id / "RECEIPT.json").write_text(
            json.dumps({"complete": True, "gate": {"ok": True},
                        "harvested_at": "2026-01-02T03:04:05Z"}, indent=1) + "\n",
            encoding="utf-8")
        return run_id

    def seal(self, clone, workdir, **over):
        fields = {"repo": clone, "workdir": workdir, "package": "loc-7",
                  "set_id": "contrib-1", "campaign": "run:loc-7",
                  "producer_commit": "a" * 40, "created": "2026-11-06T00:00:00Z",
                  "machine": "mac-one", "received_at": RECEIVED_AT}
        fields.update(over)
        return quiet(run_package.seal_stage, **fields)

    def challenged(self, clone, workdir, **over):
        run = self.write_challenge(workdir, self.root)
        return self.seal(clone, workdir, challenge_runs=(run,), **over)

    def test_a_run_with_no_challenge_seals_nothing_and_says_why(self):
        """Nothing leaves this pipeline without a challenge value — not even here."""
        clone, workdir = self.finished_run()
        with self.assertRaises(seal_mod.SealProblem) as caught:
            self.seal(clone, workdir)
        self.assertIn("--challenge-run", str(caught.exception))
        self.assertIn("challenge value", str(caught.exception))

    def test_a_finished_run_seals_and_the_intake_contract_accepts_it(self):
        clone, workdir = self.finished_run()
        done = self.challenged(clone, workdir)
        set_dir = Path(done["set_dir"])
        self.assertTrue((set_dir / "SEAL.json").is_file())
        seal = json.loads((set_dir / "SEAL.json").read_text(encoding="utf-8"))
        self.assertEqual(seal["set_id"], "contrib-1")
        self.assertEqual(seal["campaign"], "run:loc-7")
        self.assertEqual(seal["skill_commit"], "a" * 40)
        self.assertEqual(seal["created"], "2026-11-06T00:00:00Z")
        # /2, because a /1 set carries no passage_key and intake refuses it
        self.assertEqual(seal["schema"], "cf-locate-recordset/2")
        # the intake contract ran here, on this machine, before anything was pushed
        self.assertTrue(done["submission"]["records"])
        self.assertEqual(done["submission"]["set_id"], "contrib-1")

    def test_the_seal_pins_the_universe_and_sourceset_the_plan_froze(self):
        clone, workdir = self.finished_run()
        done = self.challenged(clone, workdir)
        seal = json.loads((Path(done["set_dir"]) / "SEAL.json").read_text(encoding="utf-8"))
        run = json.loads((workdir / work_mod.ROOT_DIR / "run.json")
                         .read_text(encoding="utf-8"))
        self.assertEqual(seal["universe"]["universe_sha256"],
                         run["locate"]["universe"]["sha256"])
        self.assertEqual(seal["sourceset"]["manifest_sha256"],
                         run["locate"]["sourceset"]["manifest_sha256"])

    def test_the_sealed_set_goes_back_through_the_results_leg(self):
        clone, workdir = self.finished_run()
        done = self.challenged(clone, workdir)
        result = quiet(run_package.send_results, repo=clone, workdir=workdir,
                       machine="mac-one", package="loc-7",
                       documents=workdir / work_mod.CORPUS_DIR / "loc-7",
                       record_set=Path(done["set_dir"]), execute=True,
                       received_at=RECEIVED_AT)
        self.assertEqual(result["submission"]["set_id"], "contrib-1")

    def test_direct_submission_preview_uses_the_real_checker_without_pushing_results(self):
        clone, workdir = self.finished_run()
        done = self.challenged(clone, workdir)
        import submission_transport
        with patch.object(submission_transport, "request", side_effect=AssertionError("preview made a request")), \
             patch.object(run_package, "send_results", side_effect=AssertionError("used the old results transport")):
            code = quiet(run_package.main, ["--repo", str(clone), "--workdir", str(workdir),
                         "--machine-id", "mac-one", "--stage", "results", "--submit",
                         "--record-set", done["set_dir"]])
        self.assertEqual(code, 0)
        receipt = json.loads((workdir / "SUBMISSION.json").read_text())
        self.assertTrue(receipt["dry_run"])
        self.assertGreater(receipt["bytes"], 0)

    def test_a_bad_producer_commit_is_refused_before_anything_is_written(self):
        clone, workdir = self.finished_run()
        with self.assertRaises(seal_mod.SealProblem) as caught:
            self.seal(clone, workdir, producer_commit="HEAD")
        self.assertIn("40-hex", str(caught.exception))
        self.assertFalse((workdir / run_package.SET_DIR).exists())

    def test_a_root_that_was_never_materialised_is_refused(self):
        plan_root, _workspace = self.real_plan()
        self.publish(run_id="loc-7", plan=plan_root, tasks=["t1"])
        clone = self.contributor("mac-one")
        with self.assertRaises(run_package.RunProblem) as caught:
            self.seal(clone, self.tmp / "nothing")
        self.assertIn("--stage run", str(caught.exception))

    def test_a_stage_8_refusal_exits_two_and_not_three(self):
        """Exit 3 is "the command broke"; a corpus the tally will not read is exit 2.

        The tally, the corpus gate check, the export and the index reader all raise
        ValueError subclasses, so a catch-all would report every one of them as a
        crash and send a contributor looking for a bug in the tool.
        """
        clone, workdir = self.finished_run()
        run = self.write_challenge(workdir, self.root)
        # the inventory the plan was cut from goes missing
        import test_run as run_tests  # noqa: PLC0415

        shutil.rmtree(workdir / work_mod.CORPUS_DIR / run_tests.fixture.INV_RUN)
        code = quiet(run_package.main,
                     ["--repo", str(clone), "--workdir", str(workdir),
                      "--package", "loc-7", "--stage", "seal",
                      "--set-id", "contrib-3", "--campaign", "run:loc-7",
                      "--producer-commit", "c" * 40, "--challenge-run", run,
                      "--created", "2026-11-06T00:00:00Z"])
        self.assertEqual(code, 2)

    def test_the_command_line_refuses_a_seal_with_no_pins(self):
        clone, workdir = self.finished_run()
        code = quiet(run_package.main,
                     ["--repo", str(clone), "--workdir", str(workdir),
                      "--package", "loc-7", "--stage", "seal"])
        self.assertEqual(code, 2)

    def test_the_command_line_seals_and_writes_the_set(self):
        clone, workdir = self.finished_run()
        run = self.write_challenge(workdir, self.root)
        out = self.tmp / "my-set"
        code = quiet(run_package.main,
                     ["--repo", str(clone), "--workdir", str(workdir),
                      "--package", "loc-7", "--stage", "seal",
                      "--set-id", "contrib-2", "--campaign", "run:loc-7",
                      "--producer-commit", "b" * 40, "--challenge-run", run,
                      "--created", "2026-11-06T00:00:00Z", "--set-out", str(out)])
        self.assertEqual(code, 0)
        self.assertTrue((out / "SEAL.json").is_file())
        self.assertEqual(
            json.loads((out / "SEAL.json").read_text(encoding="utf-8"))["set_id"],
            "contrib-2")


class ResultBoundaryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.root = self.base / "result"
        self.root.mkdir()
        self.outside = self.base / "outside.json"
        self.outside.write_text("private test bytes", encoding="utf-8")
        self.sha = results_mod.keys.sha256_file(self.outside)

    def receipt(self, files):
        (self.root / results_mod.RESULT_JSON).write_text(
            json.dumps({"schema": results_mod.RESULT_SCHEMA, "files": files}),
            encoding="utf-8")

    def assert_refused_without_hashing(self):
        with patch.object(results_mod.keys, "sha256_file",
                          side_effect=AssertionError("untrusted path was read")):
            self.assertTrue(results_mod.check_receipt(self.root))

    def test_parent_path_in_a_receipt_is_refused_before_reading(self):
        self.receipt({"../outside.json": self.sha})
        self.assert_refused_without_hashing()

    def test_absolute_path_in_a_receipt_is_refused_before_reading(self):
        self.receipt({str(self.outside): self.sha})
        self.assert_refused_without_hashing()

    def test_symlink_directory_in_a_receipt_is_refused_before_reading(self):
        (self.root / "linked").symlink_to(self.base, target_is_directory=True)
        self.receipt({"linked/outside.json": self.sha})
        self.assert_refused_without_hashing()

    def test_a_symlink_receipt_is_refused(self):
        self.outside.write_text(json.dumps(
            {"schema": results_mod.RESULT_SCHEMA, "files": {}}), encoding="utf-8")
        (self.root / results_mod.RESULT_JSON).symlink_to(self.outside)
        self.assertTrue(results_mod.check_receipt(self.root))

    def test_copy_refuses_symlinks_before_copying_any_file(self):
        (self.root / "a.json").write_text("{}", encoding="utf-8")
        (self.root / "z.json").symlink_to(self.outside)
        dest = self.base / "copy"
        with self.assertRaises(results_mod.ResultProblem):
            results_mod.copy_tree(self.root, dest)
        self.assertFalse(dest.exists())

    def test_a_non_object_receipt_is_a_refusal(self):
        (self.root / results_mod.RESULT_JSON).write_text("[]", encoding="utf-8")
        self.assertTrue(results_mod.check_receipt(self.root))


    def test_a_special_file_is_refused_without_opening_it(self):
        os.mkfifo(self.root / "fifo.json")
        self.receipt({"fifo.json": "a" * 64})
        self.assert_refused_without_hashing()

    def test_malformed_file_maps_are_refused(self):
        path = self.root / results_mod.RESULT_JSON
        for files in [[], None, {"./x": self.sha}, {"a//b": self.sha},
                      {"x": 123}, {"x": "not-a-hash"},
                      {results_mod.RESULT_JSON: self.sha}]:
            with self.subTest(files=files):
                path.write_text(json.dumps(
                    {"schema": results_mod.RESULT_SCHEMA, "files": files}),
                    encoding="utf-8")
                self.assert_refused_without_hashing()


class SandboxTests(unittest.TestCase):
    """The per-call settings file, and the macOS real-path fix."""

    def test_the_settings_name_the_keys_the_cli_reads(self):
        value = sandbox.settings_json(write_roots=[sandbox.WORKTREE_PLACEHOLDER],
                                      deny_write=["/gate"])
        self.assertEqual(set(value), {"sandbox"})
        inner = value["sandbox"]
        self.assertIs(inner["enabled"], True)
        self.assertIs(inner["autoAllowBashIfSandboxed"], False)
        self.assertIs(inner["filesystem"]["disabled"], False)
        self.assertEqual(inner["filesystem"]["allowWrite"],
                         [sandbox.WORKTREE_PLACEHOLDER + "/**"])
        self.assertEqual(inner["filesystem"]["denyWrite"], ["/gate/**"])

    def test_the_placeholder_is_left_alone_and_every_other_root_is_resolved(self):
        with tempfile.TemporaryDirectory() as scratch:
            # /var -> /private/var on macOS: the sandbox matches the real path a write
            # lands on, so a root reached through a symlink must be written resolved.
            real = Path(scratch) / "private" / "var" / "folders" / "wt"
            real.mkdir(parents=True)
            link = Path(scratch) / "var"
            link.symlink_to(Path(scratch) / "private" / "var")
            through = link / "folders" / "wt"
            value = sandbox.settings_json(
                write_roots=[sandbox.WORKTREE_PLACEHOLDER, str(through)])
            self.assertEqual(value["sandbox"]["filesystem"]["allowWrite"],
                             [sandbox.WORKTREE_PLACEHOLDER + "/**",
                              os.path.realpath(str(real)) + "/**"])
            self.assertNotIn(str(link), json.dumps(value))

    def test_a_root_is_written_once(self):
        value = sandbox.settings_json(write_roots=["/a", "/a/", "/a"])
        self.assertEqual(value["sandbox"]["filesystem"]["allowWrite"], ["/a/**"])

    def test_the_staged_file_is_a_template_naming_the_worktree_by_placeholder(self):
        with tempfile.TemporaryDirectory() as scratch:
            root, entrypoint, gate = (Path(scratch) / name
                                      for name in ("root", "entrypoint", "gate"))
            for path in (root, entrypoint, gate):
                path.mkdir()
            staged = sandbox.staged_settings(root, entrypoint, gate)
            text = staged.read_text(encoding="utf-8")
            self.assertIn(sandbox.WORKTREE_PLACEHOLDER, text)
            self.assertIn(str(Path(os.path.realpath(entrypoint)) / ".git"), text)
            # the entrypoint's .git is writable because a worker commits its document
            value = json.loads(text)
            self.assertEqual(len(value["sandbox"]["filesystem"]["allowWrite"]), 2)
            self.assertEqual(len(value["sandbox"]["filesystem"]["denyWrite"]), 2)

    def test_the_call_substitution_is_the_bridges(self):
        template = json.dumps(sandbox.settings_json(
            write_roots=[sandbox.WORKTREE_PLACEHOLDER]))
        self.assertIn("/work/t1/**",
                      sandbox.call_settings(template, "/work/t1"))


class BridgeTests(unittest.TestCase):
    """bridge.sh: one settings file per call, by `pwd -P`, or exit 78."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.prompts = self.tmp / "prompts"
        self.prompts.mkdir()
        (self.prompts / "worker.md").write_text("do the task $TEND_TASK_ID\n", encoding="utf-8")
        # a `claude` that records its argv instead of calling anything
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.record = self.tmp / "argv.txt"
        (self.bin / "claude").write_text(
            "#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" > " + str(self.record) + "\n",
            encoding="utf-8")
        (self.bin / "claude").chmod(0o755)

    def call(self, worktree: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
        worktree.mkdir(parents=True, exist_ok=True)
        full = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}",
                "WIKILEAN_PROMPT_DIR": str(self.prompts), "TEND_TASK_ID": "t1", **env}
        return subprocess.run(["bash", str(BRIDGE), "worker"], cwd=str(worktree),
                              env=full, text=True, capture_output=True)

    def test_bash_3_2_can_parse_it(self):
        # macOS ships bash 3.2; `bash -n` is the check a Linux box can run.
        done = subprocess.run(["bash", "-n", str(BRIDGE)], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn("<<", BRIDGE.read_text(encoding="utf-8").split("set -euo")[1])

    def test_an_unreadable_settings_file_exits_78_and_runs_nothing(self):
        done = self.call(self.tmp / "wt",
                         {sandbox.SETTINGS_ENV: str(self.tmp / "nowhere.json")})
        self.assertEqual(done.returncode, sandbox.UNSANDBOXED_EXIT)
        self.assertIn("refusing to run the agent unsandboxed", done.stderr)
        self.assertFalse(self.record.exists())

    def test_the_template_becomes_one_settings_file_per_call(self):
        template = self.tmp / "claude-settings.json"
        sandbox.write_settings(template, write_roots=[sandbox.WORKTREE_PLACEHOLDER])
        for name in ("wt-one", "wt-two"):
            worktree = self.tmp / name
            done = self.call(worktree, {sandbox.SETTINGS_ENV: str(template)})
            self.assertEqual(done.returncode, 0, done.stderr)
            written = worktree / sandbox.CALL_SETTINGS_RELATIVE
            self.assertTrue(written.is_file())
            value = json.loads(written.read_text(encoding="utf-8"))
            self.assertEqual(value["sandbox"]["filesystem"]["allowWrite"],
                             [os.path.realpath(worktree) + "/**"])
            argv = self.record.read_text(encoding="utf-8").splitlines()
            self.assertIn("--settings", argv)
            self.assertEqual(argv[argv.index("--settings") + 1], str(written.resolve()))
        # each call's file is its own worktree's, not the other's
        one = json.loads((self.tmp / "wt-one" / sandbox.CALL_SETTINGS_RELATIVE)
                         .read_text(encoding="utf-8"))
        two = json.loads((self.tmp / "wt-two" / sandbox.CALL_SETTINGS_RELATIVE)
                         .read_text(encoding="utf-8"))
        self.assertNotEqual(one, two)

    def test_the_call_names_the_worktree_by_its_real_path(self):
        # the macOS /var -> /private/var case, as far as Linux can show it
        real = self.tmp / "private" / "var" / "wt"
        real.mkdir(parents=True)
        link = self.tmp / "var"
        link.symlink_to(self.tmp / "private" / "var")
        template = self.tmp / "claude-settings.json"
        sandbox.write_settings(template, write_roots=[sandbox.WORKTREE_PLACEHOLDER])
        done = self.call(link / "wt", {sandbox.SETTINGS_ENV: str(template)})
        self.assertEqual(done.returncode, 0, done.stderr)
        value = json.loads((real / sandbox.CALL_SETTINGS_RELATIVE)
                           .read_text(encoding="utf-8"))
        self.assertEqual(value["sandbox"]["filesystem"]["allowWrite"],
                         [os.path.realpath(real) + "/**"])
        self.assertNotIn(str(link), json.dumps(value))

    def test_a_settings_file_without_the_placeholder_is_passed_through(self):
        own = self.tmp / "operator.json"
        sandbox.write_settings(own, write_roots=["/somewhere"])
        worktree = self.tmp / "wt"
        done = self.call(worktree, {sandbox.SETTINGS_ENV: str(own)})
        self.assertEqual(done.returncode, 0, done.stderr)
        argv = self.record.read_text(encoding="utf-8").splitlines()
        self.assertEqual(argv[argv.index("--settings") + 1], str(own))
        self.assertFalse((worktree / sandbox.CALL_SETTINGS_RELATIVE).exists())

    def test_the_worker_and_the_reviewer_get_different_tools(self):
        template = self.tmp / "claude-settings.json"
        sandbox.write_settings(template, write_roots=[sandbox.WORKTREE_PLACEHOLDER])
        (self.prompts / "reviewer.md").write_text("judge it\n", encoding="utf-8")
        full = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}",
                "WIKILEAN_PROMPT_DIR": str(self.prompts),
                sandbox.SETTINGS_ENV: str(template)}
        (self.tmp / "wt").mkdir()
        for role, expected in (("worker", "Edit"), ("reviewer", "git diff")):
            done = subprocess.run(["bash", str(BRIDGE), role], cwd=str(self.tmp / "wt"),
                                  env=full, text=True, capture_output=True)
            self.assertEqual(done.returncode, 0, done.stderr)
            argv = self.record.read_text(encoding="utf-8")
            self.assertIn(expected, argv)
        self.assertNotIn("acceptEdits", self.record.read_text(encoding="utf-8"))

    def test_the_api_key_is_unset_before_the_call(self):
        # AGENTS.md's max-auth gotcha: a key in the environment makes a subscription
        # call fail silently.
        (self.bin / "claude").write_text(
            "#!/usr/bin/env bash\nprintf 'key=[%s]\\n' \"${ANTHROPIC_API_KEY:-}\" > "
            + str(self.record) + "\n", encoding="utf-8")
        (self.bin / "claude").chmod(0o755)
        done = self.call(self.tmp / "wt", {"ANTHROPIC_API_KEY": "sk-should-be-unset"})
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.record.read_text(encoding="utf-8").strip(), "key=[]")


if __name__ == "__main__":
    unittest.main(verbosity=2)
