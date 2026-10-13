#!/usr/bin/env python3
"""Who counts in the settle gate: the PR author never does, only the explicit
maintainer list is 'maintainer', a reaction never overrides the author's written
objection, and the public header states the rule that was applied."""
from __future__ import annotations

import contextlib
import io
import json
import unittest
from pathlib import Path
from unittest import mock

import poll
import settle
from test_poll import CONFLICT_SHA, PR, REPO, FakeGh, PollFixture, pull

AUTHOR = "Deicyde"


def review(login, state="APPROVED", assoc="CONTRIBUTOR", at="2026-08-11T00:00:00Z"):
    return {"user": {"login": login}, "author_association": assoc, "state": state, "submitted_at": at}


def crossref_comment(cid, qid, plus=0, minus=0):
    """The bot's per-tag inline comment that reviewers react to."""
    return {"id": cid, "user": {"login": AUTHOR}, "author_association": "CONTRIBUTOR",
            "body": f"Cross-reference for {qid} <!-- crossref-bot:{qid} -->",
            "created_at": "2026-08-10T11:20:00Z", "reactions": {"+1": plus, "-1": minus}}


def reaction(login, content="+1", at="2026-08-10T12:00:00Z"):
    return {"content": content, "user": {"login": login}, "created_at": at}


def written_verdict(login, qid, status, at="2026-08-12T00:00:00Z", assoc="CONTRIBUTOR"):
    """An inline WikiLean review comment carrying an explicit verdict."""
    return {"id": 900, "user": {"login": login}, "author_association": assoc,
            "body": f"<!-- wikilean-review:{qid} --> ({status})\n> {status} this tag", "created_at": at}


def pasted_review(login, verdicts, at="2026-08-24T00:00:00Z"):
    """The '## WikiLean review' issue comment the operator pastes from the site."""
    lines = ["## WikiLean review", ""]
    for qid, status in verdicts.items():
        lines += [f"- **[{qid}]** tag", f"  - status: **{status}**", f"  - {status} by the operator"]
    return {"user": {"login": login}, "author_association": "CONTRIBUTOR",
            "body": "\n".join(lines), "created_at": at}


class GateTest(PollFixture):
    def classify(self, **kwargs):
        return self.run_with(FakeGh(**kwargs), settle.classify, PR, REPO)

    def test_the_author_is_never_a_gate_reviewer(self):
        r = self.classify(reviews=[review(AUTHOR, "COMMENTED"), review("jcommelin")],
                          issue_comments=[pasted_review(AUTHOR, {"Q36161": "approve"})])
        self.assertEqual(r["author"], AUTHOR)
        self.assertEqual(r["reviewers"], ["jcommelin"])
        self.assertFalse(r["gate"])
        self.assertFalse(r["gate_reasons"]["two_reviewers"])

        r = self.classify(reviews=[review(AUTHOR, "COMMENTED"), review("jcommelin"), review("SnirBroshi")])
        self.assertTrue(r["gate"])
        self.assertEqual(r["reviewers"], ["SnirBroshi", "jcommelin"])

    def test_the_author_s_objection_still_recycles_a_tag(self):
        r = self.classify(reviews=[review("jcommelin"), review("SnirBroshi")],
                          issue_comments=[pasted_review(AUTHOR, {"Q36161": "reject"})])
        self.assertEqual([g["qid"] for g in r["recycle"]], ["Q36161"])

    def test_github_association_does_not_make_a_maintainer(self):
        r = self.classify(reviews=[review("SnirBroshi", assoc="COLLABORATOR"),
                                   review("someone", assoc="MEMBER")])
        self.assertEqual(r["maintainer_reviewers"], [])
        self.assertFalse(r["gate"])

    def test_a_listed_maintainer_counts_whatever_the_association(self):
        r = self.classify(reviews=[review("faenuccio", assoc="CONTRIBUTOR"), review("SnirBroshi")])
        self.assertEqual(r["maintainer_reviewers"], ["faenuccio"])
        self.assertTrue(r["gate"])

    def test_a_maintainer_reaction_does_not_override_the_author_s_written_reject(self):
        r = self.classify(
            reviews=[review("SnirBroshi", "COMMENTED")],
            comments=[crossref_comment(101, "Q36161", plus=1)],
            reactions={101: [reaction("jcommelin")]},
            issue_comments=[pasted_review(AUTHOR, {"Q36161": "reject"})])
        q = next(t for t in r["green"] + r["recycle"] if t["qid"] == "Q36161")
        self.assertIn(q, r["recycle"])
        self.assertEqual(q["reason"], "objection (reject/revise)")

    def test_a_maintainer_reaction_still_approves_when_the_author_did_not_object(self):
        r = self.classify(comments=[crossref_comment(101, "Q36161", plus=1)],
                          reactions={101: [reaction("jcommelin")]})
        q = next(t for t in r["green"] if t["qid"] == "Q36161")
        self.assertEqual(q["reason"], "maintainer approved")
        self.assertTrue(q["approved_by_reviewer"])

    def test_a_maintainer_s_written_approve_still_trumps_the_author(self):
        r = self.classify(comments=[written_verdict("jcommelin", "Q36161", "approve")],
                          issue_comments=[pasted_review(AUTHOR, {"Q36161": "reject"})])
        self.assertIn("Q36161", [g["qid"] for g in r["green"]])

    def test_the_author_s_reaction_after_a_written_reject_keeps_the_objection(self):
        r = self.classify(
            comments=[crossref_comment(101, "Q36161", plus=1, minus=1)],
            reactions={101: [reaction("jcommelin", "+1", "2026-08-25T00:00:00Z"),
                             reaction(AUTHOR, "-1", "2026-08-26T00:00:00Z")]},
            issue_comments=[pasted_review(AUTHOR, {"Q36161": "reject"}, at="2026-08-24T00:00:00Z")])
        self.assertIn("Q36161", [t["qid"] for t in r["recycle"]])

    def test_a_maintainer_s_reaction_after_their_written_approve_keeps_the_trump(self):
        r = self.classify(
            comments=[written_verdict("jcommelin", "Q36161", "approve", at="2026-08-12T00:00:00Z"),
                      crossref_comment(101, "Q36161", plus=1)],
            reactions={101: [reaction("jcommelin", "+1", "2026-08-30T00:00:00Z")]},
            issue_comments=[pasted_review(AUTHOR, {"Q36161": "reject"})])
        q = next(t for t in r["green"] if t["qid"] == "Q36161")
        self.assertEqual(q["reason"], "maintainer approved")

    def test_the_author_retracting_by_reaction_lifts_the_objection(self):
        r = self.classify(
            comments=[crossref_comment(101, "Q36161", plus=2)],
            reactions={101: [reaction("jcommelin", "+1", "2026-08-25T00:00:00Z"),
                             reaction(AUTHOR, "+1", "2026-08-26T00:00:00Z")]},
            issue_comments=[pasted_review(AUTHOR, {"Q36161": "reject"}, at="2026-08-24T00:00:00Z")])
        self.assertIn("Q36161", [t["qid"] for t in r["green"]])

    def test_maintainer_logins_match_case_insensitively(self):
        r = self.classify(reviews=[review("JCommelin"), review("SnirBroshi")])
        self.assertEqual(r["maintainer_reviewers"], ["JCommelin"])
        self.assertTrue(settle.is_maintainer("remydegenne"))
        self.assertFalse(settle.is_maintainer(""))

    def test_a_pr_without_an_author_fails_closed(self):
        pr_json = pull()
        del pr_json["user"]
        with self.assertRaisesRegex(settle.GhError, "no author login"):
            self.classify(pr_json=pr_json, reviews=[review("jcommelin"), review("SnirBroshi")])

    def test_pr_42608_shape(self):
        # The author rejected Set and PartialOrder in writing; a reviewer (not a
        # maintainer) thumbed both up. The old gate opened on author + reviewer and
        # kept both tags green.
        r = self.classify(
            reviews=[review(AUTHOR, "COMMENTED", assoc="CONTRIBUTOR"),
                     review("SnirBroshi", "COMMENTED", assoc="COLLABORATOR")],
            comments=[crossref_comment(101, "Q36161", plus=1), crossref_comment(102, "Q474715", plus=1)],
            reactions={101: [reaction("SnirBroshi")], 102: [reaction("SnirBroshi")]},
            issue_comments=[pasted_review(AUTHOR, {"Q36161": "reject", "Q474715": "reject"})])
        self.assertEqual(r["reviewers"], ["SnirBroshi"])
        self.assertEqual(r["maintainer_reviewers"], [])
        self.assertFalse(r["gate"])
        self.assertEqual(sorted(t["qid"] for t in r["recycle"]), ["Q36161", "Q474715"])

    def test_approved_by_reviewer_ignores_the_author_s_own_approval(self):
        r = self.classify(reviews=[review("SnirBroshi", "COMMENTED"), review("jcommelin", "COMMENTED")],
                          issue_comments=[pasted_review(AUTHOR, {"Q36161": "approve", "Q474715": "approve"})],
                          comments=[written_verdict("SnirBroshi", "Q474715", "approve")])
        by_qid = {g["qid"]: g for g in r["green"]}
        self.assertFalse(by_qid["Q36161"]["approved_by_reviewer"])
        self.assertTrue(by_qid["Q474715"]["approved_by_reviewer"])


class ConflictRebuildTest(PollFixture):
    def test_a_rebuild_keeps_every_tag_on_the_pr(self):
        # Under the new rule the author's written reject recycles Q36161, but the gate is
        # closed, so no settle trims it; the conflict rebuild must not drop it either.
        self.write_state(settled_pr=PR, conflict_sha=CONFLICT_SHA)
        fake = FakeGh(mergeable=("CONFLICTING",), reviews=[review("SnirBroshi", "COMMENTED")],
                      issue_comments=[pasted_review(AUTHOR, {"Q36161": "reject"})])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.run_with(fake, poll.tick, Path("/unused"), True)
        self.assertIn("rebuild off fresh master with the 2 tags it carries", out.getvalue())


class HeaderTest(PollFixture):
    CLS = {"pr": PR, "author": AUTHOR, "reviewers": ["SnirBroshi", "jcommelin"],
           "maintainer_reviewers": ["jcommelin"], "gate": True, "tags": ["Q1", "Q2", "Q3"],
           "green": [{"qid": "Q1", "approved_by_reviewer": True},
                     {"qid": "Q2", "approved_by_reviewer": False}],
           "recycle": [{"qid": "Q3"}]}

    def test_header_states_the_rule_that_was_applied(self):
        h = settle.ready_header(self.CLS)
        self.assertIn("**2** `@[wikidata]` tags", h)
        self.assertIn("1 approved by a reviewer other than the PR author", h)
        self.assertIn("1 approved by the author with no objection from reviewers", h)
        self.assertIn("Reviewed by 2 people besides the author (maintainers: `jcommelin`)", h)
        self.assertIn("1 recycled to the next batch.", h)
        self.assertNotIn("@jcommelin", h)  # names, not pings
        self.assertNotIn("≥2 reviewers", h)
        self.assertTrue(h.endswith("<!-- wikilean-bot-ready -->"))

    def test_settle_posts_that_header(self):
        self.write_state(settled_pr=None)
        commands = []
        with mock.patch.object(poll, "sh", side_effect=lambda cmd, **kw: commands.append(cmd)), \
             mock.patch.object(poll, "CANDS", self.here / "state" / "pool_candidates.json"), \
             mock.patch.object(poll.pool, "candidates", return_value=[]), \
             mock.patch("pr_table.sync_body_count"):
            poll.do_settle(PR, "wikilean/wikidata-batch-7", Path("/unused"), self.CLS, dry=False)
        table = next(c for c in commands if str(c[1]).endswith("pr_table.py"))
        self.assertEqual(table[table.index("--header") + 1], settle.ready_header(self.CLS))


if __name__ == "__main__":
    unittest.main()
