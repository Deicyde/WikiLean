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
from wikilean.judge import (Rejected, Unverifiable, check_exists, clean_note,
                            plan_review, read_review, read_tag, review_prompt,
                            tag_prompt)
from wikilean.store import (Candidate, Overlay, library_roots, module_library,
                            review_candidates, valid_declaration,
                            vouched_declarations)

TOKEN = "wla_" + "C" * 43
LIBRARIES = {"mathlib": fakes.REPO, "lean-core": fakes.CORE_REPO}
ROOTS = library_roots(fakes.universe())


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
        # Nothing is pre-approved when there is nothing to approve.
        self.assertNotIn("--allowedTools", argv)

    def test_with_web_the_child_gets_web_search_and_fetch_and_nothing_else(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(model.CLAUDE_CMD_ENV, None)
            argv = model.claude_argv("claude-opus-5", web=True)
        self.assertEqual(argv[argv.index("--tools") + 1], "WebSearch,WebFetch")
        # Pre-approved, or a print-mode call would be refused each one.
        self.assertEqual(argv[argv.index("--allowedTools") + 1], "WebSearch,WebFetch")
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "")
        self.assertIn("--strict-mcp-config", argv)
        self.assertIn("--no-session-persistence", argv)
        self.assertIn("--safe-mode", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "claude-opus-5")

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
        # macOS adds __CF_USER_TEXT_ENCODING to every process's environment itself; it is not ours.
        self.assertEqual(set(seen["env"]) - set(model.CLAUDE_ENV_KEYS) - {"__CF_USER_TEXT_ENCODING"}, set())

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

    def test_the_child_runs_in_an_empty_directory_removed_afterwards(self):
        """Claude Code tells the model its working directory and that
        directory's git state; a fetch could carry either off."""
        model.ask("judge this", timeout=60)
        seen = json.loads(self.record.read_text())
        cwd = Path(seen["cwd"])
        self.assertNotEqual(cwd.resolve(), Path.cwd().resolve())
        self.assertTrue(cwd.name.startswith("wikilean-claude-"), cwd)
        self.assertEqual(seen["cwd_entries"], [])
        self.assertFalse(cwd.exists())

    def test_the_web_flag_reaches_the_child(self):
        model.ask("judge this", timeout=60, web=True)
        argv = json.loads(self.record.read_text())["argv"]
        self.assertEqual(argv[argv.index("--tools") + 1], model.WEB_TOOLS)
        model.ask("judge this", timeout=60)
        argv = json.loads(self.record.read_text())["argv"]
        self.assertEqual(argv[argv.index("--tools") + 1], "")

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


class ExistenceOracleTest(unittest.TestCase):
    """Which declaration this CLI will write, and in which library.

    The oracle (`GET /decl/<name>`) names the INDEX a declaration was found in,
    not the universe's library: live, `Int.sq_nonneg` comes back
    `{"library": "mathlib", "module": "Init.Data.Int.Pow"}` though it is Lean
    core's. The module, read against the universe's own Lake roots, is the
    signal; the fake below answers in exactly that shape.
    """

    def setUp(self):
        self.asked: list[str] = []
        self.vouched = vouched_declarations(fakes.article())

    def oracle(self, known: dict[str, str]):
        def ask(name: str):
            self.asked.append(name)
            module = known.get(name)
            return {"ok": True, "decl": name, "library": "mathlib", "module": module} if module else None
        return ask

    def check(self, declaration, library, known, *, vouched=None):
        check_exists(declaration, library, LIBRARIES, self.oracle(known), roots=ROOTS,
                     vouched=self.vouched if vouched is None else vouched)

    def test_a_library_outside_the_universe_is_refused_before_anything_is_asked(self):
        with self.assertRaises(Rejected):
            self.check("Fixture.any", "tauceti", fakes.KNOWN_DECLS)
        self.assertEqual(self.asked, [])

    def test_a_declaration_the_store_vouches_for_needs_no_lookup(self):
        self.check("Fixture.settled", "mathlib", {})
        self.assertEqual(self.asked, [], "the sealed store is the stronger evidence")
        self.check("FixtureCore.natSucc", "lean-core", {})
        self.assertEqual(self.asked, [])

    def test_a_name_whose_module_is_the_named_library_s_is_accepted(self):
        self.check("FixtureCore.zeroAdd", "lean-core", fakes.KNOWN_DECLS)
        self.assertEqual(self.asked, ["FixtureCore.zeroAdd"])

    def test_a_name_whose_module_is_another_library_s_is_refused(self):
        # The defect this replaces: /decl says "mathlib" for this name, so a
        # check that read that field saw nothing wrong and wrote it as Mathlib.
        with self.assertRaises(Rejected) as caught:
            self.check("FixtureCore.zeroAdd", "mathlib", fakes.KNOWN_DECLS)
        self.assertIn("belongs to library 'lean-core'", str(caught.exception))
        self.assertNotIsInstance(caught.exception, Unverifiable)

    def test_a_name_in_no_library_of_this_universe_is_refused(self):
        with self.assertRaises(Rejected) as caught:
            self.check("Elsewhere.lemma", "mathlib", fakes.KNOWN_DECLS)
        self.assertIn("no library of this article's universe claims", str(caught.exception))

    def test_a_name_missing_from_a_library_the_index_does_cover_is_refused(self):
        with self.assertRaises(Rejected) as caught:
            self.check("Fixture.hallucinated", "mathlib", fakes.KNOWN_DECLS)
        self.assertIn("published index", str(caught.exception))
        self.assertNotIsInstance(caught.exception, Unverifiable)
        # It established coverage by asking about a name the store vouches for.
        self.assertEqual(self.asked[0], "Fixture.hallucinated")
        self.assertEqual(self.asked[1:], [sorted(self.vouched["mathlib"])[0]])

    def test_a_library_the_index_does_not_cover_is_unverifiable_not_refused(self):
        """The live case: 0 of 19 sampled atlas / tauceti / formal-conjectures
        declarations resolve, including ones the pipeline itself located."""
        mathlib_only = {k: v for k, v in fakes.KNOWN_DECLS.items() if v.startswith("Fixture.")}
        with self.assertRaises(Unverifiable) as caught:
            self.check("FixtureCore.guess", "lean-core", mathlib_only)
        self.assertIn("does not cover library 'lean-core'", str(caught.exception))
        self.assertIn("FixtureCore.natSucc", self.asked)

    def test_a_library_with_nothing_known_in_it_cannot_be_checked_either(self):
        with self.assertRaises(Unverifiable) as caught:
            self.check("FixtureCore.guess", "lean-core", {}, vouched={"mathlib": {"Fixture.settled"}})
        self.assertIn("records no declaration in library 'lean-core'", str(caught.exception))

    def test_a_path_separator_is_the_answer_being_wrong_not_a_failed_request(self):
        """decl.ts NAME_RE refuses `/` and `\\` with a permanent 400, and
        articles-overlay.ts validDeclaration — which store.valid_declaration
        mirrors — allows them. Asking the oracle would fail identically every
        round, so the name is refused here and written off instead."""
        for name in ("Mathlib/Analysis/Foo", "Foo\\bar", "Nat/add_comm"):
            with self.subTest(declaration=name):
                self.asked.clear()
                self.assertTrue(valid_declaration(name), "the premise: the tag route would take it")
                with self.assertRaises(Rejected) as caught:
                    self.check(name, "mathlib", fakes.KNOWN_DECLS)
                self.assertIn("path separator", str(caught.exception))
                self.assertNotIsInstance(caught.exception, Unverifiable)
                self.assertEqual(self.asked, [], "a request that is always a 400 was spent")

    def test_a_vouched_name_is_still_accepted_before_the_shape_is_looked_at(self):
        """The order matters: the sealed store is the stronger evidence, and
        the separator rule is about what the ORACLE can be asked."""
        self.check("Fixture.settled", "mathlib", {})
        self.assertEqual(self.asked, [])

    def test_a_universe_that_names_no_roots_cannot_bind_a_module(self):
        with self.assertRaises(Unverifiable):
            check_exists("Fixture.known", "mathlib", LIBRARIES, self.oracle(fakes.KNOWN_DECLS),
                         roots={}, vouched={})

    def test_a_hit_with_no_module_is_unverifiable_rather_than_accepted(self):
        def no_module(name: str):
            return {"ok": True, "decl": name, "library": "mathlib"}

        with self.assertRaises(Unverifiable):
            check_exists("Fixture.known", "mathlib", LIBRARIES, no_module, roots=ROOTS,
                         vouched=self.vouched)

    def test_roots_are_read_as_lake_globs_on_a_dot_boundary(self):
        roots = {"mathlib": ("Mathlib", "Wanted"), "core": ("Init.*", "Std.*"),
                 "fc": ("FormalConjectures.+",), "ext": ("MathlibExt.+",)}
        cases = {
            "Mathlib.Algebra.Group.Defs": "mathlib",
            "Mathlib": "mathlib",
            "MathlibExt.AlgebraicGeometry.CubicSurface": "ext",
            "Init.Prelude": "core",
            "Std": "core",
            "FormalConjectures.ErdosProblems.X": "fc",
            "FormalConjectures": None,          # ".+" is strictly below
            "Mathlibre.Something": None,        # not a dot boundary
            "Cslib.Foo": None,
            "": None,
        }
        for module, expected in cases.items():
            with self.subTest(module=module):
                self.assertEqual(module_library(module, roots), expected)

    def test_the_longest_matching_root_wins(self):
        roots = {"wide": ("Foo",), "narrow": ("Foo.Bar",)}
        self.assertEqual(module_library("Foo.Bar.Baz", roots), "narrow")
        self.assertEqual(module_library("Foo.Other", roots), "wide")


class PairKeyTest(unittest.TestCase):
    """The pair key goes into a request PATH, so it is not copied on trust."""

    def _candidate(self, pair_key: str) -> Candidate:
        article = fakes.article()
        passage = next(p for p in article["passages"] if p["item"] == "downgraded")
        location = dict(passage["locations"][0], pair_key=pair_key)
        return Candidate("review", "downgraded", article["pageid"], article["title"],
                         article["campaign"], passage, location, 0)

    def removal(self, pair_key: str):
        review = read_review({"verdict": "wrong_link", "reason": "it is about something else"},
                             self._candidate(pair_key), LIBRARIES)
        return plan_review(review, self._candidate(pair_key), LIBRARIES, model="m", run_id="r",
                           overlay=Overlay.of([]))

    def test_a_good_pair_key_removes_the_location(self):
        action = self.removal(fakes.DOWNGRADED_PAIR)
        self.assertEqual(action.path, f"/api/articles/tag/{fakes.DOWNGRADED_PAIR}/state")

    def test_anything_that_is_not_a_digest_is_refused_here_not_at_the_server(self):
        for bad in ("../../etc", "abc?x=1", "f" * 63, "F" * 64, f"{'f' * 64}/x", "f" * 65):
            with self.subTest(bad=bad[:12]):
                with self.assertRaises(Rejected):
                    self.removal(bad)


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

    def test_the_web_note_is_in_a_prompt_only_when_the_call_has_the_tools(self):
        located, absence = candidates()
        for build in (lambda web: review_prompt(located, web=web),
                      lambda web: review_prompt(absence, web=web),
                      lambda web: tag_prompt(located, LIBRARIES, web=web)):
            with_web, without = build(True), build(False)
            self.assertIn("search the web", with_web)
            self.assertIn("is data too", with_web)
            self.assertNotIn("search the web", without)
            # the note comes before the quotation, never inside it (the preamble
            # names the markers too, so look for the block's own opening line)
            self.assertLess(with_web.index("search the web"), with_web.index("\n<passage>\n"))


if __name__ == "__main__":
    unittest.main()
