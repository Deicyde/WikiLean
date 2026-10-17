#!/usr/bin/env python3
"""Tests for bot/ops/state-branch.sh against local bare repositories (no network)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "ops" / "state-branch.sh"
WORKFLOW = HERE.parent / ".github" / "workflows" / "wikidata-poll.yml"
MESSAGE = "bot: advance @[wikidata] poll state [skip ci]"


class StateBranchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.env = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.tmp),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "wikilean-bot",
            "GIT_AUTHOR_EMAIL": "bot@example.invalid",
            "GIT_COMMITTER_NAME": "wikilean-bot",
            "GIT_COMMITTER_EMAIL": "bot@example.invalid",
        }
        self.origin = self.tmp / "origin.git"
        self.git("init", "--quiet", "--bare", "-b", "main", str(self.origin), cwd=self.tmp)
        seed = self.tmp / "seed"
        self.git("init", "--quiet", "-b", "main", str(seed), cwd=self.tmp)
        self.write(seed, "README.md", "main\n")
        self.write(seed, "bot/.gitignore", "state/pool_candidates.json\n")
        self.write(seed, "bot/state/bot_state.json", json.dumps({"current_pr": 7}))
        self.write(seed, "bot/state/cut_log.json", "[]")
        self.write(seed, "bot/data/corrections.jsonl", '{"qid": "Q1"}\n')
        self.write(seed, "bot/data/tagged_in_master.txt", "Q1\n")
        self.git("add", "README.md", "bot", cwd=seed)
        self.git("commit", "--quiet", "-m", "main", cwd=seed)
        self.git("push", "--quiet", str(self.origin), "main", cwd=seed)
        self.main_sha = self.git("rev-parse", "main", cwd=seed)

    def git(self, *args, cwd):
        r = subprocess.run(["git", *args], cwd=cwd, env=self.env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, f"git {args}: {r.stderr}")
        return r.stdout.strip()

    def write(self, root, rel, text):
        path = Path(root) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def clone(self, name):
        path = self.tmp / name
        self.git("clone", "--quiet", "--depth=1", self.origin.as_uri(), str(path), cwd=self.tmp)
        return path

    def run_script(self, cwd, *args, bootstrap=None, extra_env=None):
        env = dict(self.env, **(extra_env or {}))
        if bootstrap is not None:
            env["BOT_STATE_BOOTSTRAP"] = bootstrap
        return subprocess.run(["bash", str(SCRIPT), *args], cwd=cwd, env=env,
                              capture_output=True, text=True, timeout=60)

    def ok(self, cwd, *args, bootstrap=None, extra_env=None):
        r = self.run_script(cwd, *args, bootstrap=bootstrap, extra_env=extra_env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout

    def has_branch(self):
        r = subprocess.run(["git", "rev-parse", "--verify", "--quiet", "refs/heads/bot-state"],
                           cwd=self.origin, env=self.env, capture_output=True, text=True)
        return r.returncode == 0

    def branch_files(self):
        out = self.git("ls-tree", "-r", "--name-only", "bot-state", cwd=self.origin)
        return sorted(out.splitlines())

    def test_bootstrap_then_round_trip(self):
        a = self.clone("a")
        self.assertIn("bootstrap_state is set", self.ok(a, "restore", bootstrap="true"))
        self.write(a, "bot/state/bot_state.json", json.dumps({"current_pr": 8}))
        self.write(a, "bot/state/pool_candidates.json", "[]")  # ignored, never persisted
        self.write(a, "bot/data/tagged_in_master.txt", "Q1\nQ2\n")
        self.write(a, "bot/data/most_used_qids.json", "{}")  # not bot-written: stays on main
        self.ok(a, "save", MESSAGE)

        self.assertEqual(self.branch_files(), ["bot/data/corrections.jsonl",
                                               "bot/data/tagged_in_master.txt",
                                               "bot/state/bot_state.json", "bot/state/cut_log.json"])
        self.assertEqual(self.git("rev-list", "--count", "bot-state", cwd=self.origin), "1")
        self.assertEqual(self.git("rev-parse", "main", cwd=self.origin), self.main_sha)
        self.assertEqual(self.git("log", "-1", "--format=%s", "bot-state", cwd=self.origin), MESSAGE)

        b = self.clone("b")
        self.ok(b, "restore")
        self.assertEqual(json.loads((b / "bot/state/bot_state.json").read_text()), {"current_pr": 8})
        self.assertEqual((b / "bot/data/tagged_in_master.txt").read_text(), "Q1\nQ2\n")
        self.assertIn("no state change", self.ok(b, "save", MESSAGE))
        self.assertEqual(self.git("rev-list", "--count", "bot-state", cwd=self.origin), "1")

        self.write(b, "bot/state/batch8_approved.json", "{}")
        (b / "bot/state/cut_log.json").unlink()
        self.ok(b, "save", MESSAGE)
        self.assertEqual(self.git("rev-list", "--count", "bot-state", cwd=self.origin), "2")
        self.assertEqual(self.branch_files(), ["bot/data/corrections.jsonl",
                                               "bot/data/tagged_in_master.txt",
                                               "bot/state/batch8_approved.json",
                                               "bot/state/bot_state.json"])

    def test_restore_mirrors_the_branch_including_deletions(self):
        a = self.clone("a")
        self.ok(a, "restore", bootstrap="true")
        self.write(a, "bot/state/batch8_approved.json", "{}")
        self.ok(a, "save", MESSAGE)
        editor = self.tmp / "editor"
        self.git("clone", "--quiet", "--branch", "bot-state", self.origin.as_uri(), str(editor), cwd=self.tmp)
        self.git("rm", "--quiet", "bot/state/cut_log.json", cwd=editor)
        self.git("commit", "--quiet", "-m", "drop cut_log by hand", cwd=editor)
        self.git("push", "--quiet", "origin", "bot-state", cwd=editor)

        b = self.clone("b")  # main still has cut_log.json and lacks batch8_approved.json
        self.ok(b, "restore")
        self.assertFalse((b / "bot/state/cut_log.json").exists())
        self.assertTrue((b / "bot/state/batch8_approved.json").exists())
        self.assertIn("no state change", self.ok(b, "save", MESSAGE))
        self.assertNotIn("bot/state/cut_log.json", self.branch_files())

    def test_a_path_the_branch_never_carried_keeps_main_s_copy(self):
        rogue = self.clone("old")
        self.git("checkout", "--quiet", "--orphan", "bot-state", cwd=rogue)
        self.git("rm", "-r", "--quiet", "--cached", ".", cwd=rogue)
        self.git("add", "bot/state", cwd=rogue)
        self.git("commit", "--quiet", "-m", "branch from before tagged lists were persisted", cwd=rogue)
        self.git("push", "--quiet", "origin", "bot-state", cwd=rogue)
        a = self.clone("a")
        self.ok(a, "restore")
        self.assertEqual((a / "bot/data/tagged_in_master.txt").read_text(), "Q1\n")
        self.ok(a, "save", MESSAGE)
        self.assertIn("bot/data/tagged_in_master.txt", self.branch_files())

    def test_restore_refuses_a_branch_without_bot_state_json(self):
        editor = self.clone("editor")
        self.git("checkout", "--quiet", "--orphan", "bot-state", cwd=editor)
        self.git("rm", "-r", "--quiet", "--cached", ".", cwd=editor)
        self.git("add", "bot/state/cut_log.json", cwd=editor)
        self.git("commit", "--quiet", "-m", "no bot_state.json", cwd=editor)
        self.git("push", "--quiet", "origin", "bot-state", cwd=editor)
        r = self.run_script(self.clone("a"), "restore")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no bot/state/bot_state.json", r.stderr)

    def test_concurrent_writer_is_rejected_not_overwritten(self):
        a = self.clone("a")
        self.ok(a, "restore", bootstrap="true")
        self.ok(a, "save", MESSAGE)
        c, d = self.clone("c"), self.clone("d")
        self.ok(c, "restore")
        self.ok(d, "restore")
        self.write(d, "bot/state/bot_state.json", json.dumps({"current_pr": 9}))
        self.ok(d, "save", MESSAGE)
        self.write(c, "bot/state/bot_state.json", json.dumps({"current_pr": 10}))
        self.assertNotEqual(self.run_script(c, "save", MESSAGE).returncode, 0)
        tip = self.git("show", "bot-state:bot/state/bot_state.json", cwd=self.origin)
        self.assertEqual(json.loads(tip), {"current_pr": 9})

    def test_second_bootstrap_cannot_replace_an_existing_branch(self):
        a, b = self.clone("a"), self.clone("b")
        self.ok(a, "restore", bootstrap="true")
        self.ok(b, "restore", bootstrap="true")  # both saw 'absent'
        self.ok(a, "save", MESSAGE)
        # Identical state in the same second makes an identical commit, and that
        # push is a harmless no-op; different state must be rejected.
        self.write(b, "bot/state/bot_state.json", json.dumps({"current_pr": 11}))
        self.assertNotEqual(self.run_script(b, "save", MESSAGE).returncode, 0)
        tip = self.git("show", "bot-state:bot/state/bot_state.json", cwd=self.origin)
        self.assertEqual(json.loads(tip), {"current_pr": 7})

    def test_absent_branch_without_the_flag_fails_and_blocks_save(self):
        for flag in (None, "false", ""):  # unset, the dispatch default, a scheduled run
            with self.subTest(flag=flag):
                a = self.clone(f"a{flag}")
                self.ok(a, "restore", bootstrap="true")  # leaves an 'absent' record behind
                r = self.run_script(a, "restore", bootstrap=flag)
                self.assertNotEqual(r.returncode, 0)
                self.assertIn("does not exist", r.stderr)
                self.assertIn("bootstrap_state=true", r.stderr)
                self.assertIn("Restore failed", r.stderr)
                r = self.run_script(a, "save", MESSAGE)
                self.assertNotEqual(r.returncode, 0)
                self.assertIn("no restore record", r.stderr)
                self.assertFalse(self.has_branch())

    def test_a_re_run_never_bootstraps(self):
        a = self.clone("a")
        r = self.run_script(a, "restore", bootstrap="true", extra_env={"GITHUB_RUN_ATTEMPT": "2"})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("re-run attempt 2", r.stderr)
        self.assertNotEqual(self.run_script(a, "save", MESSAGE).returncode, 0)
        self.assertFalse(self.has_branch())
        self.assertIn("bootstrap_state is set",
                      self.ok(a, "restore", bootstrap="true", extra_env={"GITHUB_RUN_ATTEMPT": "1"}))

    def test_the_flag_is_ignored_once_the_branch_exists(self):
        a = self.clone("a")
        self.ok(a, "restore", bootstrap="true")
        self.write(a, "bot/state/bot_state.json", json.dumps({"current_pr": 8}))
        self.ok(a, "save", MESSAGE)
        b = self.clone("b")  # main still says current_pr 7
        self.assertIn("restored", self.ok(b, "restore", bootstrap="true"))
        self.assertEqual(json.loads((b / "bot/state/bot_state.json").read_text()), {"current_pr": 8})
        self.assertIn("no state change", self.ok(b, "save", MESSAGE))
        self.assertEqual(self.git("rev-list", "--count", "bot-state", cwd=self.origin), "1")

    def test_read_error_fails_and_blocks_save(self):
        a = self.clone("a")
        self.git("remote", "set-url", "origin", (self.tmp / "missing.git").as_uri(), cwd=a)
        r = self.run_script(a, "restore")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("cannot read", r.stderr)
        r = self.run_script(a, "save", MESSAGE)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("restore", r.stderr)

    def test_save_requires_a_restore(self):
        r = self.run_script(self.clone("a"), "save", MESSAGE)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no restore record", r.stderr)

    def test_restore_refuses_files_outside_the_state_paths(self):
        rogue = self.clone("rogue")
        self.git("checkout", "--quiet", "--orphan", "bot-state", cwd=rogue)
        self.git("add", "README.md", cwd=rogue)
        self.git("commit", "--quiet", "-m", "rogue", cwd=rogue)
        self.git("push", "--quiet", "origin", "bot-state", cwd=rogue)
        r = self.run_script(self.clone("a"), "restore")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("outside the state paths", r.stderr)


class WorkflowWiringTest(unittest.TestCase):
    def setUp(self):
        self.steps = yaml.safe_load(WORKFLOW.read_text())["jobs"]["poll"]["steps"]
        self.names = [s.get("name", "") for s in self.steps]

    def index(self, prefix):
        return next(i for i, n in enumerate(self.names) if n.startswith(prefix))

    def test_restore_runs_before_anything_reads_state(self):
        restore = self.index("Restore bot state")
        self.assertEqual(self.steps[restore]["run"].strip(), "bot/ops/state-branch.sh restore")
        for later in ("Refresh Brain queue", "Decide", "Poll tick", "Refresh tag table"):
            self.assertLess(restore, self.index(later), later)

    def test_bootstrap_is_an_explicit_dispatch_input(self):
        doc = yaml.safe_load(WORKFLOW.read_text())
        triggers = doc.get("on", doc.get(True))  # PyYAML reads the bare key `on` as True
        flag = triggers["workflow_dispatch"]["inputs"]["bootstrap_state"]
        self.assertEqual((flag["type"], flag["default"]), ("boolean", False))
        restore = self.steps[self.index("Restore bot state")]
        self.assertEqual(restore["env"]["BOT_STATE_BOOTSTRAP"], "${{ inputs.bootstrap_state }}")

    def test_a_bootstrap_run_never_ticks_or_writes_upstream(self):
        decide = self.steps[self.index("Decide")]["run"]
        self.assertLess(decide.index('"${{ inputs.bootstrap_state }}" = "true"'), decide.index("poll.py"))
        self.assertEqual(self.steps[self.index("Refresh tag table")]["if"],
                         "always() && steps.restore.outcome == 'success' && !inputs.bootstrap_state")

    def test_state_is_saved_to_the_branch_only_after_a_restore(self):
        save = self.steps[self.index("Commit poll state")]
        self.assertIn("steps.restore.outcome == 'success'", save["if"])
        # A bootstrap run sets act=false; the save must still run to create the branch.
        self.assertNotIn("steps.decide", save["if"])
        self.assertIn("bot/ops/state-branch.sh save", save["run"])

    def test_always_steps_never_act_on_main_s_snapshot(self):
        for step in self.steps:
            if "always()" in str(step.get("if", "")):
                self.assertIn("steps.restore.outcome == 'success'", step["if"], step.get("name"))

    def test_no_step_pushes_to_main(self):
        for step in self.steps:
            self.assertNotIn("git push", step.get("run", ""), step.get("name"))

    def test_long_steps_time_out_before_the_job_does(self):
        # A step that hits its own timeout fails the run; the job limit cancels it.
        job = yaml.safe_load(WORKFLOW.read_text())["jobs"]["poll"]["timeout-minutes"]
        for prefix in ("Install pinned Claude CLI", "Clone mathlib4", "Poll tick"):
            self.assertLess(self.steps[self.index(prefix)].get("timeout-minutes", job), job, prefix)


if __name__ == "__main__":
    unittest.main()
