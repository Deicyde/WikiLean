#!/usr/bin/env python3
"""Stubbed-gh tests for the poller: decide(), tick() and the settle gh helpers.

Every gh call is answered by a fake, so nothing touches the network. The rule
under test: a gh failure (non-zero exit, a JSON error body, empty output, a
crash) must surface as an error and a non-zero exit, never as POLL_DECISION=wait.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

import poll
import settle

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
WORKFLOW = ROOT / ".github" / "workflows" / "wikidata-poll.yml"
REPO = "leanprover-community/mathlib4"
PR = 42608
MASTER = "b" * 40
CONFLICT_SHA = "a" * 40

AUTH_BODY = ('{"message":"Bad credentials","documentation_url":'
             '"https://docs.github.com/rest","status":"401"}')


def pull(state="open", title="doc: add wikidata attributes (batch 7)", merged=False):
    return {"number": PR, "state": state, "merged": merged, "merged_at": None,
            "title": title, "created_at": "2026-08-10T11:00:00Z",
            "head": {"sha": "c" * 40}}


DIFF = """diff --git a/Mathlib/Data/Set/Defs.lean b/Mathlib/Data/Set/Defs.lean
+++ b/Mathlib/Data/Set/Defs.lean
+@[wikidata Q36161]
+++ b/Mathlib/Order/Defs/PartialOrder.lean
+@[wikidata Q474715]
"""

CHECKS_GREEN = ("ci / Build\tpass\t10m\thttps://example.invalid/1\t\n"
                "ci / Post-Build Step\tpass\t1m\thttps://example.invalid/2\t\n")


def approvals(*logins, assoc="CONTRIBUTOR"):
    return [{"user": {"login": u}, "author_association": assoc, "state": "APPROVED",
             "submitted_at": "2026-08-11T00:00:00Z"} for u in logins]


class FakeGh:
    """Answers gh argv the way gh 2.x does; records every call.

    `fail(argv)` may return (rc, stdout, stderr) to override any route, which is
    how the auth-failure and empty-output scenarios replace every answer.
    """

    def __init__(self, *, pr_json=None, state="OPEN", mergeable=("MERGEABLE",),
                 master=MASTER, diff=DIFF, reviews=(), comments=(), issue_comments=(),
                 checks=(0, CHECKS_GREEN, ""), fail=None):
        self.pr_json = pr_json if pr_json is not None else pull()
        self.state = state
        self.mergeable = list(mergeable)
        self.master = master
        self.diff = diff
        self.reviews = list(reviews)
        self.comments = list(comments)
        self.issue_comments = list(issue_comments)
        self.checks = checks
        self.fail = fail
        self.calls = []

    def __call__(self, cmd, *args, **kwargs):
        cmd = [str(c) for c in cmd]
        self.calls.append(cmd)
        if cmd[0] != "gh":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        answer = self.fail(cmd) if self.fail else None
        rc, out, err = answer if answer is not None else self.route(cmd)
        return subprocess.CompletedProcess(cmd, rc, stdout=out, stderr=err)

    def route(self, cmd):
        sub = cmd[1:]
        if sub[:2] == ["pr", "view"]:
            field = sub[sub.index("--json") + 1]
            if field == "state":
                return 0, self.state + "\n", ""
            if field == "mergeable":
                value = self.mergeable.pop(0) if len(self.mergeable) > 1 else self.mergeable[0]
                return 0, value + "\n", ""
        if sub[:2] == ["pr", "diff"]:
            return 0, self.diff, ""
        if sub[:2] == ["pr", "checks"]:
            return self.checks
        if sub[0] == "api":
            path = sub[1]
            lines = lambda items: "".join(json.dumps(i) + "\n" for i in items)
            if path == f"repos/{REPO}/pulls/{PR}":
                return 0, json.dumps(self.pr_json), ""
            if path == f"repos/{REPO}/commits/master":
                return 0, self.master + "\n", ""
            if path == f"repos/{REPO}/pulls/{PR}/reviews":
                return 0, lines(self.reviews), ""
            if path == f"repos/{REPO}/pulls/{PR}/comments":
                return 0, lines(self.comments), ""
            if path == f"repos/{REPO}/issues/{PR}/comments":
                return 0, lines(self.issue_comments), ""
            if path.startswith("orgs/"):
                return 1, "", "gh: Not Found (HTTP 404)\n"
        raise AssertionError(f"unrouted gh call: {cmd}")


def auth_failure(cmd):
    """An expired or revoked token: `gh api` prints the 401 JSON body on stdout
    and exits 1; `gh pr ...` prints nothing on stdout and exits 1."""
    if cmd[1] == "api":
        return 1, AUTH_BODY, "gh: Bad credentials (HTTP 401)\n"
    return 1, "", ("HTTP 401: Bad credentials (https://api.github.com/graphql)\n"
                   "Try authenticating with:  gh auth login\n")


def empty_output(cmd):
    return 0, "", ""


def crash(cmd):
    raise FileNotFoundError(2, "No such file or directory", "gh")


class PollFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.here = Path(self.tmp.name)
        (self.here / "state").mkdir()
        (self.here / "state" / "batch7_approved.json").write_text(json.dumps({
            "title": "doc: add wikidata attributes (batch 7)",
            "tags": [{"qid": "Q36161", "decl": "Set", "file": "Mathlib/Data/Set/Defs.lean"},
                     {"qid": "Q474715", "decl": "PartialOrder",
                      "file": "Mathlib/Order/Defs/PartialOrder.lean"}]}))
        patches = [
            mock.patch.object(poll, "HERE", self.here),
            mock.patch.object(poll, "STATE", self.here / "state" / "bot_state.json"),
            mock.patch.object(poll.time, "sleep", lambda s: None),
            mock.patch.dict(os.environ, {"WIKILEAN_PUSH_PROPERTY": "0"}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        quiet = contextlib.redirect_stdout(io.StringIO())  # poll narrates every step
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)
        settle._ORG_MEMBER.clear()

    def write_state(self, **state):
        base = {"current_pr": PR, "batch_num": 7, "branch": "wikilean/wikidata-batch-7"}
        base.update(state)
        (self.here / "state" / "bot_state.json").write_text(json.dumps(base))

    def run_with(self, fake, fn, *args, **kwargs):
        with mock.patch.object(subprocess, "run", fake):
            return fn(*args, **kwargs)


class DecideTest(PollFixture):
    def test_no_current_pr_opens_first_batch(self):
        (self.here / "state" / "bot_state.json").write_text("{}")
        self.assertEqual(self.run_with(FakeGh(), poll.decide), "act")

    def test_merged_by_bors_acts(self):
        self.write_state(settled_pr=PR)
        fake = FakeGh(pr_json=pull(state="closed", title="[Merged by Bors] - doc: batch 7"),
                      state="CLOSED")
        self.assertEqual(self.run_with(fake, poll.decide), "act")

    def test_merged_by_button_acts(self):
        self.write_state(settled_pr=PR)
        fake = FakeGh(pr_json=pull(state="closed", merged=True), state="MERGED")
        self.assertEqual(self.run_with(fake, poll.decide), "act")

    def test_closed_not_merged_halts_instead_of_waiting(self):
        self.write_state(settled_pr=PR)
        fake = FakeGh(pr_json=pull(state="closed"), state="CLOSED")
        with self.assertRaisesRegex(poll.Halt, "CLOSED but not merged"):
            self.run_with(fake, poll.decide)

    def test_open_unsettled_gate_not_met_waits(self):
        self.write_state()
        self.assertEqual(self.run_with(FakeGh(reviews=approvals("Deicyde")), poll.decide), "wait")

    def test_open_unsettled_gate_met_acts(self):
        self.write_state()
        fake = FakeGh(reviews=approvals("Deicyde") + approvals("jcommelin"))
        self.assertEqual(self.run_with(fake, poll.decide), "act")

    def test_open_settled_clean_waits(self):
        self.write_state(settled_pr=PR, conflict_sha=CONFLICT_SHA)
        fake = FakeGh(reviews=approvals("Deicyde") + approvals("jcommelin"))
        self.assertEqual(self.run_with(fake, poll.decide), "wait")

    def test_settled_conflicting_acts(self):
        self.write_state(settled_pr=PR, conflict_sha=CONFLICT_SHA)
        fake = FakeGh(mergeable=("CONFLICTING",),
                      reviews=approvals("Deicyde") + approvals("jcommelin"))
        self.assertEqual(self.run_with(fake, poll.decide), "act")

    def test_settled_conflicting_already_rebuilt_on_this_master_waits(self):
        self.write_state(settled_pr=PR, conflict_sha=MASTER)
        fake = FakeGh(mergeable=("CONFLICTING",),
                      reviews=approvals("Deicyde") + approvals("jcommelin"))
        self.assertEqual(self.run_with(fake, poll.decide), "wait")

    def test_unknown_mergeable_is_reread_until_github_answers(self):
        self.write_state(settled_pr=PR, conflict_sha=CONFLICT_SHA)
        fake = FakeGh(mergeable=("UNKNOWN", "UNKNOWN", "CONFLICTING"),
                      reviews=approvals("Deicyde") + approvals("jcommelin"))
        self.assertEqual(self.run_with(fake, poll.decide), "act")
        reads = [c for c in fake.calls if "mergeable" in c]
        self.assertEqual(len(reads), 3)

    def test_persistently_unknown_mergeable_halts(self):
        self.write_state(settled_pr=PR, conflict_sha=CONFLICT_SHA)
        fake = FakeGh(mergeable=("UNKNOWN",),
                      reviews=approvals("Deicyde") + approvals("jcommelin"))
        with self.assertRaisesRegex(poll.Halt, "UNKNOWN"):
            self.run_with(fake, poll.decide)

    def test_settled_ci_cache_flake_acts(self):
        self.write_state(settled_pr=PR, conflict_sha=CONFLICT_SHA)
        flake = "ci / Build\tpass\t10m\tu\t\nci / Post-Build Step\tfail\t1m\tu\t\n"
        fake = FakeGh(reviews=approvals("Deicyde") + approvals("jcommelin"), checks=(1, flake, ""))
        self.assertEqual(self.run_with(fake, poll.decide), "act")

    def test_auth_failure_raises_with_a_rotation_hint(self):
        for settled in ({}, {"settled_pr": PR}):
            with self.subTest(settled=settled):
                self.write_state(**settled)
                with self.assertRaises(settle.GhError) as caught:
                    self.run_with(FakeGh(fail=auth_failure), poll.decide)
                message = str(caught.exception)
                self.assertIn("Bad credentials", message)
                self.assertIn("MATHLIB_FORK_PAT", message)

    def test_auth_failure_on_any_later_call_raises(self):
        # The first calls succeed and a later one fails: still an error, not 'wait'.
        self.write_state(settled_pr=PR, conflict_sha=CONFLICT_SHA)
        for broken in ("pr view", "reviews", "mergeable", "commits/master", "pr checks", "pr diff"):
            with self.subTest(broken=broken):
                settle._ORG_MEMBER.clear()
                fake = FakeGh(mergeable=("CONFLICTING",) if broken == "commits/master" else ("MERGEABLE",),
                              reviews=approvals("Deicyde") + approvals("jcommelin"),
                              fail=lambda c, b=broken: auth_failure(c) if b in " ".join(c) else None)
                with self.assertRaises(settle.GhError):
                    self.run_with(fake, poll.decide)

    def test_empty_stdout_raises(self):
        for settled in ({}, {"settled_pr": PR}):
            with self.subTest(settled=settled):
                self.write_state(**settled)
                with self.assertRaises(settle.GhError):
                    self.run_with(FakeGh(fail=empty_output), poll.decide)

    def test_empty_state_from_pr_view_raises(self):
        self.write_state()
        fake = FakeGh(fail=lambda c: (0, "", "") if "state" in c and c[1] == "pr" else None)
        with self.assertRaisesRegex(settle.GhError, "empty"):
            self.run_with(fake, poll.decide)

    def test_crash_propagates(self):
        self.write_state()
        with self.assertRaises(FileNotFoundError):
            self.run_with(FakeGh(fail=crash), poll.decide)


class TickTest(PollFixture):
    def test_merged_supervised_does_not_open(self):
        self.write_state(settled_pr=PR)
        fake = FakeGh(pr_json=pull(state="closed", title="[Merged by Bors] - x"), state="CLOSED")
        self.run_with(fake, poll.tick, Path("/unused"), True, no_open=True)
        self.assertFalse([c for c in fake.calls if c[0] != "gh"])

    def test_merged_opens_next_batch(self):
        self.write_state(settled_pr=PR)
        fake = FakeGh(pr_json=pull(state="closed", title="[Merged by Bors] - x"), state="CLOSED")
        self.run_with(fake, poll.tick, Path("/unused"), True)
        opened = [c for c in fake.calls if c[0] != "gh"]
        self.assertEqual(len(opened), 1)
        self.assertTrue(opened[0][1].endswith("open_batch.py"))
        self.assertNotIn("--apply", opened[0])

    def test_closed_not_merged_halts(self):
        self.write_state(settled_pr=PR)
        fake = FakeGh(pr_json=pull(state="closed"), state="CLOSED")
        with self.assertRaisesRegex(poll.Halt, "manual attention"):
            self.run_with(fake, poll.tick, Path("/unused"), True)

    def test_settled_conflicting_rebuilds(self):
        self.write_state(settled_pr=PR, conflict_sha=CONFLICT_SHA)
        fake = FakeGh(mergeable=("UNKNOWN", "CONFLICTING"),
                      reviews=approvals("Deicyde") + approvals("jcommelin"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.run_with(fake, poll.tick, Path("/unused"), True)
        self.assertIn("RESOLVE-CONFLICT #42608: rebuild off fresh master with 2 green tags", out.getvalue())

    def test_auth_failure_raises(self):
        self.write_state(settled_pr=PR)
        with self.assertRaises(settle.GhError):
            self.run_with(FakeGh(fail=auth_failure), poll.tick, Path("/unused"), True)

    def test_refresh_table_auth_failure_raises(self):
        self.write_state()
        with self.assertRaises(settle.GhError):
            self.run_with(FakeGh(fail=auth_failure), poll.refresh_table)


class SettleHelpersTest(PollFixture):
    def test_gh_obj_rejects_error_body_with_nonzero_exit(self):
        with self.assertRaisesRegex(settle.GhError, "Bad credentials"):
            self.run_with(FakeGh(fail=auth_failure), settle.gh_obj, REPO, f"pulls/{PR}")

    def test_gh_obj_rejects_empty_and_non_json_output(self):
        for out in ("", "<html>busy</html>"):
            with self.subTest(out=out):
                with self.assertRaises(settle.GhError):
                    self.run_with(FakeGh(fail=lambda c, o=out: (0, o, "")),
                                  settle.gh_obj, REPO, f"pulls/{PR}")

    def test_is_merged_raises_on_auth_failure(self):
        with self.assertRaises(settle.GhError):
            self.run_with(FakeGh(fail=auth_failure), settle.is_merged, PR, REPO)

    def test_gh_list_raises_on_auth_failure_and_accepts_an_empty_list(self):
        with self.assertRaises(settle.GhError):
            self.run_with(FakeGh(fail=auth_failure), settle.gh_list, REPO, f"pulls/{PR}/reviews")
        self.assertEqual(self.run_with(FakeGh(), settle.gh_list, REPO, f"pulls/{PR}/reviews"), [])

    def test_reactions_failure_is_not_swallowed(self):
        # A dropped thumbs-down would turn a rejected tag green.
        with self.assertRaises(settle.GhError):
            self.run_with(FakeGh(fail=auth_failure), settle.gh_reactions, REPO, 1)

    def test_org_membership_distinguishes_not_found_from_failure(self):
        member = lambda c: (0, "", "") if c[1] == "api" and c[2].startswith("orgs/") else None
        self.assertTrue(self.run_with(FakeGh(fail=member), settle.is_org_member, REPO, "someone"))
        settle._ORG_MEMBER.clear()
        self.assertFalse(self.run_with(FakeGh(), settle.is_org_member, REPO, "someone"))
        settle._ORG_MEMBER.clear()
        with self.assertRaises(settle.GhError):
            self.run_with(FakeGh(fail=auth_failure), settle.is_org_member, REPO, "someone")

    def test_classify_rejects_an_empty_diff(self):
        with self.assertRaisesRegex(settle.GhError, "empty"):
            self.run_with(FakeGh(diff=""), settle.classify, PR, REPO)

    def test_classify_reads_tags_from_the_diff(self):
        result = self.run_with(FakeGh(reviews=approvals("Deicyde")), settle.classify, PR, REPO)
        self.assertEqual(result["tags"], ["Q36161", "Q474715"])


class DecideCommandLineTest(unittest.TestCase):
    """`poll.py --decide` as CI runs it, with a fake gh executable on PATH."""

    FAKE_GH = r"""
import json, os, sys
mode = os.environ["FAKE_GH_MODE"]
argv = sys.argv[1:]
if mode == "auth":
    if argv[0] == "api":
        sys.stdout.write(%(body)r)
        sys.stderr.write("gh: Bad credentials (HTTP 401)\n")
    else:
        sys.stderr.write("HTTP 401: Bad credentials (https://api.github.com/graphql)\n")
    sys.exit(1)
if mode == "crash":
    raise RuntimeError("simulated gh crash")
if mode == "merged":
    print(json.dumps({"state": "closed", "merged": False, "merged_at": None,
                      "title": "[Merged by Bors] - doc: batch"}))
    sys.exit(0)
sys.exit(f"unexpected mode {mode}")
"""

    def run_decide(self, mode, with_gh=True):
        with tempfile.TemporaryDirectory() as tmp:
            bindir = Path(tmp)
            if with_gh:
                gh = bindir / "gh"
                gh.write_text(f"#!{sys.executable}\n" + self.FAKE_GH % {"body": AUTH_BODY})
                gh.chmod(0o755)
            env = {"PATH": str(bindir), "FAKE_GH_MODE": mode, "PYTHONDONTWRITEBYTECODE": "1"}
            return subprocess.run([sys.executable, str(HERE / "poll.py"), "--mathlib", "/tmp/unused",
                                   "--decide"], capture_output=True, text=True, env=env, timeout=60)

    def test_auth_failure_exits_nonzero_with_a_clear_message(self):
        r = self.run_decide("auth")
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("POLL_DECISION", r.stdout)
        self.assertIn("POLL_ERROR", r.stderr)
        self.assertIn("Bad credentials", r.stderr)

    def test_gh_crash_exits_nonzero(self):
        r = self.run_decide("crash")
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("POLL_DECISION", r.stdout)

    def test_missing_gh_exits_nonzero(self):
        r = self.run_decide("merged", with_gh=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("POLL_DECISION", r.stdout)

    def test_success_prints_the_decision(self):
        r = self.run_decide("merged")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "POLL_DECISION=act")


class WorkflowDecideStepTest(unittest.TestCase):
    """The Decide step must turn the run red when poll.py fails.

    Runs the step's own script under the shell GitHub would use, with a fake
    python3 on PATH standing in for poll.py.
    """

    FAKE_PYTHON = """#!/bin/sh
case "$FAKE_DECIDE" in
  act)   echo "POLL_DECISION=act" ;;
  wait)  echo "POLL_DECISION=wait" ;;
  fail)  echo "POLL_ERROR: gh api repos/x failed (exit 1): Bad credentials" >&2; exit 1 ;;
  crash) echo "Traceback (most recent call last):" >&2; exit 1 ;;
esac
"""

    @classmethod
    def setUpClass(cls):
        steps = yaml.safe_load(WORKFLOW.read_text())["jobs"]["poll"]["steps"]
        cls.step = next(s for s in steps if s.get("id") == "decide")

    def shell_argv(self, script_path):
        # https://docs.github.com/actions/reference/workflow-syntax-for-github-actions#jobsjob_idstepsshell
        shell = self.step.get("shell")
        if shell is None:
            return ["bash", "-e", script_path]
        if shell == "bash":
            return ["bash", "--noprofile", "--norc", "-eo", "pipefail", script_path]
        if "{0}" in shell:
            return [script_path if a == "{0}" else a for a in shell.split()]
        self.fail(f"unsupported shell {shell!r}")

    def run_step(self, decide_mode):
        script = self.step["run"].replace("${{ inputs.force_heavy }}", "")
        self.assertNotIn("${{", script, "the test only substitutes inputs.force_heavy")
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "bin").mkdir()
            fake = tmp / "bin" / "python3"
            fake.write_text(self.FAKE_PYTHON)
            fake.chmod(0o755)
            (tmp / "step.sh").write_text(script)
            output = tmp / "github_output"
            output.write_text("")
            env = {"PATH": f"{tmp / 'bin'}:/usr/bin:/bin", "GITHUB_OUTPUT": str(output),
                   "FAKE_DECIDE": decide_mode, "RUNNER_TEMP": str(tmp)}
            r = subprocess.run(self.shell_argv(str(tmp / "step.sh")), cwd=tmp, env=env,
                               capture_output=True, text=True, timeout=60)
            return r, output.read_text()

    def test_failing_decide_fails_the_step(self):
        for mode in ("fail", "crash"):
            with self.subTest(mode=mode):
                r, output = self.run_step(mode)
                self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertNotIn("act=", output)

    def test_act_and_wait_set_the_output(self):
        for mode, expected in (("act", "act=true"), ("wait", "act=false")):
            with self.subTest(mode=mode):
                r, output = self.run_step(mode)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertEqual(output.strip(), expected)


if __name__ == "__main__":
    unittest.main()
