#!/usr/bin/env python3
"""Per-tag rule in settle.classify(): the PR author's latest reject/revise/flag
stands unless a LISTED maintainer (settle.MAINTAINERS) wrote a verdict on that
tag after it, and every listed maintainer counts as one whatever GitHub's
author_association says. The gate itself is unchanged."""
from __future__ import annotations

import contextlib
import io
import unittest
from pathlib import Path

import poll
import settle
from test_poll import CONFLICT_SHA, PR, REPO, FakeGh, PollFixture

AUTHOR = "Deicyde"
EARLY = "2026-08-10T12:00:00Z"      # a reviewer's reaction, before the author's verdicts
APPROVE_AT = "2026-08-12T00:00:00Z"  # a maintainer's written approve, before the author's reject
REJECT_AT = "2026-08-24T00:00:00Z"   # the author's written reject
AFTER = "2026-08-25T00:00:00Z"
LATER = "2026-08-30T00:00:00Z"


def review(login, state="COMMENTED", assoc="CONTRIBUTOR", at="2026-08-11T00:00:00Z"):
    return {"user": {"login": login}, "author_association": assoc, "state": state, "submitted_at": at}


def crossref_comment(cid, qid, plus=0, minus=0):
    """The bot's per-tag inline comment that reviewers react to."""
    return {"id": cid, "user": {"login": AUTHOR}, "author_association": "CONTRIBUTOR",
            "body": f"Cross-reference for {qid} <!-- crossref-bot:{qid} -->",
            "created_at": "2026-08-10T11:20:00Z", "reactions": {"+1": plus, "-1": minus}}


def reaction(login, content="+1", at=EARLY):
    return {"content": content, "user": {"login": login}, "created_at": at}


def written_verdict(login, qid, status, at=AFTER, assoc="CONTRIBUTOR"):
    """An inline WikiLean review comment carrying an explicit verdict."""
    return {"id": 900, "user": {"login": login}, "author_association": assoc,
            "body": f"<!-- wikilean-review:{qid} --> ({status})\n> {status} this tag", "created_at": at}


def pasted_review(login, verdicts, at=REJECT_AT):
    """The '## WikiLean review' issue comment the operator pastes from the site."""
    lines = ["## WikiLean review", ""]
    for qid, status in verdicts.items():
        lines += [f"- **[{qid}]** tag", f"  - status: **{status}**", f"  - {status} by the operator"]
    return {"user": {"login": login}, "author_association": "CONTRIBUTOR",
            "body": "\n".join(lines), "created_at": at}


AUTHOR_REJECTS_SET = pasted_review(AUTHOR, {"Q36161": "reject"})


class PerTagRuleTest(PollFixture):
    def classify(self, **kwargs):
        return self.run_with(FakeGh(**kwargs), settle.classify, PR, REPO)

    def verdict(self, r, qid="Q36161"):
        tag = next(t for t in r["green"] + r["recycle"] if t["qid"] == qid)
        return ("green" if tag in r["green"] else "recycle"), tag["reason"]

    def test_a_maintainer_s_thumbs_down_after_the_author_s_reject_is_not_green(self):
        # Written approve, then the author's written reject, then the maintainer's 👎:
        # both now reject, so the tag recycles, with or without a blanket approval.
        for blanket in ([], [review("SnirBroshi", "APPROVED")]):
            with self.subTest(blanket=bool(blanket)):
                r = self.classify(
                    reviews=blanket,
                    comments=[written_verdict("jcommelin", "Q36161", "approve", at=APPROVE_AT),
                              crossref_comment(101, "Q36161", minus=1)],
                    reactions={101: [reaction("jcommelin", "-1", LATER)]},
                    issue_comments=[AUTHOR_REJECTS_SET])
                self.assertEqual(self.verdict(r)[0], "recycle")

    def test_a_collaborator_s_thumbs_up_before_the_author_s_reject_recycles(self):
        r = self.classify(reviews=[review("SnirBroshi", assoc="COLLABORATOR")],
                          comments=[crossref_comment(101, "Q36161", plus=1)],
                          reactions={101: [reaction("SnirBroshi")]},
                          issue_comments=[AUTHOR_REJECTS_SET])
        self.assertEqual(self.verdict(r), ("recycle", "objection (reject/revise)"))

    def test_a_listed_maintainer_s_thumbs_up_before_the_author_s_reject_recycles(self):
        r = self.classify(comments=[crossref_comment(101, "Q36161", plus=1)],
                          reactions={101: [reaction("jcommelin")]},
                          issue_comments=[AUTHOR_REJECTS_SET])
        self.assertEqual(self.verdict(r), ("recycle", "objection (reject/revise)"))

    def test_a_listed_maintainer_s_reaction_alone_never_overrides_the_author(self):
        fake = FakeGh(comments=[crossref_comment(101, "Q36161", plus=1)],
                      reactions={101: [reaction("jcommelin", "+1", LATER)]},
                      issue_comments=[AUTHOR_REJECTS_SET])
        r = self.run_with(fake, settle.classify, PR, REPO)
        self.assertEqual(self.verdict(r), ("recycle", "objection (reject/revise)"))
        self.assertFalse([c for c in fake.calls if c[1] == "api" and c[2].startswith("orgs/")])

    def test_a_maintainer_s_earlier_written_approve_loses_to_the_author_s_later_reject(self):
        r = self.classify(comments=[written_verdict("jcommelin", "Q36161", "approve", at=APPROVE_AT)],
                          issue_comments=[AUTHOR_REJECTS_SET])
        self.assertEqual(self.verdict(r), ("recycle", "objection (reject/revise)"))

    def test_a_listed_maintainer_s_written_approve_after_the_author_s_reject_is_green(self):
        for login in ("jcommelin", "JCommelin", "grunweg"):  # grunweg's membership is private
            with self.subTest(login=login):
                r = self.classify(comments=[written_verdict(login, "Q36161", "approve")],
                                  issue_comments=[AUTHOR_REJECTS_SET])
                self.assertEqual(self.verdict(r), ("green", "maintainer approved"))

    def test_a_same_meaning_reaction_after_that_written_approve_keeps_it_green(self):
        r = self.classify(comments=[written_verdict("jcommelin", "Q36161", "approve"),
                                    crossref_comment(101, "Q36161", plus=1)],
                          reactions={101: [reaction("jcommelin", "+1", LATER)]},
                          issue_comments=[AUTHOR_REJECTS_SET])
        self.assertEqual(self.verdict(r), ("green", "maintainer approved"))

    def test_a_thumbs_down_after_that_written_approve_recycles(self):
        r = self.classify(comments=[written_verdict("jcommelin", "Q36161", "approve"),
                                    crossref_comment(101, "Q36161", minus=1)],
                          reactions={101: [reaction("jcommelin", "-1", LATER)]},
                          issue_comments=[AUTHOR_REJECTS_SET])
        self.assertEqual(self.verdict(r)[0], "recycle")

    def test_an_association_only_maintainer_does_not_override_the_author(self):
        r = self.classify(reviews=[review("SnirBroshi", assoc="COLLABORATOR")],
                          comments=[written_verdict("SnirBroshi", "Q36161", "approve", at=LATER,
                                                    assoc="COLLABORATOR")],
                          issue_comments=[AUTHOR_REJECTS_SET])
        self.assertEqual(self.verdict(r), ("recycle", "objection (reject/revise)"))

    def test_the_author_retracting_lifts_the_objection(self):
        r = self.classify(comments=[crossref_comment(101, "Q36161", plus=1)],
                          reactions={101: [reaction(AUTHOR, "+1", LATER)]},
                          issue_comments=[AUTHOR_REJECTS_SET])
        self.assertEqual(self.verdict(r), ("green", "approved (>=1 review, no objection)"))

    def test_a_private_listed_maintainer_s_reject_beats_a_collaborator_s_thumbs_up(self):
        # grunweg's org membership is private, so GitHub shows CONTRIBUTOR and the
        # members probe 404s; only the list makes the reject a maintainer's.
        r = self.classify(reviews=[review("SnirBroshi", assoc="COLLABORATOR")],
                          comments=[crossref_comment(101, "Q36161", plus=1),
                                    written_verdict("grunweg", "Q36161", "reject")],
                          reactions={101: [reaction("SnirBroshi")]})
        self.assertEqual(self.verdict(r), ("recycle", "maintainer rejected/revised"))
        self.assertEqual(r["maintainer_reviewers"], ["SnirBroshi", "grunweg"])

    def test_a_collaborator_s_thumbs_up_still_overrides_a_non_author_s_reject(self):
        # Unchanged: an association-only maintainer trumps everyone but the PR author,
        # and the gate still opens on the author plus that collaborator.
        r = self.classify(reviews=[review("SnirBroshi", assoc="COLLABORATOR")],
                          comments=[crossref_comment(101, "Q36161", plus=1),
                                    written_verdict("someone", "Q36161", "reject")],
                          reactions={101: [reaction("SnirBroshi")]},
                          issue_comments=[pasted_review(AUTHOR, {"Q474715": "approve"})])
        self.assertEqual(self.verdict(r), ("green", "maintainer approved"))
        r = self.classify(reviews=[review("SnirBroshi", assoc="COLLABORATOR")],
                          comments=[crossref_comment(101, "Q36161", plus=1)],
                          reactions={101: [reaction("SnirBroshi")]},
                          issue_comments=[pasted_review(AUTHOR, {"Q474715": "approve"})])
        self.assertEqual(r["reviewers"], [AUTHOR, "SnirBroshi"])
        self.assertTrue(r["gate"])

    def test_listed_maintainers_count_whatever_the_association(self):
        r = self.classify(reviews=[review("GrunWeg", "APPROVED")],
                          issue_comments=[pasted_review(AUTHOR, {"Q36161": "approve"})])
        self.assertEqual(r["maintainer_reviewers"], ["GrunWeg"])
        self.assertTrue(r["gate"])
        self.assertEqual(len(settle.MAINTAINERS), 28)
        self.assertTrue(settle.is_maintainer("remydegenne"))
        self.assertFalse(settle.is_maintainer("SnirBroshi"))
        self.assertFalse(settle.is_maintainer(""))


DIFF_42608 = "".join(f"+++ b/Mathlib/F{i}.lean\n+@[wikidata {q}]\n" for i, q in enumerate(
    ["Q166154", "Q994399", "Q124247109", "Q24743362", "Q332791", "Q36161", "Q474715",
     "Q1474074", "Q1070472"]))


class Pr42608ReplayTest(PollFixture):
    def test_set_and_partial_order_recycle_and_the_rest_stay_green(self):
        # mathlib4#42608's verdict shape: a Mathlib reviewer (association COLLABORATOR)
        # approved three tags (as 👍 here) before the author's pasted WikiLean review,
        # which approved seven and rejected Set and PartialOrder.
        approved = ["Q166154", "Q994399", "Q124247109", "Q24743362", "Q332791", "Q1474074", "Q1070472"]
        thumbs = {"Q24743362": 101, "Q36161": 102, "Q474715": 103}
        r = self.run_with(FakeGh(
            diff=DIFF_42608,
            reviews=[review("SnirBroshi", assoc="COLLABORATOR")],
            comments=[crossref_comment(cid, q, plus=1) for q, cid in thumbs.items()],
            reactions={cid: [reaction("SnirBroshi")] for cid in thumbs.values()},
            issue_comments=[pasted_review(AUTHOR, {**{q: "approve" for q in approved},
                                                   "Q36161": "reject", "Q474715": "reject"})]),
            settle.classify, PR, REPO)
        self.assertTrue(r["gate"])  # the gate is unchanged: author + a collaborator
        self.assertEqual([g["qid"] for g in r["green"]], approved)
        self.assertEqual([(t["qid"], t["reason"]) for t in r["recycle"]],
                         [("Q36161", "objection (reject/revise)"), ("Q474715", "objection (reject/revise)")])
        h = settle.ready_header(r)
        self.assertIn("**7** `@[wikidata]` tags", h)
        self.assertIn("2 recycled to the next batch.", h)


class ConflictRebuildTest(PollFixture):
    def test_a_rebuild_keeps_every_tag_on_the_pr(self):
        # With the gate closed nothing trims or triages a recycled tag, so the conflict
        # rebuild must re-apply it rather than drop it.
        self.write_state(settled_pr=PR, conflict_sha=CONFLICT_SHA)
        fake = FakeGh(mergeable=("CONFLICTING",), issue_comments=[AUTHOR_REJECTS_SET])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.run_with(fake, poll.tick, Path("/unused"), True)
        self.assertIn("rebuild off fresh master with the 2 tags it carries", out.getvalue())


if __name__ == "__main__":
    unittest.main()
