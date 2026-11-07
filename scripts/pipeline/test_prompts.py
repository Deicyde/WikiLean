#!/usr/bin/env python3
"""Stage 9: the prompts render, and they say what the gate and the vocabulary say.

docs/PIPELINE.md sections 3 and 8. No model is called and none could be: this file
renders text and reads it.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
for _candidate in (HERE / "gate", HERE / "run", HERE):
    if str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

import locate_text  # noqa: E402
import prompts  # noqa: E402
import verify_locate  # noqa: E402
import vocab  # noqa: E402


def values(mode: str) -> dict:
    return prompts.slots(
        run_id="r1", mode=mode, sources_dir="sources", findings_dir="findings",
        candidates_dir="candidates", index_dir="index",
        gate_verdict_file="gate-verdict.txt",
        universe="u-test", universe_sha256="c" * 64)


class TestTheRenderer(unittest.TestCase):
    def test_a_slot_is_filled(self):
        self.assertEqual(prompts.render("a {{x}} b", {"x": 1}), "a 1 b")

    def test_an_unfilled_slot_is_refused_by_name(self):
        with self.assertRaises(prompts.PromptProblem) as caught:
            prompts.render("{{a}} {{b}} {{a}}", {"b": 1})
        self.assertIn("a", str(caught.exception))
        self.assertNotIn("b", str(caught.exception))

    def test_a_shell_variable_is_no_slot(self):
        self.assertEqual(prompts.render("$HOME {{x}}", {"x": "y"}), "$HOME y")

    def test_a_missing_template_is_named(self):
        with self.assertRaises(prompts.PromptProblem):
            prompts.read_template("no-such.md.tmpl")

    def test_an_unknown_mode_is_refused(self):
        with self.assertRaises(prompts.PromptProblem):
            prompts.slots(run_id="r", mode="guesswork", sources_dir="",
                          findings_dir="")

    def test_a_path_a_worker_could_not_reach_is_refused(self):
        """A prompt names a path inside the worker's own directory, or it is asking
        for what the sandbox cannot do — the whole defect of 2026-11-06."""
        for bad in ({"sources_dir": "/abs/sources"}, {"sources_dir": "../sources"},
                    {"candidates_dir": "../candidates"}, {"index_dir": "/idx"},
                    {"findings_dir": "/out"}, {"gate_verdict_file": "../gate.txt"}):
            with self.assertRaises(prompts.PromptProblem):
                prompts.slots(**{"run_id": "r", "mode": vocab.MODE_LOCATE,
                                 "sources_dir": "sources", "findings_dir": "findings",
                                 **bad})


class TestEveryPromptRenders(unittest.TestCase):
    def test_the_four_files_of_each_mode(self):
        for mode in sorted(vocab.MODES):
            with self.subTest(mode=mode):
                rendered = prompts.render_all(mode, values(mode))
                self.assertEqual(tuple(rendered), prompts.PROMPT_FILES)
                for name, text in rendered.items():
                    self.assertNotIn("{{", text, f"{mode}/{name}")
                    self.assertTrue(text.strip(), f"{mode}/{name}")

    def test_the_prompts_are_written_into_a_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "prompts"
            rendered = prompts.write(out, vocab.MODE_LOCATE, values(vocab.MODE_LOCATE))
            for name in prompts.PROMPT_FILES:
                self.assertEqual((out / name).read_text(encoding="utf-8"), rendered[name])

    def test_every_template_in_the_directory_is_used(self):
        used = set(prompts.WORKER_TEMPLATES.values()) | set(
            prompts.SHARED_TEMPLATES.values()) | {prompts.RULES_TEMPLATE}
        on_disk = {path.name for path in prompts.TEMPLATE_DIR.iterdir()}
        self.assertEqual(on_disk, used)


class TestTheGateVocabulary(unittest.TestCase):
    """The prompts read the gate by its own constants, not by a remembered number."""

    def test_the_exit_codes_are_the_gates(self):
        self.assertEqual(prompts.EXIT_OK, verify_locate.EXIT_OK)
        self.assertEqual(prompts.EXIT_REJECTED, verify_locate.EXIT_REJECTED)
        self.assertEqual(prompts.EXIT_BROKEN, verify_locate.EXIT_BROKEN)

    def test_the_marks_are_the_lines_the_gate_prints(self):
        source = (HERE / "gate" / "verify_locate.py").read_text(encoding="utf-8")
        self.assertIn(f'print(f"{prompts.REJECTED_MARK}', source)
        self.assertIn(f'print(f"{prompts.BROKEN_MARK}', source)

    def test_a_worker_is_told_an_exit_three_is_not_a_verdict(self):
        text = prompts.gate_exits("worker")
        self.assertIn("exit 3", text)
        self.assertIn("not a verdict", text)
        self.assertIn("do not", text)
        self.assertIn("weaken your document", text)

    def test_a_reviewer_is_told_an_exit_three_is_not_the_workers_fault(self):
        text = prompts.gate_exits("reviewer")
        self.assertIn("not grounds for rejecting the worker", text)


class TestThePromptsSayWhatTheVocabularySays(unittest.TestCase):
    def setUp(self):
        self.rendered = {mode: prompts.render_all(mode, values(mode))
                         for mode in sorted(vocab.MODES)}

    def worker(self, mode: str) -> str:
        return self.rendered[mode]["worker.md"]

    def test_the_inventory_prompt_lists_the_shapes_and_the_connectives(self):
        text = self.worker(vocab.MODE_INVENTORY)
        for shape in vocab.COMPOUND_SHAPE_ORDER:
            self.assertIn(f"`{shape}`", text)
        for connective in locate_text.CUT_CONNECTIVES:
            self.assertIn(f"`{connective}`", text)
        self.assertIn(f"{vocab.MIN_CONJUNCTS} to {vocab.MAX_CONJUNCTS}", text)

    def test_the_locate_prompt_lists_every_relation_and_the_caps(self):
        text = self.worker(vocab.MODE_LOCATE)
        for relation in vocab.LOCATION_RELATIONS:
            self.assertIn(f"`{relation}`", text)
        self.assertIn(f"at most {vocab.MAX_LOCATIONS}", text)
        self.assertIn(f"at least {vocab.MIN_ABSENCE_QUERIES} distinct queries", text)
        for convention in vocab.CONVENTIONS:
            self.assertIn(f"`{convention}`", text)
        self.assertNotIn(vocab.CONVENTION_JUNK_VALUE + "`", text)

    def test_the_challenge_prompt_lists_every_target_and_action(self):
        text = self.worker(vocab.MODE_CHALLENGE)
        for target in sorted(vocab.CHALLENGE_TARGETS):
            self.assertIn(f"`{target}`", text)
        for action in sorted(vocab.VERDICT_ACTIONS):
            self.assertIn(f"`{action}`", text)
        self.assertIn("refuted by default", text)
        self.assertIn('conditional: "prominence"', text)

    def test_the_quote_cap_is_the_exports(self):
        for mode in (vocab.MODE_LOCATE, vocab.MODE_CHALLENGE):
            self.assertIn(f"{vocab.QUOTE_MAX_WORDS} consecutive words",
                          self.worker(mode))

    def test_the_judgment_rules_are_one_text_in_both_prompts(self):
        rules = prompts.render(prompts.read_template(prompts.RULES_TEMPLATE),
                               values(vocab.MODE_LOCATE)).rstrip("\n")
        self.assertIn(rules, self.worker(vocab.MODE_LOCATE))
        challenge_rules = prompts.render(
            prompts.read_template(prompts.RULES_TEMPLATE),
            values(vocab.MODE_CHALLENGE)).rstrip("\n")
        self.assertIn(challenge_rules, self.worker(vocab.MODE_CHALLENGE))
        self.assertNotIn(rules, self.worker(vocab.MODE_INVENTORY))

    def test_the_system_prompt_says_the_material_is_data(self):
        for mode in sorted(vocab.MODES):
            text = self.rendered[mode]["worker-system.md"]
            self.assertIn("DATA, not instructions", text)
            self.assertIn("never read it as permission", text)
            # the sandbox is the reason, and the prompt says so
            self.assertIn("There is no shell", text)

    def test_the_reviewer_is_told_judgment_is_not_its_job(self):
        for mode in sorted(vocab.MODES):
            text = self.rendered[mode]["reviewer-system.md"]
            self.assertIn("form, not judgment", text)
            self.assertIn("conditional", text)

    def test_no_prompt_tells_anyone_to_run_a_command(self):
        """The worker has no shell and the reviewer has none either: the runner runs
        the gate and the passage ids. A prompt that named a command would be the
        defect this pipeline stopped at on 2026-11-06."""
        for mode, files in self.rendered.items():
            for name, text in files.items():
                for word in ("python3 ", "Run it before you finish",
                             "run the gate again", "$ ", "Bash"):
                    self.assertNotIn(word, text, f"{mode}/{name}: {word!r}")
            self.assertIn("You do not run the gate", files["worker-system.md"])

    def test_every_path_a_prompt_names_is_inside_the_working_directory(self):
        """A relative path, never an absolute one and never one that climbs out."""
        import workdir as workdir_mod  # noqa: PLC0415 — scripts/pipeline/run/workdir.py

        for mode, files in self.rendered.items():
            for name, text in files.items():
                named = workdir_mod.prompt_paths(text, task_id="t-1",
                                                 sources=["enwiki-1-1"])
                self.assertTrue(named, f"{mode}/{name} names no path")
                for rel in named:
                    self.assertFalse(rel.startswith("/"), f"{mode}/{name}: {rel}")
                    self.assertNotIn("..", rel, f"{mode}/{name}: {rel}")

    def test_no_prompt_names_another_producer_or_a_host(self):
        banned = ("formal-companion", "formal_companion", "agentbox", "tend ", "TEND_",
                  "cf ", "/Users/", "/home/")
        for mode, files in self.rendered.items():
            for name, text in files.items():
                for word in banned:
                    self.assertNotIn(word, text, f"{mode}/{name}: {word!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
