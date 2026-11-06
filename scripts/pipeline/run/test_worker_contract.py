#!/usr/bin/env python3
"""The contract between a planned root, its prompts, and the agent that runs them.

Stage 9 writes the prompts; stage 10 makes the call. On 2026-11-06 a paid run was
stopped here, before its first model call, because the two did not meet: the prompts
named files the worker could not reach, commands it could not run, and a document it
had no tool to write. Each of those was an ``expectedFailure`` in this file. They are
now checks, and they pass — a paid run may start again.

Every check below is on a **real planned root** and the real
``site/agent_sandbox.sandboxed_options``, and **no model is called**: the options
object is built and read, which is free, and the one end-to-end run uses a stub worker
that has only the tools a sandboxed worker has.

The five properties a paid run needs:

1. ``cost.py`` answers on a command line — the spend between shards is read from it;
2. the worker can **write** its document, into one directory and nowhere else;
3. the worker can **read** every file its prompts name, out of its own directory;
4. **nothing** the prompts name has to be *run*: the runner runs the gate and the
   passage ids, and no prompt tells an agent to run anything;
5. the free pre-flight check says so before the first paid call.

Run: python3 scripts/pipeline/run/test_worker_contract.py
"""
from __future__ import annotations

import json
import subprocess
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

import agent  # noqa: E402
import cost  # noqa: E402
import prompts as prompts_mod  # noqa: E402
import test_run as run_fixture  # noqa: E402 — scripts/pipeline/run/test_run.py
import vocab  # noqa: E402
import workdir as workdir_mod  # noqa: E402

try:  # the options are the SDK's dataclass; a host without it checks the prompts only
    import claude_agent_sdk  # noqa: F401
    HAVE_SDK = True
except ImportError:  # pragma: no cover — depends on the host
    HAVE_SDK = False


def _slots(mode: str = vocab.MODE_INVENTORY) -> dict:
    """The prompt slots a planned root gets, as ``plan.plan`` fills them."""
    return prompts_mod.slots(
        run_id="r1", mode=mode, sources_dir=workdir_mod.SOURCES_DIR,
        findings_dir=workdir_mod.FINDINGS, candidates_dir=workdir_mod.CANDIDATES_DIR,
        index_dir=workdir_mod.INDEX_DIR, gate_verdict_file=workdir_mod.GATE_VERDICT,
        universe="u1", universe_sha256="0" * 64)


class CostHasACommandLine(unittest.TestCase):
    """A shard's spend is read between shards, so cost.py is a command and not only
    an import: a plan that calls it as one must not silently print nothing."""

    def test_usage_directory_is_totalled_on_the_command_line(self):
        with tempfile.TemporaryDirectory() as raw:
            usage = Path(raw) / "usage"
            usage.mkdir()
            (usage / "t1.worker.20260101T000000Z.1.json").write_text(
                json.dumps({"total_cost_usd": 1.25, "modelUsage": {
                    "claude-opus-5": {"inputTokens": 10, "outputTokens": 2,
                                      "costUSD": 1.25}}}), encoding="utf-8")
            (usage / "t1.reviewer.20260101T000100Z.2.json").write_text(
                json.dumps({"total_cost_usd": None,
                            "agent_call": {"elapsed_s": 90.0, "why": "timeout"}}),
                encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, str(HERE / "cost.py"), "--usage", str(usage),
                 "--run-id", "r1"], capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("$1.25", proc.stdout)
        # the unpriced call must be said out loud, never counted as a free call
        self.assertIn("LOWER BOUND", proc.stdout)
        self.assertIn("1 call(s) left no priced envelope", proc.stdout)

    def test_a_missing_usage_directory_is_an_error_and_not_a_zero(self):
        with tempfile.TemporaryDirectory() as raw:
            proc = subprocess.run(
                [sys.executable, str(HERE / "cost.py"), "--usage",
                 str(Path(raw) / "nope")], capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")

    def test_the_block_and_the_rendering_agree(self):
        with tempfile.TemporaryDirectory() as raw:
            usage = Path(raw) / "usage"
            usage.mkdir()
            (usage / "t1.worker.20260101T000000Z.1.json").write_text(
                json.dumps({"total_cost_usd": 2.5}), encoding="utf-8")
            block = cost.cost_block(usage)
        self.assertEqual(block["usd"], 2.5)
        self.assertIn("$2.50", "\n".join(cost.render_block(block)))


class WorkerCanDoWhatItsPromptAsks(unittest.TestCase):
    """The prompts a planned root stages, against the options and the staged directory.

    The root is a real one: ``test_run.Workspace`` plans it with the gate suite's
    synthetic source, so the files the prompts name are the files a real run stages.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.ws = run_fixture.Workspace(self.dir / "ws")
        self.search = self.dir / "search"
        self.search.mkdir()
        for name in workdir_mod.SEARCH_FILES:
            (self.search / name).write_text("testlib\tTestlib.decl\tx\n", encoding="utf-8")
        self.root = self.dir / "run"
        planned = self.ws.plan(self.root, per_task=8, render_prompts=True,
                               search=self.search)
        self.task = planned["tasks"][0]["task_id"]
        self.work = self.root / "work" / self.task
        self.prompts = {path.name: path.read_text(encoding="utf-8")
                        for path in (self.root / "prompts").glob("*.md")}

    def worker_options(self):
        """The options agent.py builds for this task; skipped where there is no SDK."""
        if not HAVE_SDK:
            self.skipTest("claude-agent-sdk is not installed on this host")
        return agent.options(model="m", system_prompt="s", cwd=self.work,
                             tools=agent.WORKER_TOOLS,
                             write_dir=workdir_mod.findings_dir(self.root, self.task))

    def test_the_prompts_tell_the_worker_to_write_a_document(self):
        """The premise of the rest: the stage's output is a file the worker writes."""
        text = self.prompts["worker-system.md"] + self.prompts["worker.md"]
        self.assertIn("<source-id>.json", text)

    def test_the_worker_has_a_tool_that_can_write_its_document(self):
        """Repaired. The worker is told to write ``<source-id>.json`` and has Write,
        scoped by the sandbox to the one directory it may write in."""
        options = self.worker_options()
        self.assertIn("Write", options.tools)
        self.assertNotIn("Write", options.disallowed_tools)
        for name in ("Bash", "Edit", "MultiEdit", "WebFetch", "Task"):
            self.assertIn(name, options.disallowed_tools, name)

    def test_the_worker_can_read_the_task_manifest_its_prompt_names(self):
        """Repaired. The prompts name ``task.json``, and the runner stages it in the
        working directory before the call."""
        cwd = Path(self.worker_options().cwd)
        self.assertIn("`task.json`", self.prompts["worker-system.md"])
        self.assertTrue((cwd / "task.json").is_file(),
                        f"{cwd} holds {sorted(p.name for p in cwd.iterdir())}")
        manifest = json.loads((cwd / "task.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["task_id"], self.task)

    def test_the_worker_can_read_every_source_view_its_prompt_names(self):
        """Repaired. ``sources_dir`` is relative and staged: ``text.txt``,
        ``layer.txt`` and the rest are inside the working directory."""
        cwd = Path(self.worker_options().cwd).resolve()
        sources = cwd / workdir_mod.SOURCES_DIR / run_fixture.fixture.SOURCE
        self.assertTrue(sources.is_dir())
        for name in workdir_mod.WORKER_VIEWS:
            self.assertTrue((sources / name).is_file(), name)
            self.assertEqual(cwd, (sources / name).resolve().parents[2])

    def test_the_worker_can_search_the_declarations_its_prompt_names(self):
        cwd = Path(self.worker_options().cwd)
        for name in workdir_mod.SEARCH_FILES:
            self.assertTrue((cwd / workdir_mod.INDEX_DIR / name).is_file(), name)

    def test_no_prompt_names_a_command_the_worker_cannot_run(self):
        """Repaired, the other way round. The prompts name no command at all: the
        gate and the passage ids are the runner's steps, and the system prompt says
        so where it used to say 'Run it before you finish'."""
        for name, text in self.prompts.items():
            self.assertNotIn("python3 ", text, name)
            self.assertNotIn("Run it before you finish", text, name)
        self.assertIn("You do not run the gate", self.prompts["worker-system.md"])
        self.assertIn("There is no shell", self.prompts["worker-system.md"])

    def test_every_path_the_prompts_name_is_in_the_staged_directory(self):
        """The free pre-flight check of the signed plan: before the first paid call,
        is everything the prompt names actually there?"""
        got = workdir_mod.check_prompt_names(
            root=self.root, task_id=self.task,
            prompts=sorted((self.root / "prompts").glob("*.md")))
        self.assertEqual(got["missing"], [])
        self.assertTrue(got["writable_dir_exists"])
        self.assertTrue(got["ok"])
        self.assertIn(f"{workdir_mod.SOURCES_DIR}/{run_fixture.fixture.SOURCE}/text.txt",
                      got["present"])

    def test_the_check_names_a_file_staging_forgot(self):
        """And it fails when it should: the check is not a formality."""
        (self.work / workdir_mod.SOURCES_DIR / run_fixture.fixture.SOURCE
         / "layer.txt").unlink()
        got = workdir_mod.check_prompt_names(
            root=self.root, task_id=self.task,
            prompts=sorted((self.root / "prompts").glob("*.md")))
        self.assertFalse(got["ok"])
        self.assertIn(f"{workdir_mod.SOURCES_DIR}/{run_fixture.fixture.SOURCE}/layer.txt",
                      got["missing"])

    def test_the_run_config_records_what_each_task_was_staged_with(self):
        config = json.loads((self.root / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(set(config["work"]["staged"]), {self.task})
        self.assertGreater(config["work"]["staged"][self.task], 1)
        self.assertTrue(config["work"]["search"])
        self.assertIn("the runner", config["gate"]["run_by"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
