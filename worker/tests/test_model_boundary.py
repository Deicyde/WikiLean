#!/usr/bin/env python3
"""The model call: its argv, the environment it gets, and what we accept back.

The fake `claude` in fakes.py exits non-zero if it is handed anything named
WIKILEAN_* or ANTHROPIC_*, or if it finds a token in the prompt, so the tests
below fail loudly rather than quietly if that boundary ever moves.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fakes
from wikilean import model
from wikilean.judge import (Rejected, clean_note, read_review, read_tag,
                            review_prompt, tag_prompt)
from wikilean.store import Candidate, Overlay, review_candidates

TOKEN = "wla_" + "C" * 43
LIBRARIES = {"mathlib": fakes.REPO, "lean-core": fakes.CORE_REPO}


def candidates():
    article = fakes.article()
    found = review_candidates(article, Overlay.of([]))
    located = next(c for c in found if c.location is not None)
    absence = next(c for c in found if c.location is None)
    return located, absence


class ArgvTest(unittest.TestCase):
    def test_the_child_is_tool_less_settings_less_and_session_less(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(model.CLAUDE_CMD_ENV, None)
            argv = model.claude_argv("claude-opus-5")
        self.assertEqual(argv[0], "claude")
        self.assertIn("-p", argv)
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "")
        self.assertIn("--strict-mcp-config", argv)
        self.assertIn("--no-session-persistence", argv)
        self.assertIn("--safe-mode", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "claude-opus-5")
        # Whatever else changes, the prompt is not an argument: it goes in on
        # stdin, so no quoted Wikipedia text is visible in `ps`.
        self.assertTrue(all(not a.startswith("A fixture") for a in argv))

    def test_the_environment_is_an_allowlist_without_the_agent_token(self):
        ambient = {
            "PATH": "/tools", "HOME": "/home/volunteer", "CLAUDE_CODE_OAUTH_TOKEN": "claude-secret",
            "WIKILEAN_AGENT_TOKEN": TOKEN, "ANTHROPIC_API_KEY": "sk-should-not-travel",
            "GH_TOKEN": "github-secret", "SOMETHING_ELSE": "ambient",
        }
        with mock.patch.dict(os.environ, ambient, clear=True):
            environment = model.claude_environment()
        self.assertEqual(environment, {"PATH": "/tools", "HOME": "/home/volunteer",
                                       "CLAUDE_CODE_OAUTH_TOKEN": "claude-secret"})
        self.assertNotIn("WIKILEAN_AGENT_TOKEN", model.CLAUDE_ENV_KEYS)
        self.assertNotIn("ANTHROPIC_API_KEY", model.CLAUDE_ENV_KEYS)


class RealChildTest(unittest.TestCase):
    """Run the real `ask` against the fake binary, in a real subprocess."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.record = self.directory / "record.json"
        command = fakes.write_fake_claude(self.directory, record=self.record)
        self.env = mock.patch.dict(os.environ, {
            model.CLAUDE_CMD_ENV: command,
            "WIKILEAN_AGENT_TOKEN": TOKEN,
            "ANTHROPIC_API_KEY": "sk-should-not-travel",
        }, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_the_token_in_our_environment_does_not_reach_the_child(self):
        answer = model.ask("judge this", timeout=60)
        self.assertEqual(answer.value, {"verdict": "unsure"})
        seen = json.loads(self.record.read_text())
        self.assertNotIn("WIKILEAN_AGENT_TOKEN", seen["env"])
        self.assertNotIn("ANTHROPIC_API_KEY", seen["env"])
        self.assertNotIn(TOKEN, json.dumps(seen))
        self.assertEqual(seen["prompt"], "judge this")
        self.assertEqual(set(seen["env"]) - set(model.CLAUDE_ENV_KEYS), set())

    def test_a_prompt_built_from_a_candidate_carries_no_token(self):
        located, absence = candidates()
        for prompt in (review_prompt(located), review_prompt(absence),
                       tag_prompt(located, LIBRARIES)):
            self.assertNotIn("wla_", prompt)
            self.assertNotIn(TOKEN, prompt)
        fakes.set_control(self.directory, reply='{"verdict": "agree"}', record=self.record)
        answer = model.ask(review_prompt(located), timeout=60)
        self.assertEqual(answer.value["verdict"], "agree")
        self.assertIn("<passage>", json.loads(self.record.read_text())["prompt"])

    def test_a_failing_child_raises_rather_than_returning_data(self):
        fakes.set_control(self.directory, fail=True)
        with self.assertRaises(model.ModelError):
            model.ask("judge this", timeout=60)

    def test_a_reply_that_is_not_json_raises(self):
        fakes.set_control(self.directory, reply="I would rather not say.", raw=True)
        with self.assertRaises(model.ModelError):
            model.ask("judge this", timeout=60)

    def test_prose_around_a_json_object_is_tolerated(self):
        fakes.set_control(self.directory, reply='Sure!\n{"verdict": "agree"}\nHope that helps.')
        self.assertEqual(model.ask("judge this", timeout=60).value, {"verdict": "agree"})

    def test_a_timeout_raises(self):
        def slow(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="claude", timeout=1)

        with self.assertRaises(model.ModelError):
            model.ask("judge this", timeout=1, runner=slow)


class AnswerValidationTest(unittest.TestCase):
    """What the CLI refuses to turn into a request."""

    def setUp(self):
        self.located, self.absence = candidates()

    def test_an_out_of_vocabulary_verdict_is_refused(self):
        for bad in ({"verdict": "looks fine"}, {"verdict": ""}, {}, {"verdict": 3},
                    {"verdict": "AGREE"}):
            with self.subTest(bad=bad):
                with self.assertRaises(Rejected):
                    read_review(bad, self.located, LIBRARIES)

    def test_an_out_of_vocabulary_relation_is_refused(self):
        with self.assertRaises(Rejected):
            read_review({"verdict": "wrong_relation", "relation": "sortOfMatch", "reason": "x"},
                        self.located, LIBRARIES)
        with self.assertRaises(Rejected):
            read_tag({"declaration": "Fixture.known", "library": "mathlib",
                      "relation": "veryCloseMatch"}, LIBRARIES)

    def test_a_relation_the_pipeline_already_recorded_is_not_a_correction(self):
        with self.assertRaises(Rejected):
            read_review({"verdict": "wrong_relation", "relation": "broadMatch", "reason": "x"},
                        self.located, LIBRARIES)

    def test_a_library_outside_the_universe_is_refused(self):
        with self.assertRaises(Rejected):
            read_tag({"declaration": "Fixture.known", "library": "tauceti",
                      "relation": "exactMatch"}, LIBRARIES)

    def test_a_lean_name_with_digits_primes_and_subscripts_is_accepted(self):
        for good in ("Nat.succ_le_succ", "foo'", "x₁.bar", "Mathlib.Algebra.Group.Defs.mul_one"):
            with self.subTest(good=good):
                tag = read_tag({"declaration": good, "library": "mathlib",
                                "relation": "exactMatch"}, LIBRARIES)
                self.assertEqual(tag.declaration, good)

    def test_a_declaration_that_is_not_a_lean_name_is_refused(self):
        for bad in ("Fixture .known", "Fixture..known", ".known", "known.", "", "x" * 241,
                    "Fixture\nknown", "Fixture\u0000known",
                    # Unicode format characters: \p{C} on the server too, and
                    # the place a hidden character would be smuggled in.
                    "Fixture​known", "Fixture‮known", "Fixture known"):
            with self.subTest(bad=bad[:16]):
                with self.assertRaises(Rejected):
                    read_tag({"declaration": bad, "library": "mathlib", "relation": "exactMatch"},
                             LIBRARIES)

    def test_a_correction_without_a_reason_is_refused(self):
        with self.assertRaises(Rejected):
            read_review({"verdict": "wrong_link"}, self.located, LIBRARIES)

    def test_a_verdict_that_does_not_fit_the_candidate_is_refused(self):
        # "missed" is for an absence, "wrong_link" for a location.
        with self.assertRaises(Rejected):
            read_review({"verdict": "missed", "declaration": "Fixture.known", "library": "mathlib",
                         "relation": "exactMatch", "reason": "x"}, self.located, LIBRARIES)
        with self.assertRaises(Rejected):
            read_review({"verdict": "wrong_link", "reason": "x"}, self.absence, LIBRARIES)

    def test_pasted_lean_source_is_refused_in_a_note(self):
        for bad in ("theorem foo : 1 = 1 := rfl", "```lean\nrfl\n```", "it is `def foo := 2`",
                    "abbrev x ≔ 2"):
            with self.subTest(bad=bad[:20]):
                with self.assertRaises(Rejected):
                    clean_note(bad, field="note")

    def test_ordinary_english_about_lean_is_not_mistaken_for_source(self):
        # "theorem", "lemma" and "example" are English words a good note uses;
        # refusing them would throw away correct answers.
        for good in ("Mathlib states this theorem for a general monoid",
                     "the lemma is the same up to the usual convention",
                     "for example, it covers the finite case"):
            with self.subTest(good=good[:24]):
                self.assertEqual(clean_note(good, field="note"), good)

    def test_an_over_long_note_is_refused_rather_than_truncated(self):
        with self.assertRaises(Rejected):
            clean_note("x" * 501, field="note")
        self.assertEqual(clean_note("  a   sentence  ", field="note"), "a sentence")

    def test_a_good_answer_is_accepted(self):
        review = read_review({"verdict": "wrong_relation", "relation": "closeMatch",
                              "reason": "the Lean statement matches up to the usual convention"},
                             self.located, LIBRARIES)
        self.assertEqual((review.verdict, review.relation), ("wrong_relation", "closeMatch"))
        tag = read_tag({"declaration": "Fixture.known", "library": "mathlib",
                        "relation": "exactMatch", "note": "states the same thing"}, LIBRARIES)
        self.assertFalse(tag.skip)
        self.assertEqual(tag.declaration, "Fixture.known")
        self.assertTrue(read_tag({"skip": True, "reason": "nothing states it"}, LIBRARIES).skip)


class PromptTest(unittest.TestCase):
    def test_the_prompt_frames_the_quotation_as_data(self):
        located, absence = candidates()
        prompt = review_prompt(located)
        self.assertIn("<passage>", prompt)
        self.assertIn("not an instruction", prompt)
        self.assertIn("exactMatch", prompt)
        self.assertIn(fakes.DOWNGRADED_DECL, prompt)
        self.assertIn("DOWNGRADED", review_prompt(located))
        self.assertIn("absence", review_prompt(absence).lower())

    def test_the_tag_prompt_offers_only_this_universe_s_libraries(self):
        located, _ = candidates()
        prompt = tag_prompt(located, LIBRARIES)
        offered = prompt.split("use an id from this list verbatim):\n", 1)[1].splitlines()[0]
        self.assertEqual(sorted(part.strip() for part in offered.split(",")), ["lean-core", "mathlib"])
        self.assertNotIn("tauceti", prompt)


if __name__ == "__main__":
    unittest.main()
