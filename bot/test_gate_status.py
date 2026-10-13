#!/usr/bin/env python3
"""Tests for bot/gate_status.py with a stubbed gh (no network).

Run step lists are built from the real .github/workflows/wikidata-poll.yml, so a
renamed or reordered step breaks these tests instead of silently mislabelling runs.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

import gate_status

HERE = Path(__file__).resolve().parent
WORKFLOW_STEPS = yaml.safe_load((HERE.parent / ".github" / "workflows" / "wikidata-poll.yml").read_text()
                                )["jobs"]["poll"]["steps"]
NOW = dt.datetime(2026, 10, 13, 12, 0, tzinfo=dt.timezone.utc)
LIVE_STATE = {"current_pr": 42608, "batch_num": 7, "settled_pr": 42608}
NOT_FOUND = "gh: Not Found (HTTP 404)"


def run_steps(act=False, decide="success", fail=None):
    """The poll job's steps as the jobs API reports them for one run of the real workflow."""
    out, failed, restore_failed = [{"name": "Set up job", "conclusion": "success"}], False, False
    for step in WORKFLOW_STEPS:
        name = step.get("name") or f"Run {step['uses']}"
        cond = str(step.get("if", ""))
        if "always()" in cond:
            conclusion = "skipped" if restore_failed and "steps.restore.outcome" in cond else "success"
        elif failed:
            conclusion = "skipped"
        elif name.startswith("Decide"):
            conclusion = decide
        elif "steps.decide.outputs.act" in cond:
            conclusion = "success" if act else "skipped"
        else:
            conclusion = "success"
        if fail and name.startswith(fail):
            conclusion = "failure"
        if conclusion == "failure":
            failed = True
            restore_failed = restore_failed or name.startswith("Restore bot state")
        out.append({"name": name, "conclusion": conclusion})
    return out + [{"name": "Complete job", "conclusion": "success"}]


def run(id_, conclusion, created, status="completed"):
    return {"databaseId": id_, "status": status, "conclusion": conclusion, "createdAt": created,
            "updatedAt": created, "event": "schedule",
            "url": f"https://github.com/Deicyde/WikiLean/actions/runs/{id_}"}


class FakeGh:
    def __init__(self, state=LIVE_STATE, state_raw=None, state_error=None, branch_exists=True,
                 runs=None, runs_error=None, steps=None, jobs_raw=None, pr=None, pr_error=None):
        self.state_raw = state_raw if state_raw is not None else json.dumps(state)
        self.state_error, self.branch_exists = state_error, branch_exists
        self.runs = runs if runs is not None else [run(2, "success", "2026-10-13T10:00:00Z")]
        self.runs_error = runs_error
        self.jobs_raw = jobs_raw if jobs_raw is not None else json.dumps(
            {"jobs": [{"name": "poll", "steps": steps if steps is not None else run_steps()}]})
        self.pr, self.pr_error = pr, pr_error
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        assert cmd[0] == "gh", cmd
        args = cmd[1:]
        ok = lambda out: subprocess.CompletedProcess(cmd, 0, out, "")
        bad = lambda err: subprocess.CompletedProcess(cmd, 1, "", err)
        if args[0] == "api" and "contents/bot/state/bot_state.json" in args[-1]:
            return bad(self.state_error) if self.state_error else ok(self.state_raw)
        if args[0] == "api" and args[1].endswith("/branches/bot-state"):
            return ok("bot-state\n") if self.branch_exists else bad(NOT_FOUND)
        if args[:2] == ["run", "list"]:
            assert args[args.index("--branch") + 1] == "main"
            return bad(self.runs_error) if self.runs_error else ok(json.dumps(self.runs))
        if args[0] == "api" and args[1].endswith("/jobs"):
            return ok(self.jobs_raw)
        if args[:2] == ["pr", "view"]:
            if self.pr_error:
                return bad(self.pr_error)
            return ok(json.dumps(self.pr or {
                "state": "OPEN", "mergeable": "MERGEABLE", "createdAt": "2026-10-01T00:00:00Z",
                "labels": [], "url": "https://github.com/leanprover-community/mathlib4/pull/42608"}))
        raise AssertionError(f"unrouted {cmd}")


class DecisionTest(unittest.TestCase):
    def test_decisions_on_the_real_workflow(self):
        cases = {
            "wait": (run_steps(), ("wait", None)),
            "act": (run_steps(act=True), ("act", None)),
            "act, setup fails": (run_steps(act=True, fail="Install pinned Claude CLI"),
                                 ("act", "Install pinned Claude CLI")),
            "act, node fails": (run_steps(act=True, fail="Run actions/setup-node"), ("act", None)),
            "act, tick fails": (run_steps(act=True, fail="Poll tick"), ("act", "Poll tick (apply)")),
            "wait, save fails": (run_steps(fail="Commit poll state"), ("wait", "Commit poll state")),
            "wait, table fails": (run_steps(fail="Refresh tag table"), ("wait", "Refresh tag table")),
            "decide fails": (run_steps(decide="failure"), ("error", "Decide")),
            "brain queue fails": (run_steps(fail="Refresh Brain queue"), ("error", "Refresh Brain queue")),
            "restore fails": (run_steps(fail="Restore bot state"),
                              ("error", "Restore bot state from the bot-state branch")),
        }
        for label, (steps, expected) in cases.items():
            with self.subTest(label):
                decision, failed = gate_status.decision_of(steps)
                if label == "act, node fails":  # the unnamed step's name carries the pinned ref
                    self.assertEqual(decision, "act")
                    self.assertTrue(failed.startswith("Run actions/setup-node@"))
                else:
                    self.assertEqual((decision, failed), expected)

    def test_renamed_steps_read_unknown(self):
        self.assertEqual(gate_status.decision_of([{"name": "Something", "conclusion": "success"}]),
                         ("unknown", None))


class ReportTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.snapshot = Path(tmp.name) / "bot_state.json"
        self.snapshot.write_text(json.dumps({"current_pr": 42608, "batch_num": 7, "frozen": True}))
        patch = mock.patch.object(gate_status, "SNAPSHOT", self.snapshot)
        patch.start()
        self.addCleanup(patch.stop)

    def report(self, fake, live=True):
        with mock.patch.object(subprocess, "run", fake):
            return gate_status.report(live, now=NOW)

    def test_offline_touches_no_network_and_says_so(self):
        fake = FakeGh()
        r = self.report(fake, live=False)
        self.assertEqual(r["state"]["source"], "main-snapshot")
        self.assertEqual(fake.calls, [])
        self.assertEqual(len(r["alerts"]), 1)
        self.assertTrue(r["alerts"][0].startswith("offline: main's frozen snapshot only"))

    def test_live_reads_the_bot_state_branch(self):
        r = self.report(FakeGh())
        self.assertEqual(r["state"], {**LIVE_STATE, "source": "bot-state"})
        self.assertEqual((r["poller"]["decision"], r["poller"]["failed_step"]), ("wait", None))
        self.assertEqual(r["alerts"], [])

    def test_a_source_key_in_the_state_cannot_relabel_it(self):
        r = self.report(FakeGh(state={**LIVE_STATE, "source": "forged"}))
        self.assertEqual(r["state"]["source"], "bot-state")

    def test_missing_branch_falls_back_with_the_hazard_spelled_out(self):
        pr = {"state": "OPEN", "mergeable": "CONFLICTING", "createdAt": "2026-10-01T00:00:00Z",
              "labels": [], "url": "u"}
        r = self.report(FakeGh(state_error=NOT_FOUND, branch_exists=False, pr=pr))
        self.assertEqual(r["state"]["source"], "main-snapshot")
        self.assertTrue(r["state"]["branch_missing"])
        self.assertTrue(any(a.startswith("no bot-state branch") and "stale snapshot" in a for a in r["alerts"]))
        self.assertIn("batch PR #42608 conflicts with master (batch number from main's frozen snapshot)",
                      r["alerts"])

    def test_branch_without_the_state_file_is_an_error(self):
        r = self.report(FakeGh(state_error=NOT_FOUND, branch_exists=True))
        self.assertIn("has no bot/state/bot_state.json", r["state"]["error"])
        self.assertNotIn("pr", r)

    def test_other_read_errors_are_alerts_not_fallbacks(self):
        r = self.report(FakeGh(state_error="gh: Bad credentials (HTTP 401)"))
        self.assertTrue(any(a.startswith("bot state unreadable (bot-state)") for a in r["alerts"]))
        self.assertNotIn("pr", r)

    def test_non_object_state_is_an_error_not_a_crash(self):
        for raw in ("null", "[]", "\"x\"", "{not json"):
            with self.subTest(raw=raw):
                r = self.report(FakeGh(state_raw=raw))
                self.assertIn("error", r["state"])

    def test_a_red_latest_run_names_its_decision_and_failed_step(self):
        runs = [run(3, "failure", "2026-10-13T11:00:00Z"), run(2, "success", "2026-10-13T07:00:00Z")]
        r = self.report(FakeGh(runs=runs, steps=run_steps(act=True, fail="Install pinned Claude CLI")))
        self.assertIn("latest poller run 3 is failure (decision act, failed at 'Install pinned Claude CLI'): "
                      "https://github.com/Deicyde/WikiLean/actions/runs/3", r["alerts"])

    def test_an_earlier_red_run_still_alerts_after_a_green_one(self):
        runs = [run(4, "success", "2026-10-13T11:00:00Z"), run(3, "failure", "2026-10-13T07:00:00Z")]
        r = self.report(FakeGh(runs=runs))
        self.assertIn("1 poller run(s) failed in the last 24h "
                      "(latest: https://github.com/Deicyde/WikiLean/actions/runs/3)", r["alerts"])

    def test_cancelled_runs_are_skipped(self):
        runs = [run(5, "cancelled", "2026-10-13T11:30:00Z"), run(4, "success", "2026-10-13T11:00:00Z")]
        r = self.report(FakeGh(runs=runs))
        self.assertEqual(r["poller"]["run_id"], 4)
        self.assertEqual(r["alerts"], [])

    def test_no_success_in_a_day_is_an_alert(self):
        runs = [run(3, "failure", "2026-10-13T11:00:00Z"), run(2, "success", "2026-10-11T07:00:00Z")]
        r = self.report(FakeGh(runs=runs, steps=run_steps(decide="failure")))
        self.assertTrue(any(a.startswith("no successful poller run in 24h") for a in r["alerts"]))

    def test_unreadable_runs_jobs_and_pr_are_alerts(self):
        r = self.report(FakeGh(runs_error="HTTP 502: Bad Gateway", pr_error="HTTP 401: Bad credentials"))
        self.assertTrue(any(a.startswith("poller runs unreadable: cannot list poller runs") for a in r["alerts"]))
        self.assertTrue(any(a.startswith("batch PR #42608 unreadable") for a in r["alerts"]))
        r = self.report(FakeGh(jobs_raw="[]"))
        self.assertIn("decision_error", r["poller"])

    def test_conflicting_aging_and_closed_batch_prs(self):
        conflicting = {"state": "OPEN", "mergeable": "CONFLICTING", "createdAt": "2026-08-10T11:18:00Z",
                       "labels": [{"name": "merge-conflict"}], "url": "u"}
        r = self.report(FakeGh(pr=conflicting))
        self.assertIn("batch PR #42608 conflicts with master", r["alerts"])
        self.assertIn("batch PR #42608 open 64 days", r["alerts"])
        label_only = dict(conflicting, mergeable="UNKNOWN", createdAt="2026-10-01T00:00:00Z")
        self.assertIn("batch PR #42608 conflicts with master", self.report(FakeGh(pr=label_only))["alerts"])
        closed = dict(conflicting, state="CLOSED", labels=[])
        self.assertTrue(any("is CLOSED, not merged" in a for a in self.report(FakeGh(pr=closed))["alerts"]))

    def test_main_prints_json_even_when_report_crashes(self):
        out = io.StringIO()
        with mock.patch.object(gate_status, "report", side_effect=KeyError("boom")), \
             mock.patch.object(sys, "argv", ["gate_status.py"]), contextlib.redirect_stdout(out):
            gate_status.main()
        self.assertEqual(json.loads(out.getvalue())["alerts"], ["gate_status failed: KeyError: 'boom'"])


class CommandLineTest(unittest.TestCase):
    def test_offline_command_prints_one_json_object(self):
        with tempfile.TemporaryDirectory() as tmp:
            # A private copy with its own state, so the repo's snapshot is never read.
            shutil.copy(HERE / "gate_status.py", tmp)
            (Path(tmp) / "state").mkdir()
            (Path(tmp) / "state" / "bot_state.json").write_text(json.dumps({"current_pr": 1}))
            r = subprocess.run([sys.executable, str(Path(tmp) / "gate_status.py")],
                               capture_output=True, text=True, timeout=60, env={"PATH": tmp})
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out["state"], {"current_pr": 1, "source": "main-snapshot"})


if __name__ == "__main__":
    unittest.main()
