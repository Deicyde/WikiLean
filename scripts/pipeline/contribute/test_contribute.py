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
* **what never travels**: article text in a package or in a result, by name.

What only a real Mac can prove is docs/PIPELINE.md section 9, and nothing here claims
otherwise: these tests cover the transport, not Claude Code's own sandbox.

Run: python3 scripts/pipeline/contribute/test_contribute.py
"""
from __future__ import annotations

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

HERE = Path(__file__).resolve().parent
PIPELINE = HERE.parent
ROOT = PIPELINE.parents[1]
for _path in (str(HERE), str(PIPELINE)):
    sys.path.insert(0, _path)

import collect_results  # noqa: E402
import package as package_mod  # noqa: E402
import publish_package  # noqa: E402
import results as results_mod  # noqa: E402
import run_package  # noqa: E402
import sandbox  # noqa: E402
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
            self.assertEqual(argv[argv.index("--settings") + 1], str(written))
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
