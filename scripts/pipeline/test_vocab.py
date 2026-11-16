#!/usr/bin/env python3
"""Tests for scripts/pipeline/vocab.py, the locate vocabulary.

New code. The producer writes these values and the consumer checks them, so the test is
that the two lists are one list: every vocabulary, key tuple and pattern the pipeline
writes by is pinned against scripts/articles/build_store.py's. A value added on one side
fails here until it is added on the other.

Run: python3 scripts/pipeline/test_vocab.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "scripts" / "articles"))

import build_store  # noqa: E402
import vocab  # noqa: E402

# Every name the two modules must spell the same way. Each is a vocabulary the record
# format fixes, a tuple of keys a document may carry, or a pattern a value is held to.
SHARED = [
    "LOCATE", "SEAL_SCHEMA", "SEAL_SCHEMA_BUNDLE",
    "STATUS_RULE_D1", "STATUS_RULE_BUNDLE", "STATUS_RULE_BROAD", "BUNDLE_STATUS_RULES",
    "KINDS", "THEOREM_KINDS", "DEFINITION_KINDS", "EITHER_KINDS",
    "PROMINENCES", "LOCATION_RELATIONS", "PART_RELATIONS", "DATA_MATCH_RELATIONS",
    "PROOF_STATUSES", "UNPROVED", "COMPLETE",
    "STATUSES", "STATUS_RANK", "CHALLENGES", "CONFIRMED_WITH_CLAIM", "CONFIDENCES",
    "COVERAGE_KEYS", "COVERAGE_KEYS_BUNDLE", "BUNDLE_RELATIONS", "CONJUNCT_KEYS",
    "MIN_CONJUNCTS", "MAX_CONJUNCTS", "MAX_CONJUNCT_CHARS",
    "COVERAGE_RELATION", "MIN_COVERAGE_PARTS", "COVERAGE_ACTS_AS", "COVERAGE_BASIS",
    "AFFIX_MAX_CHARS", "MAX_LOCATIONS", "MIN_ABSENCE_QUERIES",
    "ABSENCE_END_KEYS", "RECEIPT_KEYS",
    "RECORD_EXT_KEYS", "RECORD_EXT_OPTIONAL", "RECORD_EXT_KEYS_BUNDLE",
    "SHAPE_EXT_KEYS", "SHAPE_EXT_KEYS_BUNDLE",
    "SUBJECT_EXT_KEYS", "SUBJECT_EXT_OPTIONAL", "LEAN_EXT_KEYS", "LEAN_EXT_OPTIONAL",
    "SEAL_KEYS", "SEAL_KEYS_BUNDLE", "SEAL_UNIVERSE_KEYS", "SEAL_SOURCESET_KEYS",
    "UNIVERSE_KEYS", "LIBRARY_KEYS", "LIBRARY_OPTIONAL", "EXCLUDED_KEYS",
    "MAX_NAME_CHARS", "MAX_REASON_CHARS",
    "CARRY_SCHEMA", "CARRY_FATES", "CARRY_NEW",
]
# Patterns, compared by their source: the same grammar, spelled the same way.
SHARED_PATTERNS = ["HEX40", "HEX64", "ITEM_RE", "SOURCE_ID_RE", "TOKEN_RE", "CAMPAIGN_RE",
                   "CREATED_RE", "DATE_RE", "REPO_RE", "RENDERER_RE", "DISPLAY_NAME_RE"]


class ParityTests(unittest.TestCase):
    def test_every_shared_value_is_the_store_builders(self):
        for name in SHARED:
            with self.subTest(name=name):
                self.assertTrue(hasattr(build_store, name), f"build_store has no {name}")
                self.assertEqual(getattr(vocab, name), getattr(build_store, name))

    def test_every_shared_pattern_is_the_store_builders(self):
        for name in SHARED_PATTERNS:
            with self.subTest(name=name):
                self.assertEqual(getattr(vocab, name).pattern, getattr(build_store, name).pattern)

    def test_the_quote_cap_is_the_store_builders(self):
        # The one name that differs: the producer's cap and the consumer's are one number.
        self.assertEqual(vocab.QUOTE_MAX_WORDS, build_store.QUOTE_MAX_WORDS)

    def test_the_seal_schema_of_each_status_rule(self):
        self.assertEqual(vocab.SEAL_SCHEMAS, {1: "cf-locate-recordset/1",
                                              2: "cf-locate-recordset/2",
                                              3: "cf-locate-recordset/2"})


class MemberTests(unittest.TestCase):
    def test_member_takes_a_string_and_nothing_else(self):
        self.assertTrue(vocab.member("theorem", vocab.KINDS))
        self.assertFalse(vocab.member("lemma", vocab.KINDS))
        # True == 1, so a bool must not pass a membership test over a tuple of numbers.
        self.assertFalse(vocab.member(True, ("theorem",)))
        self.assertFalse(vocab.member(None, vocab.KINDS))

    def test_it_is_the_store_builders_rule(self):
        for value in ("theorem", "lemma", True, 1, None, ["theorem"]):
            with self.subTest(value=value):
                self.assertEqual(vocab.member(value, vocab.KINDS),
                                 build_store.member(value, build_store.KINDS))


class StatusRuleTests(unittest.TestCase):
    def test_it_names_the_rules_it_knows(self):
        for rule in (1, 2, 3):
            self.assertIsNone(vocab.status_rule_problem(rule))
        for bad in (0, 4, "2", 2.0, True, None):
            with self.subTest(status_rule=bad):
                self.assertIsNotNone(vocab.status_rule_problem(bad))


class GrammarTests(unittest.TestCase):
    """The grammars a seal is refused over, in both directions."""

    def test_the_campaign_token(self):
        for good in ("tierA-b1", "run:loc-tierA-b1a+chal-tierA-b1", "run:loc-7", "a.b:c-d"):
            self.assertTrue(vocab.CAMPAIGN_RE.match(good), good)
        for bad in ("", "run:", "_draft", "smoke+2", "run:a+b+c", "run:a/b", "x" * 129):
            self.assertFalse(vocab.CAMPAIGN_RE.match(bad), bad)

    def test_the_record_set_token(self):
        for good in ("tierA-b1a", "wpm3", "u1", "a" * 128):
            self.assertTrue(vocab.TOKEN_RE.match(good), good)
        for bad in ("", "_draft", "smoke+2", "wpm math", "a" * 129):
            self.assertFalse(vocab.TOKEN_RE.match(bad), bad)

    def test_the_repository_url(self):
        for good in ("https://github.com/leanprover-community/mathlib4",
                     "https://gitlab.example.org/group/sub/lean/beta"):
            self.assertTrue(vocab.is_repo_url(good), good)
        for bad in ("https://github.com/owner/repo/", "http://github.com/o/r",
                    "https://github/owner", "https://github.com/a/b/c/d/e",
                    # the grammar admits a dot segment; is_repo_url does not
                    "https://github.com/../repo", "https://github.com/owner/./repo",
                    "", None, 7):
            self.assertFalse(vocab.is_repo_url(bad), bad)

    def test_the_repository_rule_is_the_store_builders(self):
        for uri in ("https://github.com/o/r", "https://github.com/../r", "https://x.y/a/b/c/d/e",
                    "https://github.com/o/r/", None):
            with self.subTest(uri=uri):
                self.assertEqual(vocab.is_repo_url(uri), build_store.is_repo_url(uri))


if __name__ == "__main__":
    unittest.main(verbosity=2)
