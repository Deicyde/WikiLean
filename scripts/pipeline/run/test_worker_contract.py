#!/usr/bin/env python3
"""The contract between a planned root, its prompts, and the agent that runs them.

Stage 9 writes the prompts; stage 10 makes the call. Nothing holds the two together,
and they do not currently meet: a run planned by :mod:`plan` and called by :mod:`agent`
hands a worker a prompt naming files it cannot reach and tools it does not have. Every
check below is on a real planned root and the real
``site/agent_sandbox.sandboxed_options``, and **no model is called**: the options object
is built and read, which is free.

The four properties a paid run needs, one test each:

1. ``cost.py`` answers on a command line — the spend between shards is read from it.
2. the worker can **write** its document, which is the whole output of the stage;
3. the worker can **read** the files its prompts name — its task manifest and the
   source's views;
4. the worker can **run** the gate and the index tool its prompts tell it to run.

Only (1) holds. (2), (3) and (4) are marked ``expectedFailure``: the defect is real and
this file is where it is recorded, so that whoever repairs it gets an *unexpected
success* here rather than silence. **Do not spend on a run of stage 9 until they pass.**
Repairing (2) means giving a worker a write tool inside its own working directory,
which moves ``site/agent_sandbox``'s boundary and is not a change to make quietly:
that boundary is why the worker reads untrusted article text safely at all.

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
import vocab  # noqa: E402

try:  # the options are the SDK's dataclass; a host without it checks the prompts only
    import claude_agent_sdk  # noqa: F401
    HAVE_SDK = True
except ImportError:  # pragma: no cover — depends on the host
    HAVE_SDK = False

#: what the planner renders the worker's task directory as (plan.py)
TASKS_DIR = "tasks"


def _slots() -> dict:
    """The prompt slots a planned root gets, as ``plan.plan`` fills them."""
    return prompts_mod.slots(
        run_id="r1", mode=vocab.MODE_INVENTORY, sources_dir="/elsewhere/sources",
        tasks_dir=TASKS_DIR, candidates_dir="",
        gate_command="python3 gate/verify_locate.py --gate-config gate/gate.json",
        ids_command="python3 gate/passage_ids.py",
        index_command="python3 gate/index_search.py",
        universe="u1", universe_sha256="0" * 64)


def _rendered() -> dict[str, str]:
    return prompts_mod.render_all(vocab.MODE_INVENTORY, _slots())


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
    """The prompts a planned root stages against the options agent.py builds."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "run"
        self.task = "inv-source-1"
        # exactly what plan.plan leaves behind: the manifest at the root, work/ empty
        (self.root / "work" / self.task).mkdir(parents=True)
        (self.root / TASKS_DIR).mkdir(parents=True)
        (self.root / TASKS_DIR / f"{self.task}.json").write_text("{}", encoding="utf-8")
        self.prompts = _rendered()

    def worker_options(self):
        """The options agent.py builds for this task; skipped where there is no SDK."""
        if not HAVE_SDK:
            self.skipTest("claude-agent-sdk is not installed on this host")
        return agent.options(model="m", system_prompt="s",
                             cwd=self.root / "work" / self.task)

    def test_the_prompts_tell_the_worker_to_write_a_document(self):
        """The premise of the three below: the stage's output is a file the worker writes."""
        text = self.prompts["worker-system.md"] + self.prompts["worker.md"]
        self.assertIn("<source-id>.json", text)

    @unittest.expectedFailure
    def test_the_worker_has_a_tool_that_can_write_its_document(self):
        """DEFECT. The worker is told to write ``<source-id>.json`` and is given
        Read, Grep and Glob, with Write and Edit denied: it has no way to produce the
        one artefact the stage exists for. A paid call buys nothing."""
        options = self.worker_options()
        writers = {"Write", "Edit", "MultiEdit"}
        self.assertTrue(writers & set(options.tools),
                        f"worker tools are {options.tools} and cannot write a file")
        self.assertFalse(writers & set(options.disallowed_tools))

    @unittest.expectedFailure
    def test_the_worker_can_read_the_task_manifest_its_prompt_names(self):
        """DEFECT. The prompts name the manifest at ``tasks/<task id>.json``, relative
        to the worker's working directory; ``plan`` writes it at the run root and
        creates ``work/<task id>/`` empty, and the sandbox scopes reads to cwd. The
        worker cannot open the file that says what its task is."""
        cwd = Path(self.worker_options().cwd)
        self.assertIn(f"{TASKS_DIR}/", self.prompts["worker-system.md"])
        self.assertTrue((cwd / TASKS_DIR / f"{self.task}.json").is_file(),
                        f"{cwd} holds {sorted(p.name for p in cwd.iterdir())}")

    @unittest.expectedFailure
    def test_the_worker_can_read_the_source_views_its_prompt_names(self):
        """DEFECT. ``sources_dir`` is rendered as an absolute path outside the worker's
        working directory, and the sandbox pre-approves Read, Grep and Glob only inside
        it. Every view the prompt sends the worker to — ``text.txt``, ``layer.txt``,
        ``sections.json`` — is unreachable."""
        cwd = Path(self.worker_options().cwd).resolve()
        sources = Path(_slots()["sources_dir"])
        self.assertTrue(sources.is_absolute())
        self.assertTrue(cwd == sources or cwd in sources.parents,
                        f"the prompt sends the worker to {sources}, outside its cwd {cwd}")

    @unittest.expectedFailure
    def test_the_worker_can_run_the_commands_its_prompt_names(self):
        """DEFECT. The same system prompt says 'There is no shell: you cannot run a
        command' and then tells the worker to run ``python3 gate/verify_locate.py`` and
        ``python3 gate/index_search.py``. Both are true of the options — there is no
        Bash — so the gate is never run before a document is finished, and the index is
        never searched."""
        options = self.worker_options()
        text = self.prompts["worker-system.md"]
        named = [line for line in text.splitlines() if "python3 gate/" in line]
        self.assertTrue(named, "no prompt names a command; nothing to check")
        self.assertIn("Bash", options.tools,
                      f"the prompt names {len(named)} command(s) the worker cannot run")


if __name__ == "__main__":
    unittest.main(verbosity=2)
