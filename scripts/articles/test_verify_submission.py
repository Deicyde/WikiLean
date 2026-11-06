#!/usr/bin/env python3
"""Tests for scripts/articles/verify_submission.py, the intake contract (docs/INTAKE.md section 3).

New code. Runs offline on the same hand-built fixture as scripts/articles/test_build_store.py,
converted to cf-locate-recordset/2 by that module's own converter so there is one converter
and not two. Each check of section 3 is exercised twice: the fixture passes it, and one
broken submission is refused with the code, the HTTP status and the retry rule the contract
fixes. The last class is the deliberate non-check — intake judges no location — and the
staging rows, whose payloads must be the posted bytes.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "fixtures"))

import build_store  # noqa: E402
import test_build_store as atlas_tests  # noqa: E402  the fixture copy helpers and the /2 converter
import verify_submission as intake  # noqa: E402

LOCATE = build_store.LOCATE
MACHINE = "contrib-mac-1"
NOW = "2026-11-06T00:00:00Z"
UNIVERSE_SHA = "0e6efcaae05b9aee11c90b919b55052636ee2932901127ea571268dc3a8d6a25"
CAMPAIGN = "fixture-2026-10"


class SubmissionTest(atlas_tests.SetCopyTest):
    """A scratch copy of the fixture record set, given /2's shapes, posted as a submission."""

    def setUp(self):
        super().setUp()
        self.to_v2()

    def fresh(self, convert: bool = True) -> None:
        """A new copy of the fixture; `convert=False` leaves it the committed /1 set."""
        atlas_tests.SetCopyTest.tearDown(self)
        atlas_tests.SetCopyTest.setUp(self)
        if convert:
            self.to_v2()

    def to_v2(self, status_rule: int = 3) -> None:
        # One converter for both test modules: it reads self.set and self.edit_seal, which
        # SetCopyTest provides, so it runs unbound against this class too.
        atlas_tests.RecordSetTwoTests.to_v2(self, status_rule)

    def scope(self, **overrides) -> intake.KeyScope:
        fields = {"machine_id": MACHINE}
        fields.update(overrides)
        return intake.KeyScope(**fields)

    def verify(self, **kwargs):
        kwargs.setdefault("scope", self.scope())
        kwargs.setdefault("received_at", NOW)
        return intake.verify(self.set, **kwargs)

    def accepted(self, **kwargs) -> intake.Accepted:
        outcome = self.verify(**kwargs)
        if isinstance(outcome, intake.Refusal):
            self.fail(outcome.report())
        return outcome

    def refused(self, code: str, status: int, **kwargs) -> intake.Refusal:
        outcome = self.verify(**kwargs)
        if isinstance(outcome, intake.Accepted):
            self.fail(f"accepted, and {code} was expected")
        self.assertEqual((outcome.code, outcome.status), (code, status), outcome.reason)
        return outcome

    def envelope(self, body: bytes = b"a body", **overrides) -> intake.Envelope:
        digest = hashlib.sha256(body).hexdigest()
        _, seal_sha, _ = build_store.read_record_set(self.set)
        fields = {"body_bytes": len(body), "body_sha256": digest, "content_digest": digest,
                  "idempotency_key": seal_sha}
        fields.update(overrides)
        return intake.Envelope(**fields)


class EnvelopeTests(SubmissionTest):
    """Section 2.1: the three things outside the set."""

    def test_a_body_whose_digest_is_not_its_header_is_refused_before_anything_is_unpacked(self):
        refusal = self.refused("body_digest", intake.UNPROCESSABLE,
                               envelope=self.envelope(content_digest="0" * 64))
        self.assertFalse(refusal.retryable)
        self.assertIn(hashlib.sha256(b"a body").hexdigest(), refusal.reason)

    def test_the_idempotency_key_must_be_the_seals_own_sha256(self):
        _, seal_sha, _ = build_store.read_record_set(self.set)
        self.accepted(envelope=self.envelope())
        refusal = self.refused("idempotency_key", intake.UNPROCESSABLE,
                               envelope=self.envelope(idempotency_key="f" * 64))
        self.assertIn(seal_sha, refusal.reason)
        self.assertFalse(refusal.retryable)

    def test_a_body_over_the_endpoints_maximum_is_413_and_names_sharding(self):
        refusal = self.refused("body_too_large", intake.TOO_LARGE,
                               scope=self.scope(max_body_bytes=3), envelope=self.envelope())
        self.assertIn("run:<run>", refusal.reason)
        self.assertFalse(refusal.retryable)

    def test_without_an_envelope_the_two_envelope_checks_are_skipped(self):
        # An operator or a contributor checking a set before it is posted has no headers.
        self.accepted()


class SealTests(SubmissionTest):
    """Checks 2, 3 and 7, and the /1 refusal of section 2."""

    def test_a_v1_set_is_refused_for_carrying_no_passage_key(self):
        self.fresh(convert=False)  # the fixture as committed: cf-locate-recordset/1
        refusal = self.refused("schema_not_accepted", intake.UNPROCESSABLE)
        self.assertIn("cf-locate-recordset/2", refusal.reason)
        self.assertIn("passage_key", refusal.reason)

    def test_both_status_rules_of_a_v2_set_are_taken_and_recorded_per_set(self):
        for rule in (2, 3):
            with self.subTest(rule=rule):
                self.fresh(convert=False)
                self.to_v2(rule)
                self.assertEqual(self.accepted().submission["status_rule"], rule)

    def test_a_file_the_seal_does_not_name_is_a_refusal_not_an_extra(self):
        (self.set / "records" / "extra.txt").write_text("x\n")
        refusal = self.refused("seal", intake.UNPROCESSABLE)
        self.assertIn("present but not listed in SEAL.json", refusal.reason)

    def test_a_file_whose_bytes_are_not_its_sha256_is_refused(self):
        path = self.set / "sources.jsonl"
        path.write_bytes(path.read_bytes().replace(b"Widget lemma", b"Widget lemmb"))
        self.refused("seal", intake.UNPROCESSABLE)

    def test_a_skill_commit_that_is_not_a_commit_is_refused(self):
        self.edit_seal(lambda seal: seal.update(skill_commit="not-a-commit"))
        refusal = self.refused("seal", intake.UNPROCESSABLE)
        self.assertIn("skill_commit must be a 40-hex commit", refusal.reason)

    def test_the_skill_commit_is_recorded_and_not_checked_against_a_release(self):
        # Section 3, check 7: intake records what produced the set; promotion decides.
        self.assertEqual(self.accepted().submission["skill_commit"],
                         "3bceabcee0724ffdf50ce65dde3ab10048c72cba")


class RecordSetTests(SubmissionTest):
    """Checks 4, 5 and 6: the builder's own refusals are intake's, by construction."""

    def test_a_record_the_literature_links_checker_refuses_is_refused(self):
        self.edit_v2(lambda rows: rows[0].update(status={"value": "formalized"}))
        refusal = self.refused("record_set", intake.UNPROCESSABLE)
        self.assertIn("Literature Links $.status", refusal.reason)

    def test_an_identity_pin_that_does_not_resolve_is_refused(self):
        self.edit_seal(lambda seal: seal["universe"].update(universe_sha256="a" * 64))
        refusal = self.refused("record_set", intake.UNPROCESSABLE)
        self.assertIn("universe_sha256", refusal.reason)

    def test_a_quote_over_the_cap_is_refused(self):
        def overlong(rows):
            quote = rows[0]["subject"][0]["location"]["quote"]
            quote["exact"] = " ".join(f"word{n}" for n in range(build_store.QUOTE_MAX_WORDS + 4))
        self.edit_v2(overlong)
        self.refused("record_set", intake.UNPROCESSABLE)

    def test_the_refusal_carries_every_problem_the_builder_named(self):
        self.edit_v2(lambda rows: [rows[0].update(kind="nonsense"), rows[1].update(kind="nonsense")])
        refusal = self.refused("record_set", intake.UNPROCESSABLE)
        self.assertEqual(refusal.reason.count("kind must be one of"), 2, refusal.reason)

    def edit_v2(self, edit) -> None:
        """Edit the first article's records in the converted copy, then reseal it as /2."""
        path = self.set / "records/enwiki-1001-2002.jsonl"
        rows = atlas_tests.read_jsonl(path)
        edit(rows)
        atlas_tests.write_jsonl(path, rows)
        atlas_tests.reseal(self.set)
        self.edit_seal(lambda seal: seal.update(schema="cf-locate-recordset/2", status_rule=3))


class ScopeTests(SubmissionTest):
    """Check 8: one key per machine, and what it may post."""

    def test_a_key_narrowed_to_another_campaign_may_not_post_this_set(self):
        refusal = self.refused("scope_campaign", intake.FORBIDDEN,
                               scope=self.scope(campaigns=("run:loc-7",)))
        self.assertIn(CAMPAIGN, refusal.reason)
        self.assertFalse(refusal.retryable)

    def test_a_key_narrowed_to_another_universe_may_not_post_this_set(self):
        refusal = self.refused("scope_universe", intake.FORBIDDEN, scope=self.scope(universes=("b" * 64,)))
        self.assertIn(UNIVERSE_SHA, refusal.reason)

    def test_a_key_narrowed_to_this_campaign_and_universe_posts_it(self):
        self.accepted(scope=self.scope(campaigns=(CAMPAIGN, "run:loc-7"), universes=(UNIVERSE_SHA,)))

    def test_an_unnarrowed_key_posts_any_campaign_and_any_universe(self):
        self.assertEqual(self.accepted().submission["campaign"], CAMPAIGN)

    def test_the_submissions_quota_is_429_and_names_itself(self):
        self.accepted(scope=self.scope(max_submissions_per_day=4), usage=intake.Usage(submissions=3))
        refusal = self.refused("quota_submissions", intake.TOO_MANY,
                               scope=self.scope(max_submissions_per_day=4),
                               usage=intake.Usage(submissions=4))
        self.assertTrue(refusal.retryable)
        self.assertIn("4 a day", refusal.reason)

    def test_the_byte_quota_counts_this_submission_and_is_429(self):
        refusal = self.refused("quota_bytes", intake.TOO_MANY, scope=self.scope(max_bytes_per_day=1000),
                               usage=intake.Usage(bytes=999))
        self.assertTrue(refusal.retryable)
        self.assertIn("999 bytes today", refusal.reason)

    def test_a_zero_quota_is_no_quota(self):
        self.accepted(usage=intake.Usage(submissions=10_000, bytes=10 ** 12))


class StagingTests(SubmissionTest):
    """Section 4: what an accepted submission becomes, and what it does not do."""

    def test_the_submission_row_carries_the_set_its_rule_its_machine_and_the_checker(self):
        row = self.accepted().submission
        _, seal_sha, _ = build_store.read_record_set(self.set)
        provenance = json.loads((build_store.VENDOR / "provenance.json").read_text(encoding="utf-8"))
        self.assertEqual(row["seal_sha256"], seal_sha)
        self.assertEqual(row["set_id"], "fixture-set")
        self.assertEqual(row["sourceset"], "ss-fixture")
        self.assertEqual(row["universe_sha256"], UNIVERSE_SHA)
        self.assertEqual(row["machine_id"], MACHINE)
        self.assertEqual(row["received_at"], NOW)
        self.assertEqual(row["state"], "accepted")
        self.assertEqual(row["checker_version"], provenance["version"])

    def test_every_record_is_one_row_whose_payload_is_the_posted_line(self):
        accepted = self.accepted()
        posted = {path: (self.set / path).read_text(encoding="utf-8").splitlines()
                  for path in ("records/enwiki-1001-2002.jsonl", "records/enwiki-1003-2004.jsonl")}
        self.assertEqual(len(accepted.records), sum(len(lines) for lines in posted.values()))
        for row in accepted.records:
            self.assertIn(row["payload"], posted[f"records/{row['source_id']}.jsonl"])
            self.assertEqual(json.loads(row["payload"])["extensions"][LOCATE]["passage_key"],
                             row["passage_key"])

    def test_a_location_row_carries_its_pair_key_and_an_absence_row_carries_none(self):
        rows = self.accepted().records
        located = [r for r in rows if r["relation"] not in (None, "noMatch")]
        absent = [r for r in rows if r["relation"] == "noMatch"]
        self.assertTrue(located and absent)
        self.assertTrue(all(r["pair_key"] for r in located))
        self.assertTrue(all(r["pair_key"] is None for r in absent))

    def test_every_file_of_the_set_is_one_row_with_the_seals_sha256(self):
        accepted = self.accepted()
        seal, _, _ = build_store.read_record_set(self.set)
        self.assertEqual({row["path"] for row in accepted.files}, set(seal["files"]))
        for row in accepted.files:
            self.assertEqual(row["sha256"], seal["files"][row["path"]])
            self.assertEqual(row["bytes"], (self.set / row["path"]).stat().st_size)

    def test_intake_judges_no_location(self):
        # A set whose location is wrong is still a well-formed set: the challenge judges it,
        # and an intake that re-judged records would be a second judgment nobody reviewed.
        accepted = self.accepted()
        self.assertNotIn("verdict", accepted.submission)
        for row in accepted.records:
            self.assertEqual(set(row), {"source_id", "passage_key", "pair_key", "relation",
                                        "status", "payload"})

    def test_the_rows_are_a_pure_function_of_the_set_and_the_clock(self):
        self.assertEqual(self.accepted().as_dict(), self.accepted().as_dict())

    def test_a_received_at_that_is_not_an_utc_time_is_the_callers_bug(self):
        with self.assertRaises(ValueError):
            self.verify(received_at="yesterday")


class CommandTests(SubmissionTest):
    """The CLI an operator runs by hand until the endpoint exists."""

    def run_main(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = intake.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_an_accepted_set_exits_0_and_writes_its_staging_rows(self):
        target = self.tmp / "rows.json"
        code, _, err = self.run_main("--record-set", str(self.set), "--machine-id", MACHINE,
                                     "--received-at", NOW, "--out", str(target))
        self.assertEqual(code, 0, err)
        self.assertIn("staged, not live", err)
        self.assertEqual(json.loads(target.read_text()), self.accepted().as_dict())

    def test_a_refused_set_exits_2_and_names_its_code_and_status(self):
        self.fresh(convert=False)  # /1
        code, out, err = self.run_main("--record-set", str(self.set), "--machine-id", MACHINE)
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("schema_not_accepted, HTTP 422, must not be retried", err)

    def test_the_three_envelope_options_go_together(self):
        with self.assertRaises(SystemExit) as caught:
            self.run_main("--record-set", str(self.set), "--machine-id", MACHINE,
                          "--content-digest", "0" * 64)
        self.assertIn("go together", str(caught.exception))

    def test_the_envelope_options_check_the_posted_body(self):
        body = self.tmp / "body.bin"
        body.write_bytes(b"the posted bytes")
        _, seal_sha, _ = build_store.read_record_set(self.set)
        argv = ["--record-set", str(self.set), "--machine-id", MACHINE, "--received-at", NOW,
                "--body", str(body), "--idempotency-key", seal_sha, "--content-digest"]
        code, _, err = self.run_main(*argv, hashlib.sha256(b"the posted bytes").hexdigest())
        self.assertEqual(code, 0, err)
        code, _, err = self.run_main(*argv, "0" * 64)
        self.assertEqual(code, 2)
        self.assertIn("body_digest", err)

    def test_an_interpreter_the_builder_refuses_checks_nothing(self):
        original = build_store.interpreter_problem
        build_store.interpreter_problem = lambda: original((3, 9, 6), "cpython")
        try:
            code, out, err = self.run_main("--record-set", str(self.set), "--machine-id", MACHINE)
        finally:
            build_store.interpreter_problem = original
        self.assertEqual(code, 3)
        self.assertEqual(out, "")
        self.assertIn("nothing was checked", err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
