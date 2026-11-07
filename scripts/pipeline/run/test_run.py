#!/usr/bin/env python3
"""Tests for scripts/pipeline/run/ — stage 10: the plan, the harvest and the watchdog.

New code, on synthetic fixtures. The synthetic source is the gate suite's
(``scripts/pipeline/gate/test_gate.py``): one fixture, both suites, so a document that
passes the gate here is the same document the gate tests gate.

**No model is called.** ``agent.call`` runs a local command in place of the worker, which
is how a contributor's stub worker runs too; the real call needs ``--execute``, and the
one test that touches it asserts the options are ``site/agent_sandbox``'s.

What is held:

* the plan writes the same root twice — a re-plan of an unchanged run is the same bytes —
  and cuts only the located passages into tasks;
* the harvest is a reconciliation: a document that did not land is ``missing`` and the
  receipt is not complete, a second harvest says what it gained, and a file whose bytes
  differ from the source's is named;
* the watchdog prices an unpriced call rather than counting it zero, holds the per-task
  rule back below ``--min-tasks``, and **keeps watching after a drain** until the spend
  settles — the two lessons the port was built around;
* plan → stub worker → gate → harvest → tally runs end to end on a synthetic task.

Run: python3 scripts/pipeline/run/test_run.py
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
PIPELINE = HERE.parent
for _candidate in (HERE, PIPELINE, PIPELINE / "gate", PIPELINE.parents[1] / "site"):
    if str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

import agent as agent_mod  # noqa: E402
import corpus as corpus_mod  # noqa: E402
import cost as cost_mod  # noqa: E402
import harvest as harvest_mod  # noqa: E402
import plan as plan_mod  # noqa: E402
import tally as tally_mod  # noqa: E402
import test_gate as fixture  # noqa: E402 — scripts/pipeline/gate/test_gate.py
import verify_locate as gate  # noqa: E402
import vocab  # noqa: E402
import watchdog as watchdog_mod  # noqa: E402

UNIVERSE = {
    "id": "u-test", "universe_sha256": fixture.UNIVERSE_SHA, "frozen": "2026-01-01",
    "libraries": [{"id": "testlib", "brain_lib": "testlib",
                   "repo": "https://example.invalid/testlib", "commit": fixture.COMMIT,
                   "toolchain": "leanprover/lean4:v4.0.0", "roots": ["Testlib"],
                   "licence": {"spdx": "Apache-2.0"}}],
    "excluded": [],
}


def _write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=1, sort_keys=True) + "\n", encoding="utf-8")


class Workspace:
    """A synthetic workspace: a rendered source, an inventory corpus and a universe."""

    def __init__(self, root: Path):
        self.root = root
        self.sources = root / "sources"
        (self.sources / fixture.SOURCE).mkdir(parents=True)
        (self.sources / fixture.SOURCE / "layer.txt").write_text(fixture.LAYER,
                                                                 encoding="utf-8")
        # the reading copy and the two views a worker's prompt also names: the staged
        # working directory is built out of exactly the files run/workdir.py lists
        (self.sources / fixture.SOURCE / "text.txt").write_text(fixture.LAYER,
                                                                encoding="utf-8")
        _write(self.sources / fixture.SOURCE / "sections.json", [])
        _write(self.sources / fixture.SOURCE / "layer_map.json",
               {"schema": "cf-layer-map/2", "renderer": "web-html/3",
                "segments": [[0, len(fixture.LAYER), 0, len(fixture.LAYER)]]})
        _write(self.sources / fixture.SOURCE / "paragraphs.json", fixture.PARAGRAPHS)
        _write(self.sources / fixture.SOURCE / "formulas.json", fixture.FORMULAS)
        _write(self.sources / fixture.SOURCE / "finders.json", fixture.FINDERS)
        self.universe = root / "universe.json"
        _write(self.universe, UNIVERSE)
        self.manifest = root / "manifest.jsonl"
        self.manifest.write_text(json.dumps({
            "source_id": fixture.SOURCE, "pageid": fixture.PAGEID, "revision": 1000000007,
            "title": "Sprocket", "wikidata": "Q1", "html_sha256": fixture.HTML_SHA,
            "layer_sha256": gate.sha256_bytes(fixture.LAYER.encode("utf-8")),
            "renderer": "web-html/3", "licence_class": "open_share_alike",
            "assessment": {"class": "C", "importance": "low"}}) + "\n", encoding="utf-8")
        self.index = root / "decls.jsonl"
        self.index.write_text("".join(json.dumps(row) + "\n" for row in fixture.DECLS),
                              encoding="utf-8")
        self.candidates = root / "candidates"
        self.candidates.mkdir()
        _write(self.candidates / f"{fixture.SOURCE}.json",
               {"source_id": fixture.SOURCE, "curated": [], "lexical": []})
        self.corpus = root / "corpus"
        inv = self.corpus / fixture.INV_RUN / fixture.INV_TASK
        _write(inv / f"{fixture.SOURCE}.json", fixture.good_inventory_document())
        _write(self.corpus / fixture.INV_RUN / "RECEIPT.json",
               {"complete": True, "gate": {"ok": True},
                "harvested_at": "2026-01-02T03:04:05Z"})

    def plan(self, out: Path, **kw) -> dict:
        return plan_mod.plan(run_id=fixture.RUN, mode=vocab.MODE_LOCATE, out=out,
                             inventory=self.corpus / fixture.INV_RUN,
                             inventory_run=fixture.INV_RUN, universe=self.universe,
                             manifest=self.manifest, sources=self.sources,
                             candidates=self.candidates, index=self.index, **kw)


class PlanTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.ws = Workspace(self.root / "ws")

    def test_a_plan_cuts_only_the_located_passages_into_tasks(self):
        planned = self.ws.plan(self.root / "run", per_task=8)
        passages = [p for task in planned["tasks"] for doc in task["documents"]
                    for p in doc["passages"]]
        self.assertEqual(len(planned["tasks"]), 1)
        # three passages, of which the prose one is scoped expository and not planned;
        # a manifest passage carries the cut, not the inventory's own bookkeeping
        self.assertEqual(len(passages), 2)
        self.assertEqual({key for p in passages for key in p} - set(
            plan_mod.LOCATE_PASSAGE_KEYS), set())
        inventory = plan_mod.frozen_inventory(self.ws.corpus / fixture.INV_RUN)
        self.assertEqual(len(inventory[fixture.SOURCE]), 3)
        self.assertEqual({p["item"] for p in passages},
                         {p["item"] for p in inventory[fixture.SOURCE]
                          if p["scope"] == vocab.SCOPE_FORMAL_MATH})

    def test_the_per_task_cap_is_the_cut(self):
        planned = self.ws.plan(self.root / "run", per_task=1)
        self.assertEqual(len(planned["tasks"]), 2)
        self.assertEqual([len(t["documents"][0]["passages"]) for t in planned["tasks"]],
                         [1, 1])

    def test_a_replan_of_an_unchanged_run_writes_the_same_bytes(self):
        out = self.root / "run"
        self.ws.plan(out)
        before = {path.relative_to(out).as_posix(): path.read_bytes()
                  for path in sorted(out.rglob("*")) if path.is_file()}
        self.ws.plan(out)
        after = {path.relative_to(out).as_posix(): path.read_bytes()
                 for path in sorted(out.rglob("*")) if path.is_file()}
        self.assertEqual(sorted(before), sorted(after))
        differ = [name for name in before if before[name] != after[name]]
        self.assertEqual(differ, [])
        # and a second root holds the same plan, which is what a contributor checks
        other = self.root / "run-b"
        self.ws.plan(other)
        self.assertEqual((out / "planned.json").read_bytes(),
                         (other / "planned.json").read_bytes())

    def test_the_root_holds_the_gate_out_of_tree_and_the_runners_directories(self):
        out = self.root / "run"
        self.ws.plan(out)
        for name in plan_mod.ROOT_DIRS:
            self.assertTrue((out / name).is_dir(), name)
        self.assertTrue((out / "gate" / "verify_locate.py").is_file())
        self.assertTrue((out / "gate" / "tasks").is_dir())
        config = json.loads((out / "run.json").read_text())
        self.assertTrue(config["gate"]["out_of_tree"])
        self.assertIn("anchor_resolve.py", config["gate"]["files"])

    def test_a_source_the_manifest_does_not_pin_is_refused(self):
        self.ws.manifest.write_text("", encoding="utf-8")
        with self.assertRaises(plan_mod.PlanProblem):
            self.ws.plan(self.root / "run")


class HarvestTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.source = self.root / "from"
        self.dest = self.root / "to"
        for number in range(3):
            _write(self.source / f"task-{number}" / "enwiki-1-1.json",
                   {"schema": vocab.DOCUMENT_SCHEMA, "mode": vocab.MODE_LOCATE,
                    "source_id": "enwiki-1-1", "passages": []})

    def test_the_harvest_reconciles_and_is_complete(self):
        receipt = harvest_mod.harvest(run_id="r1", source=self.source, dest=self.dest)
        self.assertTrue(receipt["complete"])
        self.assertEqual(receipt["expected"]["count"], 3)
        self.assertEqual(receipt["actual"]["count"], 3)
        self.assertEqual(receipt["missing"], [])
        self.assertEqual(len(receipt["manifest"]), 3)
        self.assertTrue(receipt["sha_verified"])

    def test_a_document_that_did_not_land_is_missing_and_the_receipt_is_not_complete(self):
        expected = harvest_mod.source_names(self.source)
        self.dest.mkdir(parents=True)
        shutil.copytree(self.source / "task-0", self.dest / "task-0")
        receipt = harvest_mod.build_receipt(run_id="r1", expected=expected, dest=self.dest)
        self.assertFalse(receipt["complete"])
        self.assertEqual(len(receipt["missing"]), 2)
        self.assertIn("2 missing", harvest_mod.describe(receipt))

    def test_a_file_whose_bytes_are_not_the_sources_is_named(self):
        harvest_mod.harvest(run_id="r1", source=self.source, dest=self.dest)
        (self.dest / "task-0" / "enwiki-1-1.json").write_text('{"changed": true}',
                                                              encoding="utf-8")
        receipt = harvest_mod.build_receipt(
            run_id="r1", expected=harvest_mod.source_names(self.source), dest=self.dest,
            source_manifest=harvest_mod.manifest(self.source))
        self.assertEqual(receipt["sha_mismatched"], ["task-0/enwiki-1-1.json"])
        self.assertFalse(receipt["complete"])

    def test_a_second_harvest_says_what_it_gained(self):
        self.dest.mkdir(parents=True)
        shutil.copytree(self.source / "task-0", self.dest / "task-0")
        first = harvest_mod.build_receipt(run_id="r1",
                                          expected=harvest_mod.source_names(self.source),
                                          dest=self.dest)
        _write(self.dest / "RECEIPT.json", first)
        receipt = harvest_mod.harvest(run_id="r1", source=self.source, dest=self.dest)
        self.assertTrue(receipt["complete"])
        self.assertEqual(len(receipt["previous"]["gained"]), 2)
        self.assertEqual(receipt["previous"]["lost"], [])

    def test_a_file_that_is_not_a_document_is_malformed(self):
        harvest_mod.harvest(run_id="r1", source=self.source, dest=self.dest)
        (self.dest / "task-0" / "enwiki-1-1.json").write_text("[1, 2]", encoding="utf-8")
        receipt = harvest_mod.build_receipt(
            run_id="r1", expected=harvest_mod.source_names(self.source), dest=self.dest)
        self.assertEqual(receipt["malformed"], ["task-0/enwiki-1-1.json"])
        self.assertFalse(receipt["complete"])

    def test_the_receipt_is_not_complete_when_the_gate_rejected(self):
        log = self.root / "gate.jsonl"
        log.write_text(json.dumps({"changed_files": True, "pre_merge": False, "ok": False,
                                   "task_id": "task-0"}) + "\n", encoding="utf-8")
        receipt = harvest_mod.harvest(run_id="r1", source=self.source, dest=self.dest,
                                      gate_log=log)
        self.assertFalse(receipt["complete"])
        self.assertEqual(receipt["gate"]["rejected"], ["task-0"])

    def test_a_task_the_gate_passed_on_a_retry_is_gated_and_the_retry_is_counted(self):
        """The runner retries a refused document with the gate's own refusals, so one
        shard's log holds an attempt's refusal beside the pass that replaced it. Only
        the document whose last call passed is in the corpus, so the verdict is the
        last call — and the retry is still reported."""
        log = self.root / "gate.jsonl"
        log.write_text("".join(json.dumps(row) + "\n" for row in (
            {"changed_files": True, "pre_merge": False, "ok": False, "task_id": "task-0"},
            {"changed_files": True, "pre_merge": False, "ok": True, "task_id": "task-0"},
            {"changed_files": True, "pre_merge": False, "ok": True, "task_id": "task-1"},
            {"changed_files": True, "pre_merge": False, "ok": False, "task_id": "task-2"},
        )), encoding="utf-8")
        receipt = harvest_mod.harvest(run_id="r1", source=self.source, dest=self.dest,
                                      gate_log=log)
        gate = receipt["gate"]
        self.assertEqual(gate["calls"], 4)
        self.assertEqual(gate["tasks"], 3)
        self.assertEqual(gate["retried"], 1)
        self.assertEqual(gate["rejected"], ["task-2"])
        self.assertFalse(receipt["complete"])          # task-2 is still refused

    def test_every_task_passing_on_a_retry_is_a_complete_harvest(self):
        log = self.root / "gate.jsonl"
        log.write_text("".join(json.dumps(row) + "\n" for row in (
            {"changed_files": True, "pre_merge": False, "ok": False, "task_id": "task-0"},
            {"changed_files": True, "pre_merge": False, "ok": True, "task_id": "task-0"},
        )), encoding="utf-8")
        receipt = harvest_mod.harvest(run_id="r1", source=self.source, dest=self.dest,
                                      gate_log=log)
        self.assertEqual(receipt["gate"]["failed"], 0)
        self.assertEqual(receipt["gate"]["retried"], 1)
        self.assertTrue(receipt["complete"], harvest_mod.describe(receipt))


class CostTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.usage = Path(self._tmp.name) / "usage"
        self.usage.mkdir()

    def envelope(self, name: str, value: dict) -> None:
        _write(self.usage / name, value)

    def test_the_cost_block_adds_the_envelopes_up(self):
        self.envelope("t.worker.20260101T000000Z.1.json", {
            "total_cost_usd": 1.5, "duration_ms": 1000,
            "modelUsage": {"m": {"inputTokens": 10, "outputTokens": 20, "costUSD": 1.5}}})
        self.envelope("t.reviewer.20260101T000100Z.2.json", {
            "total_cost_usd": 0.25, "duration_ms": 500,
            "modelUsage": {"m": {"inputTokens": 1, "outputTokens": 2, "costUSD": 0.25}}})
        block = cost_mod.cost_block(self.usage)
        self.assertEqual(block["usd"], 1.75)
        self.assertEqual(block["envelope_count"], 2)
        self.assertEqual(block["models"], ["m"])
        self.assertEqual(block["tokens"]["m"]["output"], 22)

    def test_a_call_with_no_priced_envelope_is_counted_not_dropped(self):
        self.envelope("t.worker.20260101T000000Z.1.json",
                      {"total_cost_usd": None, "agent_call": {"elapsed_s": 2700.0}})
        block = cost_mod.cost_block(self.usage)
        self.assertEqual(block["usd"], 0.0)
        self.assertEqual(block["unpriced_calls"], 1)
        self.assertEqual(block["unpriced_seconds"], 2700.0)

    def test_a_call_that_returned_empty_at_once_is_counted_at_its_own_zero(self):
        # the record's elapsed_s is the authority even at 0.0: reading the harness's
        # duration_ms as the agent's time prices an empty return as if it had run
        self.envelope("t.worker.20260101T000000Z.1.json",
                      {"total_cost_usd": None, "duration_ms": 8,
                       "cf_agent_call": {"elapsed_s": 0.0, "outcome": "empty"}})
        block = cost_mod.cost_block(self.usage)
        self.assertEqual(block["unpriced_calls"], 1)
        self.assertEqual(block["unpriced_seconds"], 0.0)


class Args:
    def __init__(self, **kw):
        self.__dict__.update({
            "gate_log": None, "findings": None, "p90_cost_per_task": None,
            "p90_gate_failure_rate": None, "multiple": 2.0,
            "unpriced_usd_per_hour": None, "min_tasks": watchdog_mod.MIN_TASKS,
            "min_gate_calls": watchdog_mod.MIN_GATE_CALLS, "drain_command": "true",
            "interval": 1.0, "settle_timeout": 10.0, "settle_checks": 2,
            "settle_interval": 0.0, "no_settle": False, "once": True, "dry_run": True,
            **kw})


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "root"
        (self.root / "usage").mkdir(parents=True)

    def envelope(self, name: str, value: dict) -> None:
        _write(self.root / "usage" / name, value)

    def priced(self, number: int, usd: float, millis: int = 1000) -> None:
        self.envelope(f"t{number}.worker.2026010{number}T000000Z.{number}.json",
                      {"total_cost_usd": usd, "duration_ms": millis})

    # -- the ledger -----------------------------------------------------------------

    def test_an_unpriced_call_is_estimated_from_the_fastest_priced_rate(self):
        self.priced(1, 1.0, millis=1000)          # $1 a second
        self.envelope("t2.worker.20260102T000000Z.2.json",
                      {"total_cost_usd": None, "agent_call": {"elapsed_s": 10.0}})
        got = watchdog_mod.ledger(self.root / "usage")
        self.assertEqual(got["dollars"], 1.0)
        self.assertEqual(got["unpriced_calls"], 1)
        self.assertEqual(got["estimated_unpriced_usd"], 10.0)
        self.assertEqual(got["usd_per_hour"], 3600.0)

    def test_an_empty_return_is_not_priced_as_if_it_had_run(self):
        self.priced(1, 1.0, millis=1000)          # $1 a second
        for number in range(2, 5):
            self.envelope(f"t{number}.worker.2026010{number}T000000Z.{number}.json",
                          {"total_cost_usd": None, "duration_ms": 8,
                           "cf_agent_call": {"elapsed_s": 0.0, "outcome": "empty"}})
        got = watchdog_mod.ledger(self.root / "usage")
        self.assertEqual(got["unpriced_calls"], 3)
        self.assertEqual(got["unpriced_seconds"], 0.0)
        self.assertEqual(got["estimated_unpriced_usd"], 0.0)

    def test_an_unpriced_call_with_no_rate_at_all_is_not_guessed_at(self):
        self.envelope("t2.worker.20260102T000000Z.2.json",
                      {"total_cost_usd": None, "agent_call": {"elapsed_s": 10.0}})
        got = watchdog_mod.ledger(self.root / "usage")
        self.assertIsNone(got["estimated_unpriced_usd"])
        self.assertEqual(got["unpriced_seconds"], 10.0)

    def test_an_envelope_name_is_read_from_its_right_end(self):
        self.assertEqual(watchdog_mod.envelope_name(
            "run.with.dots-1.worker.20260101T000000Z.77.json"),
            {"task_id": "run.with.dots-1", "role": "worker", "step": None,
             "stamp": "20260101T000000Z", "pid": 77})
        self.assertEqual(watchdog_mod.envelope_name(
            "t-1.reviewer.skeptic.20260101T000000Z.9.json")["step"], "skeptic")
        self.assertIsNone(watchdog_mod.envelope_name("not-an-envelope.json"))

    # -- the gate log ---------------------------------------------------------------

    def test_only_a_validation_call_counts_toward_the_rate(self):
        log = self.root / "gate-log" / "r.jsonl"
        log.parent.mkdir(parents=True)
        log.write_text("".join(json.dumps(row) + "\n" for row in (
            {"changed_files": True, "pre_merge": False, "ok": False},
            {"changed_files": True, "pre_merge": False, "ok": True},
            {"changed_files": False, "ok": False},          # an agent's own check
            {"changed_files": True, "pre_merge": True, "ok": False},   # the batch re-check
        )), encoding="utf-8")
        counts = watchdog_mod.gate_counts(self.root / "gate-log")
        self.assertEqual(counts, {"calls": 2, "failed": 1,
                                  "pre_merge_calls": 1, "pre_merge_failed": 1})

    # -- the rules ------------------------------------------------------------------

    def test_the_cap_drains(self):
        reasons = watchdog_mod.decide(dollars=100.0, max_cost=100.0, calls=0, failed=0,
                                      merged=None, p90_cost=None, p90_gate=None,
                                      multiple=2.0)
        self.assertEqual(len(reasons), 1)

    def test_the_per_task_rule_is_held_back_below_min_tasks(self):
        common = dict(dollars=100.0, max_cost=1000.0, calls=0, failed=0, p90_cost=1.0,
                      p90_gate=None, multiple=2.0)
        self.assertEqual(watchdog_mod.decide(merged=4, **common), [])
        self.assertEqual(len(watchdog_mod.decide(merged=5, **common)), 1)

    def test_the_per_task_rule_does_not_fire_on_an_unreadable_merge_target(self):
        self.assertEqual(watchdog_mod.decide(dollars=1e6, max_cost=1e9, calls=0, failed=0,
                                             merged=None, p90_cost=0.01, p90_gate=None,
                                             multiple=2.0), [])

    def test_the_gate_rate_rule_is_held_back_below_min_calls(self):
        common = dict(dollars=1.0, max_cost=1000.0, failed=4, p90_cost=None,
                      p90_gate=0.05, multiple=2.0, merged=None)
        self.assertEqual(watchdog_mod.decide(calls=4, **common), [])
        self.assertEqual(len(watchdog_mod.decide(calls=20, failed=20, max_cost=1000.0,
                                                 dollars=1.0, p90_cost=None, p90_gate=0.05,
                                                 multiple=2.0, merged=None)), 1)

    # -- the drain and the settle ---------------------------------------------------

    def test_a_drain_is_followed_by_settling_until_the_spend_stops_moving(self):
        self.priced(1, 60.0)
        args = Args(root=str(self.root), max_cost=50.0, settle_checks=2)
        record = watchdog_mod.check(args)
        self.assertTrue(record["drained"])
        self.assertFalse(record["settled"])
        final = watchdog_mod.settle(args, record, sleep=lambda _s: None)
        self.assertTrue(final["settled"])
        self.assertEqual(final["spend_after_drain_usd"], 60.0)
        self.assertTrue(final["over_cap_after_drain"])

    def test_settling_sees_what_the_calls_in_flight_spent_after_the_drain(self):
        self.priced(1, 60.0)
        args = Args(root=str(self.root), max_cost=100.0, settle_checks=1)
        record = watchdog_mod.check(args)
        self.assertFalse(record["drained"])    # under the cap
        # now a call in flight lands after a drain would have been ordered
        calls = {"n": 0}

        def land(_seconds: float) -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                self.priced(2, 70.0)

        drained = {**record, "drained": True, "reasons": ["forced"]}
        final = watchdog_mod.settle(args, drained, sleep=land)
        self.assertEqual(final["spend_after_drain_usd"], 130.0)
        self.assertTrue(final["over_cap_after_drain"])
        self.assertEqual(final["spend_at_drain_usd"], 60.0)

    def test_main_exits_three_when_the_cap_is_still_over_after_the_drain(self):
        self.priced(1, 60.0)
        code = watchdog_mod.main(["--root", str(self.root), "--max-cost", "50",
                                  "--dry-run", "--settle-checks", "1",
                                  "--settle-interval", "0", "--settle-timeout", "1"],
                                 sleep=lambda _s: None)
        self.assertEqual(code, watchdog_mod.EXIT_OVER_CAP)

    def test_main_needs_a_cap(self):
        self.assertEqual(watchdog_mod.main(["--root", str(self.root)]),
                         watchdog_mod.EXIT_USAGE)
        self.assertEqual(watchdog_mod.main(["--root", str(self.root), "--max-cost", "0"]),
                         watchdog_mod.EXIT_USAGE)

    def test_no_settle_leaves_the_cap_unenforced_and_says_which_it_chose(self):
        self.priced(1, 60.0)
        code = watchdog_mod.main(["--root", str(self.root), "--max-cost", "50",
                                  "--dry-run", "--no-settle"], sleep=lambda _s: None)
        self.assertEqual(code, watchdog_mod.EXIT_DRAINED)
        record = json.loads((self.root / "watchdog.json").read_text())
        self.assertFalse(record["settled"])


class AgentTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "root"
        (self.root / "work" / "t1").mkdir(parents=True)

    def test_a_dry_run_spends_nothing_and_still_leaves_an_envelope(self):
        envelope = agent_mod.call(root=self.root, task_id="t1", role="worker",
                                  prompt="do the thing")
        self.assertTrue(envelope["dry_run"])
        self.assertEqual(len(list((self.root / "usage").glob("*.json"))), 1)

    def test_a_stub_worker_runs_a_local_command_and_is_recorded_unpriced(self):
        envelope = agent_mod.call(root=self.root, task_id="t1", role="worker",
                                  prompt="", worker_command="true")
        self.assertIsNone(envelope["total_cost_usd"])
        self.assertEqual(envelope["agent_call"]["exit"], 0)
        block = cost_mod.cost_block(self.root / "usage")
        self.assertEqual(block["unpriced_calls"], 1)

    def test_the_envelope_name_is_the_one_the_watchdog_reads(self):
        path = agent_mod.envelope_path(self.root, "loc-1-enwiki-1-1-2", "reviewer")
        self.assertEqual(watchdog_mod.envelope_name(path.name)["task_id"],
                         "loc-1-enwiki-1-1-2")

    def test_the_options_are_the_repositorys_sandboxed_ones(self):
        try:
            import claude_agent_sdk  # noqa: F401, PLC0415
        except ImportError:
            self.skipTest("claude-agent-sdk is not installed")
        import agent_sandbox  # noqa: PLC0415

        opts = agent_mod.options(model=None, system_prompt="s", cwd=None,
                                 tools=agent_mod.REVIEWER_TOOLS)
        self.assertEqual(opts.permission_mode, agent_sandbox.PERMISSION_MODE)
        self.assertNotIn("Bash", opts.tools)
        self.assertIn("Write", opts.disallowed_tools)
        self.assertEqual(opts.setting_sources, [])

    def test_a_worker_writes_in_its_findings_directory_and_nowhere_else(self):
        try:
            import claude_agent_sdk  # noqa: F401, PLC0415
        except ImportError:
            self.skipTest("claude-agent-sdk is not installed")
        import agent_sandbox  # noqa: PLC0415

        findings = self.root / "work" / "t1" / "findings" / "t1"
        findings.mkdir(parents=True)
        opts = agent_mod.options(model=None, system_prompt="s",
                                 cwd=self.root / "work" / "t1", write_dir=findings)
        self.assertIn("Write", opts.tools)
        self.assertNotIn("Write", opts.disallowed_tools)
        self.assertIn("Bash", opts.disallowed_tools)
        self.assertEqual(agent_sandbox.tool_decision(
            "Write", {"file_path": "a.json"}, write_dir=findings,
            tools=opts.tools)[0], agent_sandbox.ALLOW)
        self.assertEqual(agent_sandbox.tool_decision(
            "Write", {"file_path": str(self.root / "a.json")}, write_dir=findings,
            tools=opts.tools)[0], agent_sandbox.DENY)


class EndToEndTests(unittest.TestCase):
    """plan -> a stub worker -> the gate -> harvest -> the tally, with no model called."""

    def test_a_synthetic_task_runs_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = Workspace(root / "ws")
            run_root = root / "run"
            planned = workspace.plan(run_root, per_task=8)
            self.assertEqual(len(planned["tasks"]), 1)
            task = planned["tasks"][0]["task_id"]

            # the stub worker: a command that writes the task's document, as a
            # contributor's would, run through the same agent entry a model call takes
            document = fixture.good_locate_document()
            document["run_id"] = fixture.RUN
            document["task_id"] = task
            # a worker answers exactly the passages its task was handed; the inventory's
            # prose passage is scoped out and was never planned
            handed = {p["item"] for p in planned["tasks"][0]["documents"][0]["passages"]}
            document["passages"] = [p for p in document["passages"] if p["item"] in handed]
            worktree = run_root / "work" / task
            (worktree / "findings" / task).mkdir(parents=True, exist_ok=True)
            payload = json.dumps(document, sort_keys=True)
            script = root / "stub-worker.py"
            script.write_text(
                "import pathlib, sys\n"
                f"out = pathlib.Path({str(worktree / 'findings' / task / (fixture.SOURCE + '.json'))!r})\n"
                f"out.write_text({payload!r}, encoding='utf-8')\n", encoding="utf-8")
            envelope = agent_mod.call(root=run_root, task_id=task, role="worker", prompt="",
                                      worker_command=f"{sys.executable} {script}")
            self.assertEqual(envelope["agent_call"]["exit"], 0, envelope)

            # the gate, out of tree, over the worker's own document
            code = gate.main(["--gate-config", str(run_root / "gate" / "gate.json"),
                              "--root", str(worktree),
                              "--findings-dir", str(worktree / "findings")],
                             env={"TEND_TASK_ID": task})
            self.assertEqual(code, gate.EXIT_OK)

            # the harvest: the documents come home with a receipt
            receipt = harvest_mod.harvest(
                run_id=fixture.RUN, source=worktree / "findings",
                dest=root / "corpus" / fixture.RUN, usage=run_root / "usage",
                gate_log=run_root / "gate-log",
                run_config=json.loads((run_root / "run.json").read_text()))
            self.assertTrue(receipt["complete"], harvest_mod.describe(receipt))
            self.assertEqual(receipt["actual"]["count"], 1)
            self.assertEqual(receipt["cost"]["unpriced_calls"], 1)

            # the tally: outcome rows out of the harvested document
            shutil.copytree(workspace.corpus / fixture.INV_RUN,
                            root / "corpus" / fixture.INV_RUN)
            runs = root / "runs"
            (runs / fixture.RUN).mkdir(parents=True)
            shutil.copyfile(run_root / "run.json", runs / fixture.RUN / "run.json")
            shutil.copyfile(run_root / "planned.json", runs / fixture.RUN / "planned.json")
            (runs / fixture.INV_RUN).mkdir(parents=True)
            _write(runs / fixture.INV_RUN / "run.json",
                   {"run_id": fixture.INV_RUN, "mode": vocab.MODE_INVENTORY,
                    "locate": {"mode": vocab.MODE_INVENTORY,
                               "status_rule": vocab.STATUS_RULE_BROAD}})
            import index as index_mod  # noqa: PLC0415

            built = tally_mod.build(
                tally_mod.Workspace(root / "corpus", runs, workspace.sources), fixture.RUN,
                universe=tally_mod.Universe.load(workspace.universe),
                declarations=index_mod.JsonlIndex.load(workspace.index))
            self.assertEqual(built["counts"]["by_outcome"], {"located": 2, "none": 1})
            self.assertEqual(built["counts"]["problems"], 0)
            rows, refused = tally_mod.export_rows(built, tally_mod.Workspace(
                root / "corpus", runs, workspace.sources))
            self.assertEqual(refused, [])
            self.assertEqual(len(rows), 2)
            self.assertTrue(all(row["cut"]["span_sha256"] for row in rows))


if __name__ == "__main__":
    unittest.main(verbosity=2)
