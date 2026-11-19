#!/usr/bin/env python3
"""Offline tests of the acquisition step: pinning, politeness and the one-build rule."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from scripts.articles_brain import fixtures  # noqa: E402
from scripts.articles_brain.canon import read_json, sha256_bytes  # noqa: E402
from scripts.articles_brain.fetch import (  # noqa: E402
    PINS_SCHEMA, USER_AGENT, FetchError, fetch,
)


class Sleeper:
    def __init__(self):
        self.waits: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


def run(inputs: str, opener=None, pins: str | None = None, sleep=None) -> dict:
    return fetch(inputs, pins, opener or fixtures.FakeOpener(), site=fixtures.SITE,
                 delay=0.25, sleep=sleep or Sleeper())


class FetchTest(unittest.TestCase):
    def test_the_user_agent_is_generic(self):
        self.assertEqual(USER_AGENT, "WikiLean-articles-brain/1")

    def test_a_run_pins_every_file_it_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            pins = run(tmp)
            self.assertEqual(pins["schema"], PINS_SCHEMA)
            self.assertEqual(sorted(pins["inputs"]["articles"]), ["101", "102", "103"])
            self.assertEqual(sorted(pins["inputs"]["overlay"]), ["101", "102", "103"])
            self.assertEqual(pins["store"]["universe_sha256"], fixtures.UNIVERSE_SHA)
            self.assertEqual(pins["store"]["record_set_seals"],
                             sorted([fixtures.SEAL_ONE, fixtures.SEAL_TWO]))
            for group, entries in pins["inputs"].items():
                rows = [entries] if group == "current" else list(entries.values())
                for entry in rows:
                    path = os.path.join(tmp, entry["path"])
                    with open(path, "rb") as handle:
                        self.assertEqual(sha256_bytes(handle.read()), entry["sha256"])
            self.assertEqual(pins["inputs"]["overlay"]["103"]["version"], 7)
            self.assertEqual(pins["inputs"]["overlay"]["101"]["version"], 0)
            self.assertEqual(read_json(os.path.join(tmp, "PINS.json"))["schema"], PINS_SCHEMA)

    def test_it_asks_one_request_at_a_time_and_waits_between_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            opener, sleeper = fixtures.FakeOpener(), Sleeper()
            run(tmp, opener, sleep=sleeper)
            # current, one shard, the universe, three articles, three overlays, current.
            self.assertEqual(len(opener.calls), 10)
            self.assertEqual(len(sleeper.waits), 9)  # none before the first request
            self.assertEqual(set(sleeper.waits), {0.25})

    def test_it_backs_off_and_honours_retry_after(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = {"/api/articles/article/101": [
                (429, {"Retry-After": "3"}, b""),
                (503, {}, b""),
            ]}
            opener, sleeper = fixtures.FakeOpener(script=script), Sleeper()
            run(tmp, opener, sleep=sleeper)
            self.assertIn(3.0, sleeper.waits)
            self.assertEqual(opener.calls.count("/api/articles/article/101"), 3)

    def test_a_status_that_will_not_pass_fails_the_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = {"/api/articles/overlay/102": [(500, {}, b"")] * 5}
            with self.assertRaises(FetchError) as caught:
                run(tmp, fixtures.FakeOpener(script=script))
            self.assertIn("overlay/102", str(caught.exception))
            self.assertFalse(os.path.exists(os.path.join(tmp, "PINS.json")))

    def test_it_refuses_to_mix_two_store_builds(self):
        pages = fixtures.site_bodies()
        other = fixtures.articles()[101]
        other["universe"]["sha256"] = fixtures.OTHER_UNIVERSE_SHA
        pages["/api/articles/article/101"] = fixtures.body(other)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FetchError) as caught:
                run(tmp, fixtures.FakeOpener(pages))
            self.assertIn("two store builds must never be mixed", str(caught.exception))
            self.assertFalse(os.path.exists(os.path.join(tmp, "PINS.json")))

    def test_it_refuses_an_article_from_another_record_set(self):
        pages = fixtures.site_bodies()
        other = fixtures.articles()[101]
        other["record_set"] = "9" * 64
        pages["/api/articles/article/101"] = fixtures.body(other)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FetchError) as caught:
                run(tmp, fixtures.FakeOpener(pages))
            self.assertIn("record set", str(caught.exception))

    def test_a_republish_while_fetching_pins_nothing(self):
        moved = fixtures.current()
        moved["built_at"] = "2026-11-02T00:00:00Z"
        script = {"/api/articles/current": [
            (200, {}, fixtures.body(fixtures.current())),  # the first read
            *[(200, {}, fixtures.body(fixtures.current()))] * 0,
        ]}
        pages = fixtures.site_bodies()
        pages["/api/articles/current"] = fixtures.body(moved)  # every later read
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FetchError) as caught:
                run(tmp, fixtures.FakeOpener(pages, script))
            self.assertIn("changed while fetching", str(caught.exception))
            self.assertFalse(os.path.exists(os.path.join(tmp, "PINS.json")))

    def test_pins_replay_the_same_bytes(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as again:
            first = run(tmp)
            second = run(again, pins=os.path.join(tmp, "PINS.json"))
            self.assertEqual(first["inputs"]["articles"]["101"]["sha256"],
                             second["inputs"]["articles"]["101"]["sha256"])

    def test_a_pin_mismatch_refuses_and_leaves_no_pins(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as again:
            run(tmp)
            pins_path = os.path.join(tmp, "PINS.json")
            pins = read_json(pins_path)
            pins["inputs"]["articles"]["103"]["sha256"] = "0" * 64
            with open(pins_path, "w", encoding="utf-8") as handle:
                json.dump(pins, handle)
            with self.assertRaises(FetchError) as caught:
                run(again, pins=pins_path)
            self.assertIn("articles/103.json", str(caught.exception))
            self.assertIn("does not match pinned", str(caught.exception))
            self.assertFalse(os.path.exists(os.path.join(again, "PINS.json")))
            # The mismatched bytes were never written over the pinned input.
            self.assertFalse(os.path.exists(os.path.join(again, "articles", "103.json")))

    def test_an_index_that_disagrees_with_the_build_fails(self):
        pages = fixtures.site_bodies()
        shard = fixtures.index_shard()
        shard["articles"] = shard["articles"][:1]
        pages["/api/articles/index/0"] = fixtures.body(shard)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FetchError) as caught:
                run(tmp, fixtures.FakeOpener(pages))
            self.assertIn("current.json says 3", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
