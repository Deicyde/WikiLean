#!/usr/bin/env python3
"""A whole round against a loopback WikiLean: what it picks, and what it sends.

These run the real HTTP client against the real routes' shapes, with the real
model boundary pointed at a fake `claude`. Nothing here reaches the network
beyond 127.0.0.1, and nothing calls a model.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fakes
from wikilean import journal, model, state
from wikilean.config import Config
from wikilean.site import Limited, Site, retry_after_seconds
from wikilean.store import (Overlay, article_order, open_work,
                            review_candidates, tag_candidates)
from wikilean.work import (EX_FAILED, EX_NOTHING, EX_OK, EX_WAIT, Options,
                           run_loop, run_round)

TOKEN = "wla_" + "D" * 43


class CandidateOrderTest(unittest.TestCase):
    """Which passage a round offers, decided on the fixture alone."""

    def setUp(self):
        self.article = fakes.article()

    def test_review_offers_the_downgraded_link_before_the_checked_absence(self):
        found = review_candidates(self.article, Overlay.of([]))
        self.assertEqual([(c.reason, c.passage["item"]) for c in found],
                         [("downgraded", "downgraded"), ("absence", "absence")])

    def test_tag_offers_only_a_passage_with_no_record_and_no_judged_absence(self):
        found = tag_candidates(self.article, Overlay.of([]))
        self.assertEqual([c.passage["item"] for c in found], ["untagged"])

    def test_the_two_units_never_offer_the_same_passage(self):
        review = {c.passage["item"] for c in review_candidates(self.article, Overlay.of([]))}
        tag = {c.passage["item"] for c in tag_candidates(self.article, Overlay.of([]))}
        self.assertEqual(review & tag, set())

    def test_a_row_a_person_wrote_is_not_offered(self):
        overlay = Overlay.of([{"pair_key": fakes.DOWNGRADED_PAIR, "actor_type": "human",
                               "state": "live", "version": 2, "passage_key": fakes.PASSAGE_DOWNGRADED}])
        self.assertTrue(overlay.written_by_a_person(fakes.DOWNGRADED_PAIR))
        found = review_candidates(self.article, overlay)
        self.assertEqual([c.reason for c in found], ["absence"])

    def test_a_row_we_already_wrote_is_not_offered_again(self):
        overlay = Overlay.of([{"pair_key": fakes.DOWNGRADED_PAIR, "actor_type": "agent",
                               "state": "live", "version": 1, "passage_key": fakes.PASSAGE_DOWNGRADED}])
        self.assertEqual([c.reason for c in review_candidates(self.article, overlay)], ["absence"])

    def test_an_unresolved_passage_is_never_offered(self):
        article = fakes.article()
        article["passages"][2]["outcome"] = "unresolved"
        self.assertEqual([c.reason for c in review_candidates(article, Overlay.of([]))], ["downgraded"])

    def test_open_work_counts_what_the_index_says(self):
        rows = [dict(zip(fakes.index_shard()["fields"], row)) for row in fakes.index_shard()["articles"]]
        self.assertEqual(open_work(rows)["not_formalized"], 2)
        self.assertEqual([e["pageid"] for e in article_order(rows)], [fakes.PAGEID])
        self.assertEqual(article_order(rows, skip=[fakes.PAGEID]), [])


class RoundTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.site_server = fakes.FakeWikiLean()
        self.addCleanup(self.site_server.close)
        overrides = fakes.private_home(self.directory)
        overrides[model.CLAUDE_CMD_ENV] = fakes.write_fake_claude(self.directory)
        self.env = mock.patch.dict(os.environ, overrides, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.config = Config(base_url=self.site_server.base_url, interval=0.0)
        self.site = Site(self.site_server.base_url, TOKEN)

    def reply(self, value: dict) -> None:
        fakes.set_control(self.directory, reply=json.dumps(value))

    def run_one(self, **kwargs):
        return run_round(self.site, self.config, Options(verbose=False, **kwargs), log=lambda *_: None)

    # ---- the three outcomes of a review --------------------------------------

    def test_agreeing_with_a_downgraded_link_writes_a_confirming_row(self):
        self.reply({"verdict": "agree"})
        outcome = self.run_one()
        self.assertEqual(outcome.code, EX_OK, outcome.summary)
        self.assertTrue(outcome.wrote)
        writes = self.site_server.writes
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0]["path"], "/api/articles/tag")
        body = writes[0]["body"]
        # The article's own campaign, not current.json's: the route answers
        # 409 on a mismatch and the two differ in production.
        self.assertEqual(body["campaign"], fakes.CAMPAIGN)
        self.assertNotEqual(body["campaign"], fakes.current()["campaign"])
        self.assertEqual(body["passage_key"], fakes.PASSAGE_DOWNGRADED)
        self.assertEqual(body["declaration"], fakes.DOWNGRADED_DECL)
        self.assertEqual(body["relation"], "broadMatch")
        self.assertEqual(body["library"], "mathlib")
        self.assertEqual(body["base_version"], 0)
        self.assertIn("run_id", body)
        self.assertNotIn("repository", body)   # the server derives it
        self.assertNotIn("revision", body)

    def test_a_wrong_relation_writes_the_correction_with_the_reason(self):
        self.reply({"verdict": "wrong_relation", "relation": "closeMatch",
                    "reason": "the two differ only by the usual convention"})
        self.assertTrue(self.run_one().wrote)
        body = self.site_server.writes[0]["body"]
        self.assertEqual(body["relation"], "closeMatch")
        self.assertEqual(body["note"], "the two differ only by the usual convention")

    def test_a_wrong_link_is_a_soft_delete_at_the_pair_key_with_a_reason(self):
        self.reply({"verdict": "wrong_link", "reason": "this declaration is about something else"})
        self.assertTrue(self.run_one().wrote)
        write = self.site_server.writes[0]
        self.assertEqual(write["path"], f"/api/articles/tag/{fakes.DOWNGRADED_PAIR}/state")
        self.assertEqual(write["body"]["state"], "removed")
        self.assertEqual(write["body"]["reason"], "this declaration is about something else")
        self.assertEqual(write["body"]["base_version"], 0)

    def test_a_confirmed_absence_sends_nothing_because_there_is_no_row_for_it(self):
        # The downgraded link is dealt with first, so mark it seen and the
        # absence becomes the round's candidate.
        self.reply({"verdict": "agree"})
        self.run_one()
        self.site_server.calls.clear()
        outcome = self.run_one()
        self.assertEqual(outcome.code, EX_OK)
        self.assertFalse(outcome.wrote)
        self.assertEqual(self.site_server.writes, [])
        self.assertIn("absence confirmed", outcome.summary)

    # ---- validation ----------------------------------------------------------

    def test_an_out_of_vocabulary_answer_is_skipped_and_logged_never_sent(self):
        self.reply({"verdict": "looks right to me"})
        outcome = self.run_one()
        self.assertEqual(outcome.code, EX_OK)
        self.assertFalse(outcome.wrote)
        self.assertEqual(self.site_server.writes, [])
        entry = journal.read(1, event="round")[0]
        self.assertFalse(entry["sent"])
        self.assertIn("verdict must be one of", entry["rejected"])

    def test_a_declaration_the_index_does_not_know_is_never_written(self):
        self.reply({"verdict": "agree"})
        self.run_one()                       # settle the downgraded link
        self.site_server.calls.clear()
        self.reply({"verdict": "missed", "declaration": "Fixture.hallucinated",
                    "library": "mathlib", "relation": "exactMatch", "reason": "surely this exists"})
        outcome = self.run_one()
        self.assertEqual(self.site_server.writes, [])
        self.assertIn("published index", outcome.summary)
        # It did ask the oracle, rather than guessing.
        self.assertIn("/decl/Fixture.hallucinated", [call["path"] for call in self.site_server.calls])

    def test_a_declaration_the_index_knows_is_written(self):
        self.reply({"verdict": "agree"})
        self.run_one()
        self.site_server.calls.clear()
        self.reply({"verdict": "missed", "declaration": "Fixture.known", "library": "mathlib",
                    "relation": "exactMatch", "reason": "this states exactly the passage"})
        outcome = self.run_one()
        self.assertTrue(outcome.wrote, outcome.summary)
        body = self.site_server.writes[0]["body"]
        self.assertEqual(body["declaration"], "Fixture.known")
        self.assertEqual(body["passage_key"], fakes.PASSAGE_ABSENCE)

    def test_a_model_failure_ends_the_round_with_no_write(self):
        fakes.set_control(self.directory, fail=True)
        outcome = self.run_one()
        self.assertEqual(outcome.code, EX_FAILED)
        self.assertEqual(self.site_server.writes, [])

    # ---- the dry run ----------------------------------------------------------

    def test_dry_run_prints_the_request_and_sends_nothing(self):
        self.reply({"verdict": "agree"})
        printed: list[str] = []
        outcome = run_round(self.site, self.config, Options(dry_run=True, verbose=False),
                            log=printed.append)
        self.assertEqual(outcome.code, EX_OK)
        self.assertFalse(outcome.wrote)
        self.assertEqual(self.site_server.writes, [])
        text = "\n".join(printed)
        self.assertIn("POST " + self.site_server.base_url + "/api/articles/tag", text)
        self.assertIn(fakes.DOWNGRADED_DECL, text)
        self.assertIn(fakes.CAMPAIGN, text)
        # The header is named, the value never shown.
        self.assertIn("Authorization: Bearer <your agent token>", text)
        self.assertNotIn(TOKEN, text)

    def test_dry_run_does_not_mark_the_candidate_as_done(self):
        self.reply({"verdict": "agree"})
        run_round(self.site, self.config, Options(dry_run=True, verbose=False), log=lambda *_: None)
        outcome = self.run_one()
        self.assertTrue(outcome.wrote)

    # ---- unit selection --------------------------------------------------------

    def test_review_comes_before_tag(self):
        self.reply({"verdict": "agree"})
        self.assertEqual(self.run_one().unit, "review")

    def test_unit_tag_goes_straight_to_the_untagged_passage(self):
        self.reply({"declaration": "Fixture.known", "library": "mathlib",
                    "relation": "exactMatch", "note": "it states this"})
        outcome = self.run_one(unit="tag")
        self.assertEqual(outcome.unit, "tag")
        self.assertTrue(outcome.wrote, outcome.summary)
        self.assertEqual(self.site_server.writes[0]["body"]["passage_key"], fakes.PASSAGE_UNTAGGED)

    def test_a_declining_tag_answer_sends_nothing(self):
        self.reply({"skip": True, "reason": "no declaration in these libraries states it"})
        outcome = self.run_one(unit="tag")
        self.assertFalse(outcome.wrote)
        self.assertEqual(self.site_server.writes, [])

    def test_locate_is_off_without_both_opt_ins(self):
        outcome = self.run_one(unit="locate")
        self.assertEqual(outcome.code, EX_NOTHING)
        self.assertIn("--enable-locate", outcome.summary)
        self.assertEqual(self.site_server.writes, [])

    def test_when_everything_is_judged_the_round_reports_nothing_to_do(self):
        from wikilean.work import _seen_key

        article, overlay = fakes.article(), Overlay.of([])
        for candidate in review_candidates(article, overlay) + tag_candidates(article, overlay):
            state.mark_seen(_seen_key(candidate))
        outcome = self.run_one()
        self.assertEqual(outcome.code, EX_NOTHING)
        self.assertEqual(self.site_server.writes, [])

    # ---- agreeing must not quietly rewrite the pipeline's own text ---------------

    def test_a_confirmation_sends_the_pipeline_s_note_back_unchanged(self):
        """articles.ts reads `confirms` from the relation AND the note, so a
        confirmation that dropped the note would be a silent edit."""
        site_with_note = fakes.FakeWikiLean()
        self.addCleanup(site_with_note.close)
        original = fakes.article
        noted = fakes.article()
        noted["passages"][1]["locations"][0]["note"] = "the pipeline said this"
        with mock.patch.object(fakes, "article", lambda: noted):
            self.reply({"verdict": "agree"})
            site = Site(site_with_note.base_url, TOKEN)
            run_round(site, Config(base_url=site_with_note.base_url), Options(verbose=False),
                      log=lambda *_: None)
        self.assertEqual(fakes.article, original)
        self.assertEqual(site_with_note.writes[0]["body"]["note"], "the pipeline said this")

    # ---- a pair the model names may already be somebody's row --------------------

    def _only_the_absence(self):
        """Settle the downgraded link locally, so the absence is next up."""
        from wikilean.work import _seen_key

        for candidate in review_candidates(fakes.article(), Overlay.of([])):
            if candidate.reason == "downgraded":
                state.mark_seen(_seen_key(candidate))

    def _overlay_row(self, declaration: str, **fields) -> dict:
        row = {"pair_key": fakes.pair_key(fakes.PASSAGE_ABSENCE, fakes.REPO, declaration),
               "passage_key": fakes.PASSAGE_ABSENCE, "declaration": declaration,
               "actor_type": "agent", "state": "live", "version": 1}
        row.update(fields)
        return row

    def test_base_version_for_a_new_pair_comes_from_the_overlay(self):
        self._only_the_absence()
        self.site_server.overlay_rows = [self._overlay_row("Fixture.known", state="removed", version=3)]
        self.reply({"verdict": "missed", "declaration": "Fixture.known", "library": "mathlib",
                    "relation": "exactMatch", "reason": "this states exactly the passage"})
        outcome = self.run_one(unit="review")
        self.assertTrue(outcome.wrote, outcome.summary)
        body = self.site_server.writes[-1]["body"]
        self.assertEqual(body["declaration"], "Fixture.known")
        # Not 0: the row is already at 3, and a 0 here would be a certain 409.
        self.assertEqual(body["base_version"], 3)

    def test_a_pair_a_person_wrote_is_refused_before_the_request(self):
        self._only_the_absence()
        self.site_server.overlay_rows = [self._overlay_row("Fixture.known", actor_type="human")]
        self.reply({"verdict": "missed", "declaration": "Fixture.known", "library": "mathlib",
                    "relation": "exactMatch", "reason": "this states exactly the passage"})
        outcome = self.run_one(unit="review")
        self.assertEqual(self.site_server.writes, [])
        self.assertIn("a person already tagged", outcome.summary)

    def test_a_passage_a_person_has_tagged_is_not_offered_to_the_tag_unit(self):
        overlay = Overlay.of([{"pair_key": "f" * 64, "passage_key": fakes.PASSAGE_UNTAGGED,
                               "actor_type": "human", "state": "live", "version": 1}])
        self.assertEqual(tag_candidates(fakes.article(), overlay), [])

    # ---- the unit order is global, not per article --------------------------------

    def test_review_work_beats_tag_work_in_an_earlier_article(self):
        both = fakes.FakeWikiLean(second=True)
        self.addCleanup(both.close)
        self.reply({"verdict": "agree"})
        site = Site(both.base_url, TOKEN)
        outcome = run_round(site, Config(base_url=both.base_url), Options(verbose=False),
                            log=lambda *_: None)
        self.assertEqual(outcome.unit, "review")
        self.assertEqual(outcome.candidate.pageid, fakes.PAGEID)
        # It did look at the tag-only article first, and passed it over.
        looked = [call["path"] for call in both.calls]
        self.assertLess(looked.index(f"/api/articles/article/{fakes.SECOND_PAGEID}"),
                        looked.index(f"/api/articles/article/{fakes.PAGEID}"))
        self.assertEqual(both.writes[0]["body"]["pageid"], fakes.PAGEID)

    # ---- one write a round, and backing off -------------------------------------

    def test_exactly_one_write_leaves_the_round(self):
        self.reply({"verdict": "agree"})
        self.run_one()
        self.assertEqual(len(self.site_server.writes), 1)

    def test_a_429_ends_the_round_and_sets_the_pause_from_retry_after(self):
        self.reply({"verdict": "agree"})
        self.site_server.next_write = (429, {"ok": False, "error": "rate limited — slow down"},
                                       {"Retry-After": "42"})
        outcome = self.run_one()
        self.assertEqual(outcome.code, EX_WAIT)
        self.assertAlmostEqual(outcome.wait, 42, delta=1)
        self.assertGreater(state.pause_until(), time.time() + 40)
        self.assertEqual(len(self.site_server.writes), 1)   # not retried

    def test_a_503_write_brake_pauses_for_much_longer(self):
        self.reply({"verdict": "agree"})
        self.site_server.next_write = (503, {"ok": False, "error": "editing is paused site-wide"}, {})
        outcome = self.run_one()
        self.assertEqual(outcome.code, EX_WAIT)
        self.assertGreaterEqual(outcome.wait, 600)
        self.assertIn("paused", outcome.summary)

    def test_a_round_started_inside_a_pause_sends_nothing(self):
        state.pause_until(time.time() + 300)
        self.reply({"verdict": "agree"})
        outcome = self.run_one()
        self.assertEqual(outcome.code, EX_WAIT)
        self.assertEqual(self.site_server.writes, [])

    def test_the_loop_waits_the_retry_after_it_was_given(self):
        self.reply({"verdict": "agree"})
        self.site_server.next_write = (429, {"ok": False, "error": "slow down"}, {"Retry-After": "90"})
        slept: list[float] = []
        run_loop(self.site, self.config, Options(verbose=False), log=lambda *_: None,
                 sleep=slept.append, rounds=2)
        self.assertTrue(slept and 80 <= slept[0] <= 100, slept)

    def test_retry_after_parses_seconds_and_dates_and_falls_back(self):
        self.assertEqual(retry_after_seconds("30", fallback=60), 30)
        self.assertEqual(retry_after_seconds(None, fallback=60), 60)
        self.assertEqual(retry_after_seconds("nonsense", fallback=60), 60)
        self.assertEqual(retry_after_seconds("-5", fallback=60), 60)
        self.assertEqual(retry_after_seconds("99999", fallback=60), 3600)
        future = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(time.time() + 120))
        self.assertTrue(100 <= retry_after_seconds(future, fallback=60) <= 125)

    def test_a_rate_limited_read_is_retried_after_the_delay(self):
        slept: list[float] = []
        site = Site(self.site_server.base_url, TOKEN, sleep=slept.append)
        calls = {"n": 0}
        real = site._send

        def flaky(method, path, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise Limited("slow down", status=429, retry_after=7)
            return real(method, path, **kwargs)

        with mock.patch.object(site, "_send", flaky):
            self.assertEqual(site.current()["campaign"], "fixture-b1")
        self.assertEqual(slept, [7])

    # ---- the journal ------------------------------------------------------------

    def test_every_round_and_request_is_journalled_without_the_token(self):
        self.reply({"verdict": "agree"})
        self.run_one()
        text = journal.journal_path().read_text(encoding="utf-8")
        self.assertNotIn(TOKEN, text)
        events = [json.loads(line)["event"] for line in text.strip().splitlines()]
        self.assertIn("round-start", events)
        self.assertIn("request", events)
        self.assertIn("round", events)
        request = journal.read(1, event="request")[0]
        self.assertEqual(request["method"], "POST")
        self.assertEqual(request["path"], "/api/articles/tag")
        self.assertEqual(request["body"]["declaration"], fakes.DOWNGRADED_DECL)


class TokenOnTheWireTest(unittest.TestCase):
    """Who is given the token, and who is not."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.site_server = fakes.FakeWikiLean()
        self.addCleanup(self.site_server.close)
        overrides = fakes.private_home(self.directory)
        overrides[model.CLAUDE_CMD_ENV] = fakes.write_fake_claude(
            self.directory, reply='{"verdict": "agree"}')
        self.env = mock.patch.dict(os.environ, overrides, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_public_reads_are_anonymous_and_only_the_write_is_authenticated(self):
        site = Site(self.site_server.base_url, TOKEN)
        run_round(site, Config(base_url=self.site_server.base_url), Options(verbose=False),
                  log=lambda *_: None)
        authenticated = {call["path"] for call in self.site_server.calls
                         if "authorization" in call["headers"]}
        self.assertEqual(authenticated, {"/api/articles/tag"})
        for call in self.site_server.calls:
            self.assertNotIn(TOKEN, call["path"])
        header = next(call["headers"]["authorization"] for call in self.site_server.writes)
        self.assertEqual(header, "Bearer " + TOKEN)

    def test_a_site_with_no_token_cannot_write(self):
        site = Site(self.site_server.base_url, None)
        outcome = run_round(site, Config(base_url=self.site_server.base_url), Options(verbose=False),
                            log=lambda *_: None)
        self.assertEqual(outcome.code, EX_FAILED)
        self.assertEqual(self.site_server.writes, [])

    def test_a_redirect_is_never_followed_with_the_token(self):
        site = Site(self.site_server.base_url, TOKEN)
        self.site_server.next_write = (302, {"ok": False},
                                       {"Location": "https://example.invalid/collect"})
        with self.assertRaises(Exception):
            site.write(site.plan("POST", "/api/articles/tag", {"pageid": 1}))


if __name__ == "__main__":
    unittest.main()
