#!/usr/bin/env python3
"""Stage 10: the runner — a stub worker runs a synthetic task through gate and harvest.

**No model is called.** The worker here is a local script that stands in for one, and
it is not an ordinary script: every file it touches goes through :class:`StubWorker`,
which applies the sandbox's own rules — ``site/agent_sandbox.tool_decision`` for a
write, and the ``dontAsk`` working-directory scoping for a read — and raises
:class:`Refused` on anything they deny. So a test that says "the worker read its
manifest" means a worker with Read, Grep, Glob and Write could have, and a test that
says "it was refused" means the sandbox refuses it.

What is held:

* a stub worker with only those four tools runs a synthetic task **end to end**: the
  staged directory, the document, the passage ids, the gate, the reviewer, the
  promotion, the harvest and its receipt;
* **the runner runs the gate**, not the worker: the gate log holds the run's
  validation call, and the worker never had a shell to run one with;
* a worker reading or writing **outside its directory is refused**, by path, by
  ``..``, and through a symlink planted in the staged material;
* a document the gate refuses is handed back with the refusals and retried, and after
  the last attempt it is **not promoted** — the corpus holds gated documents only;
* a file that appears in the working directory outside the staged set and the findings
  directory is reported, so a boundary that stopped holding would be noticed.

Run: python3 scripts/pipeline/run/test_task_runner.py
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
PIPELINE = HERE.parent
REPO = PIPELINE.parents[1]
for _candidate in (HERE, PIPELINE, PIPELINE / "gate", REPO / "site"):
    if str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

import agent_sandbox  # noqa: E402 — site/agent_sandbox.py
import harvest as harvest_mod  # noqa: E402
import task as task_mod  # noqa: E402
import test_gate as fixture  # noqa: E402 — scripts/pipeline/gate/test_gate.py
import test_run as run_fixture  # noqa: E402
import vocab  # noqa: E402
import workdir as workdir_mod  # noqa: E402


class Refused(PermissionError):
    """What the sandbox does to a tool call it will not make."""


class StubWorker:
    """A worker with Read, Grep, Glob and Write, and the sandbox's limits on each.

    This is the contract an SDK worker runs under, in a form a test can drive:

    * **Read, Grep, Glob** are pre-approved inside ``cwd`` and nowhere else — that is
      what ``permission_mode="dontAsk"`` buys, and ``site/test_agent_sandbox.py`` pins
      the options that ask for it;
    * **Write** goes through :func:`agent_sandbox.tool_decision`, the same function the
      PreToolUse hook calls, against the same ``write_dir``.

    There is no fifth method on purpose: a worker has no shell.
    """

    TOOLS = ("Read", "Grep", "Glob", "Write")

    def __init__(self, cwd: Path, write_dir: Path):
        self.cwd = Path(cwd).resolve()
        self.write_dir = Path(write_dir).resolve()

    def _readable(self, path: Path) -> Path:
        target = Path(path)
        if not target.is_absolute():
            target = self.cwd / target
        resolved = target.resolve()
        if not (resolved == self.cwd or self.cwd in resolved.parents):
            raise Refused(f"Read({path}) is outside the working directory {self.cwd}")
        return resolved

    def read(self, path) -> str:
        return self._readable(path).read_text(encoding="utf-8")

    def glob(self, pattern: str) -> list[str]:
        return sorted(str(p.relative_to(self.cwd))
                      for p in self.cwd.glob(pattern)
                      if p.is_file() and self._ok(p))

    def grep(self, pattern: str, path) -> list[str]:
        rx = re.compile(pattern)
        return [line for line in self.read(path).splitlines() if rx.search(line)]

    def write(self, path, content: str) -> None:
        decision, why = agent_sandbox.tool_decision(
            "Write", {"file_path": str(path), "content": content},
            write_dir=self.write_dir, tools=self.TOOLS)
        if decision != agent_sandbox.ALLOW:
            raise Refused(f"Write({path}): {why}")
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def _ok(self, path: Path) -> bool:
        try:
            self._readable(path)
        except Refused:
            return False
        return True


#: The stub worker as a command the runner calls, the way a contributor's own would be.
#: It uses the four methods above and nothing else, so what it can do is what a
#: sandboxed worker can do.
STUB_SOURCE = """\
import json, sys
sys.path[:0] = {paths!r}
import test_task_runner as suite

worker = suite.StubWorker({cwd!r}, {write_dir!r})
manifest = json.loads(worker.read("task.json"))
source_id = manifest["documents"][0]["source_id"]
# the worker reads the article the way its prompt says to, out of its own directory
worker.read("sources/%s/text.txt" % source_id)
worker.read("sources/%s/layer.txt" % source_id)
document = json.loads({document!r})
worker.write(worker.write_dir / ("%s.json" % source_id), json.dumps(document, sort_keys=True))
"""


def _plan(root: Path, workspace, **kw) -> str:
    planned = workspace.plan(root, per_task=8, render_prompts=True, **kw)
    return planned["tasks"][0]["task_id"]


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.ws = run_fixture.Workspace(self.dir / "ws")
        self.root = self.dir / "run"
        self.task = _plan(self.root, self.ws)
        self.work = self.root / "work" / self.task
        self.findings = workdir_mod.findings_dir(self.root, self.task)

    # -- the stub worker ---------------------------------------------------------

    def stub(self) -> StubWorker:
        return StubWorker(self.work, self.findings)

    def document(self, *, good: bool = True) -> dict:
        document = fixture.good_locate_document()
        document["run_id"] = fixture.RUN
        document["task_id"] = self.task
        handed = {p["item"] for p in json.loads(
            (self.root / "tasks" / f"{self.task}.json").read_text()
        )["documents"][0]["passages"]}
        document["passages"] = [p for p in document["passages"] if p["item"] in handed]
        if not good:
            document["source_sha"] = "0" * 64      # rule 1: not the pinned source
        return document

    def command(self, *, good: bool = True) -> str:
        script = self.dir / f"stub-worker-{'good' if good else 'bad'}.py"
        script.write_text(STUB_SOURCE.format(
            paths=[str(HERE), str(PIPELINE), str(PIPELINE / "gate"), str(REPO / "site")],
            cwd=str(self.work), write_dir=str(self.findings),
            document=json.dumps(self.document(good=good))), encoding="utf-8")
        return f"{sys.executable} {script}"

    # -- end to end ---------------------------------------------------------------

    def test_a_stub_worker_runs_a_synthetic_task_through_gate_and_harvest(self):
        record = task_mod.run_task(root=self.root, task_id=self.task,
                                   sources=self.ws.sources, candidates=self.ws.candidates,
                                   worker_command=self.command(), reviewer=False)
        self.assertEqual(len(record["attempts"]), 1, record)
        self.assertTrue(record["gate"]["ok"], record["gate"]["output"])
        self.assertEqual(record["gate"]["exit"], task_mod.GATE_OK)
        self.assertEqual(record["promoted"], [f"{fixture.SOURCE}.json"])
        self.assertEqual(record["wrote_outside"], [])

        # the harvest reads from where the gated documents are, and reconciles
        receipt = harvest_mod.harvest(
            run_id=fixture.RUN, source=self.root / workdir_mod.FINDINGS,
            dest=self.dir / "corpus" / fixture.RUN, usage=self.root / "usage",
            gate_log=self.root / "gate-log" / f"{fixture.RUN}.jsonl",
            run_config=json.loads((self.root / "run.json").read_text()))
        self.assertTrue(receipt["complete"], harvest_mod.describe(receipt))
        self.assertEqual(receipt["actual"]["count"], 1)
        self.assertEqual(sorted(receipt["manifest"]),
                         [f"{self.task}/{fixture.SOURCE}.json"])

    def test_the_runner_ran_the_gate_and_the_worker_had_no_way_to(self):
        task_mod.run_task(root=self.root, task_id=self.task, sources=self.ws.sources,
                          candidates=self.ws.candidates,
                          worker_command=self.command(), reviewer=False)
        log = (self.root / "gate-log" / f"{fixture.RUN}.jsonl").read_text(encoding="utf-8")
        calls = [json.loads(line) for line in log.splitlines() if line.strip()]
        self.assertEqual(len(calls), 1, calls)
        # a validation call: what the watchdog's gate-failure rule and the harvest
        # receipt both count, and what rule 7 runs on
        self.assertTrue(calls[0]["changed_files"])
        self.assertFalse(calls[0]["pre_merge"])
        self.assertEqual(calls[0]["task_id"], self.task)
        self.assertTrue(calls[0]["ok"])
        self.assertFalse(hasattr(self.stub(), "bash"))
        self.assertEqual(StubWorker.TOOLS, ("Read", "Grep", "Glob", "Write"))

    def test_the_gates_own_output_is_left_for_the_reviewer_to_read(self):
        task_mod.run_task(root=self.root, task_id=self.task, sources=self.ws.sources,
                          candidates=self.ws.candidates,
                          worker_command=self.command(), reviewer=False)
        verdict = (self.work / workdir_mod.GATE_VERDICT).read_text(encoding="utf-8")
        self.assertIn("exit 0", verdict)
        self.assertIn("PASS", verdict)
        self.assertEqual(self.stub().read(workdir_mod.GATE_VERDICT), verdict)

    # -- the boundary ---------------------------------------------------------------

    def test_a_worker_reading_outside_its_directory_is_refused(self):
        worker = self.stub()
        self.assertTrue(worker.read("task.json"))
        for path in (str(self.root / "run.json"),
                     str(self.root / "gate" / "gate.json"),
                     str(self.ws.sources / fixture.SOURCE / "layer.txt"),
                     "../../run.json", "../../../ws/universe.json", "/etc/hostname"):
            with self.assertRaises(Refused, msg=path):
                worker.read(path)

    def test_a_worker_writing_outside_its_directory_is_refused(self):
        worker = self.stub()
        worker.write("ok.json", "{}")
        self.assertTrue((self.findings / "ok.json").is_file())
        for path in (str(self.root / "planted.json"),
                     str(self.work / "task.json"),
                     str(self.dir / "planted.json"),
                     "../../task.json", "../../../planted.json"):
            with self.assertRaises(Refused, msg=path):
                worker.write(path, "{}")
        self.assertFalse((self.root / "planted.json").exists())
        self.assertFalse((self.dir / "planted.json").exists())
        self.assertEqual(json.loads((self.work / "task.json").read_text())["task_id"],
                         self.task)

    def test_a_symlink_in_the_staged_material_carries_no_write_out(self):
        link = self.findings / "escape.json"
        link.symlink_to(self.dir / "planted.json")
        with self.assertRaises(Refused):
            self.stub().write(str(link), "{}")
        self.assertFalse((self.dir / "planted.json").exists())

    def test_a_file_outside_the_staged_set_is_reported(self):
        staged = json.loads((self.work / workdir_mod.STAGED_NAME).read_text())
        self.assertEqual(workdir_mod.wrote_outside(self.root, self.task, staged), [])
        (self.work / "stray.txt").write_text("x", encoding="utf-8")
        self.assertEqual(workdir_mod.wrote_outside(self.root, self.task, staged),
                         ["stray.txt"])

    # -- the retry ---------------------------------------------------------------

    def test_a_refused_document_is_retried_with_the_gates_own_refusals(self):
        record = task_mod.run_task(root=self.root, task_id=self.task,
                                   sources=self.ws.sources,
                                   candidates=self.ws.candidates,
                                   worker_command=self.command(good=False),
                                   reviewer=False, max_attempts=2)
        self.assertEqual(len(record["attempts"]), 2)
        for attempt in record["attempts"]:
            self.assertEqual(attempt["gate"]["exit"], task_mod.GATE_REJECTED)
            self.assertIn("VALIDATION FAILED", attempt["gate"]["output"])
        # and what the second call was told
        prompt = task_mod.FEEDBACK_HEAD.format(attempt=2, total=2,
                                               refusals=record["gate"]["output"])
        self.assertIn("attempt 2 of 2", prompt)
        self.assertIn("rule 1", prompt)

    def test_a_document_the_gate_refused_is_never_promoted(self):
        record = task_mod.run_task(root=self.root, task_id=self.task,
                                   sources=self.ws.sources,
                                   candidates=self.ws.candidates,
                                   worker_command=self.command(good=False),
                                   reviewer=False, max_attempts=1)
        self.assertFalse(record["gate"]["ok"])
        self.assertEqual(record["promoted"], [])
        self.assertFalse((self.root / workdir_mod.FINDINGS / self.task).exists())
        # the worker's own document is still on disk, where a person can read it
        self.assertTrue((self.findings / f"{fixture.SOURCE}.json").is_file())

    def test_a_worker_that_writes_nothing_is_recorded_and_not_gated(self):
        record = task_mod.run_task(root=self.root, task_id=self.task,
                                   sources=self.ws.sources,
                                   candidates=self.ws.candidates,
                                   worker_command=f"{sys.executable} -c pass",
                                   reviewer=False, max_attempts=2)
        self.assertEqual(len(record["attempts"]), 2)
        self.assertFalse(record["gate"]["ok"])
        self.assertEqual(record["promoted"], [])
        self.assertIn("no document", record["gate"]["output"])

    # -- the attempt record --------------------------------------------------------

    def test_the_runner_writes_what_it_did(self):
        task_mod.run_task(root=self.root, task_id=self.task, sources=self.ws.sources,
                          candidates=self.ws.candidates,
                          worker_command=self.command(), reviewer=False)
        record = json.loads(
            (self.root / "attempts" / f"{self.task}.json").read_text(encoding="utf-8"))
        self.assertEqual(record["task_id"], self.task)
        self.assertEqual(record["mode"], vocab.MODE_LOCATE)
        self.assertGreater(record["staged"], 5)
        self.assertTrue(record["gate"]["ok"])


#: The inventory stub: it writes the quotes and **not** ``item``, ``head_words`` or
#: ``span``, exactly as the rewritten prompt tells a worker to. If the runner did not
#: key the passages before the gate, every one of them would be refused by rule 2.
INVENTORY_STUB = """\
import json, sys
sys.path[:0] = {paths!r}
import test_task_runner as suite

worker = suite.StubWorker({cwd!r}, {write_dir!r})
manifest = json.loads(worker.read("task.json"))
source_id = manifest["documents"][0]["source_id"]
worker.read("sources/%s/text.txt" % source_id)
document = json.loads({document!r})
worker.write(worker.write_dir / ("%s.json" % source_id), json.dumps(document, sort_keys=True))
"""


class InventoryRunnerTests(unittest.TestCase):
    """Shard A's mode: the worker writes quotes, and the runner keys them.

    An ``In-text #<hex>`` is a hash of the passage's first words on the layer and a
    span is a pair of offsets into it. A model cannot compute either, and the gate
    refuses a document that gets one wrong — so ``gate/passage_ids.py`` runs between
    the worker and the gate, and these tests are what say it does.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.ws = run_fixture.Workspace(self.dir / "ws")
        self.root = self.dir / "run"
        self.task = fixture.INV_TASK
        entry = json.loads(self.ws.manifest.read_text(encoding="utf-8").strip())
        import plan as plan_mod  # noqa: PLC0415

        plan_mod.plan(
            run_id=fixture.INV_RUN, mode=vocab.MODE_INVENTORY, out=self.root,
            universe=self.ws.universe, manifest=self.ws.manifest,
            sources=self.ws.sources, entries={fixture.SOURCE: entry},
            index=self.ws.index, render_prompts=True,
            task_documents=[{"task_id": self.task, "run_id": fixture.INV_RUN,
                             "mode": vocab.MODE_INVENTORY,
                             "documents": [{"source_id": fixture.SOURCE,
                                            "source_sha": entry["html_sha256"],
                                            "layer_sha256": entry["layer_sha256"],
                                            "renderer": entry["renderer"],
                                            "finders": fixture.FINDERS}]}])
        self.work = self.root / "work" / self.task
        self.findings = workdir_mod.findings_dir(self.root, self.task)

    def command(self, *, keyed: bool) -> str:
        document = fixture.good_inventory_document()
        for passage in document["passages"]:
            if not keyed:
                for key in ("item", "head_words", "span"):
                    passage.pop(key, None)
        script = self.dir / f"stub-inv-{keyed}.py"
        script.write_text(INVENTORY_STUB.format(
            paths=[str(HERE), str(PIPELINE), str(PIPELINE / "gate"), str(REPO / "site")],
            cwd=str(self.work), write_dir=str(self.findings),
            document=json.dumps(document)), encoding="utf-8")
        return f"{sys.executable} {script}"

    def test_the_runner_keys_the_passages_the_worker_only_quoted(self):
        record = task_mod.run_task(root=self.root, task_id=self.task,
                                   sources=self.ws.sources,
                                   candidates=self.ws.candidates,
                                   worker_command=self.command(keyed=False),
                                   reviewer=False, max_attempts=1)
        self.assertEqual(record["attempts"][0]["ids"]["exit"], 0,
                         record["attempts"][0]["ids"]["output"])
        self.assertTrue(record["gate"]["ok"], record["gate"]["output"])
        self.assertEqual(record["promoted"], [f"{fixture.SOURCE}.json"])
        written = json.loads((self.findings / f"{fixture.SOURCE}.json").read_text())
        keyed = {p["item"]: p for p in written["passages"]}
        self.assertEqual(sorted(keyed),
                         sorted(p["item"] for p in fixture.INVENTORY_PASSAGES))
        for item, passage in keyed.items():
            original = next(p for p in fixture.INVENTORY_PASSAGES
                            if p["item"] == item)
            self.assertEqual(passage["span"], list(original["span"]))
            self.assertEqual(passage["head_words"], original["head_words"])

    def test_a_quote_that_is_not_on_the_layer_is_named_and_nothing_is_invented(self):
        import passage_ids  # noqa: PLC0415 — scripts/pipeline/gate/passage_ids.py
        import verify_locate as gate_mod  # noqa: PLC0415

        document = fixture.good_inventory_document()
        for passage in document["passages"]:
            for key in ("item", "head_words", "span"):
                passage.pop(key, None)
        document["passages"][0]["quote"] = {"exact": "no such words on this layer",
                                            "prefix": "", "suffix": ""}
        config = self.root / "gate" / "gate.json"
        cfg = gate_mod.GateConfig(gate_mod.read_json(config, "the gate configuration"),
                                  config)
        got = passage_ids.place(document, gate_mod.Context(cfg).view(fixture.SOURCE),
                                span_end_rule=cfg.span_end_rule)
        self.assertEqual(len(got["unplaced"]), 1)
        self.assertIn("occurs 0 times", got["unplaced"][0]["why"])
        self.assertNotIn("item", document["passages"][0])
        self.assertTrue(got["placed"])

    def test_passage_ids_does_nothing_to_a_locate_document(self):
        import passage_ids  # noqa: PLC0415

        other = self.dir / "loc"
        run_fixture.Workspace(self.dir / "ws2").plan(other, per_task=8)
        got = passage_ids.run(other / "gate" / "gate.json", [], write=True)
        self.assertEqual(got["mode"], vocab.MODE_LOCATE)
        self.assertIn("only an inventory", got["skipped"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
