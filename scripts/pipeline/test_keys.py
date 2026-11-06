#!/usr/bin/env python3
"""Tests for scripts/pipeline/keys.py, the revision-free keys and the canonical spelling.

New code. These three byte strings are load-bearing beyond the record format: a reader's
verdict in `atlas_feedback` carries `pair_key`, so a change to any of them re-keys every
vote. So the tests pin the hashes themselves (worked by hand from the definition), pin
them against the consumer's own implementation in scripts/articles/build_store.py, and
check that a value a key cannot be made from is named rather than hashed anyway.

Run: python3 scripts/pipeline/test_keys.py
"""
from __future__ import annotations

import hashlib
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "scripts" / "articles"))

import build_store  # noqa: E402
import keys  # noqa: E402

PAGEID = 712675
SPAN_SHA = "f" * 64
REPO = "https://github.com/leanprover-community/mathlib4"
DECLARATION = "Subalgebra"


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class CanonicalTests(unittest.TestCase):
    """The one JSON spelling: sorted keys, no spaces, UTF-8, no ASCII escaping."""

    def test_keys_are_sorted_and_there_are_no_spaces(self):
        self.assertEqual(keys.canonical({"b": 1, "a": [2, 3]}), '{"a":[2,3],"b":1}')

    def test_non_ascii_is_written_as_itself(self):
        # A passage's quote holds mathematics; escaping it would change the bytes a seal
        # names and the length a byte budget counts.
        self.assertEqual(keys.canonical({"q": "ℝ → ℕ"}), '{"q":"ℝ → ℕ"}')

    def test_a_line_ends_in_one_newline(self):
        self.assertEqual(keys.canonical_line({"a": 1}), '{"a":1}\n')

    def test_it_is_the_spelling_the_store_builder_reads(self):
        # build_store.canonical writes the store's files, so it returns the line's bytes.
        value = {"z": "ℝ", "a": {"c": 2, "b": [1, {"y": None, "x": True}]}}
        self.assertEqual(keys.canonical_line(value).encode("utf-8"), build_store.canonical(value))

    def test_nan_is_refused(self):
        with self.assertRaises(ValueError):
            keys.canonical({"a": float("nan")})


class PassageKeyTests(unittest.TestCase):
    def test_it_is_the_sha_of_the_pageid_and_the_span_sha(self):
        self.assertEqual(keys.passage_key(PAGEID, SPAN_SHA), sha(f"{PAGEID}\n{SPAN_SHA}"))

    def test_the_store_builder_computes_the_same(self):
        self.assertEqual(keys.passage_key(PAGEID, SPAN_SHA), build_store.passage_key(PAGEID, SPAN_SHA))

    def test_a_pageid_that_is_not_a_positive_integer_is_refused(self):
        for bad in (0, -1, True, "712675", 712675.0, None):
            with self.subTest(pageid=bad), self.assertRaises(keys.KeyProblem):
                keys.passage_key(bad, SPAN_SHA)

    def test_a_span_sha_that_is_not_64_hex_is_refused(self):
        for bad in ("", "F" * 64, "f" * 63, None, 1):
            with self.subTest(span=bad), self.assertRaises(keys.KeyProblem):
                keys.passage_key(PAGEID, bad)


class PairKeyTests(unittest.TestCase):
    def setUp(self):
        self.passage = keys.passage_key(PAGEID, SPAN_SHA)

    def test_it_is_the_sha_of_the_three_lines(self):
        self.assertEqual(keys.pair_key(self.passage, REPO, DECLARATION),
                         sha(f"{self.passage}\n{REPO}\n{DECLARATION}"))

    def test_the_store_builder_computes_the_same(self):
        self.assertEqual(keys.pair_key(self.passage, REPO, DECLARATION),
                         build_store.pair_key(self.passage, REPO, DECLARATION))

    def test_the_repository_is_the_end_uri_not_the_library_id(self):
        # A library may be renamed or split and the pair is the same pair.
        self.assertNotEqual(keys.pair_key(self.passage, REPO, DECLARATION),
                            keys.pair_key(self.passage, "mathlib", DECLARATION))

    def test_a_part_that_is_not_a_clean_one_line_string_is_refused(self):
        for bad in ("", " mathlib", "mathlib ", "a\nb", None, 7):
            with self.subTest(part=bad), self.assertRaises(keys.KeyProblem):
                keys.pair_key(self.passage, REPO, bad)
            with self.subTest(repo=bad), self.assertRaises(keys.KeyProblem):
                keys.pair_key(self.passage, bad, DECLARATION)


class BundleKeyTests(unittest.TestCase):
    def setUp(self):
        self.passage = keys.passage_key(PAGEID, SPAN_SHA)
        self.one = keys.pair_key(self.passage, REPO, "One")
        self.two = keys.pair_key(self.passage, REPO, "Two")

    def test_it_is_the_passage_key_and_the_sorted_pair_keys(self):
        self.assertEqual(keys.bundle_key(self.passage, [self.one, self.two]),
                         sha("\n".join([self.passage, *sorted([self.one, self.two])])))

    def test_the_order_the_parts_were_listed_in_does_not_change_it(self):
        self.assertEqual(keys.bundle_key(self.passage, [self.one, self.two]),
                         keys.bundle_key(self.passage, [self.two, self.one]))

    def test_the_store_builder_computes_the_same(self):
        self.assertEqual(keys.bundle_key(self.passage, [self.one, self.two]),
                         build_store.bundle_key(self.passage, [self.one, self.two]))

    def test_fewer_than_two_distinct_parts_is_refused(self):
        for parts in ([], [self.one], [self.one, self.one]):
            with self.subTest(parts=len(parts)), self.assertRaises(keys.KeyProblem):
                keys.bundle_key(self.passage, parts)


class DigestTests(unittest.TestCase):
    def test_text_and_bytes_agree(self):
        self.assertEqual(keys.sha256_text("ℝ"), keys.sha256_bytes("ℝ".encode("utf-8")))

    def test_a_file_is_hashed_by_its_bytes(self):
        self.assertEqual(keys.sha256_file(HERE / "keys.py"),
                         keys.sha256_bytes((HERE / "keys.py").read_bytes()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
