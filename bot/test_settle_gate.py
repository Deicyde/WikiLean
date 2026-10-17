#!/usr/bin/env python3
"""Per-tag rule in settle.classify(): every listed maintainer (settle.MAINTAINERS)
counts as one whatever GitHub's author_association says, and the PR author gets no
more say than any other reviewer: a maintainer's verdict beats theirs whenever and
however it was given. The gate itself is unchanged."""
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

    def test_a_maintainer_s_approve_beats_the_author_s_reject(self):
        # The author is reviewed like anyone else: a maintainer's approve wins whether
        # it came before or after the author's reject, as a 👍 or written, and whether
        # the maintainer is listed or recognised by association.
        collaborator = [review("SnirBroshi", assoc="COLLABORATOR")]
        shapes = {
            "collaborator 👍 earlier": dict(
                reviews=collaborator, comments=[crossref_comment(101, "Q36161", plus=1)],
                reactions={101: [reaction("SnirBroshi")]}),
            "listed 👍 later": dict(
                comments=[crossref_comment(101, "Q36161", plus=1)],
                reactions={101: [reaction("jcommelin", "+1", LATER)]}),
            "listed written approve earlier": dict(
                comments=[written_verdict("jcommelin", "Q36161", "approve", at=APPROVE_AT)]),
            "collaborator written approve later": dict(
                reviews=collaborator,
                comments=[written_verdict("SnirBroshi", "Q36161", "approve", at=LATER,
                                          assoc="COLLABORATOR")]),
        }
        for name, shape in shapes.items():
            with self.subTest(shape=name):
                r = self.classify(**shape, issue_comments=[AUTHOR_REJECTS_SET])
                self.assertEqual(self.verdict(r), ("green", "maintainer approved"))

    def test_a_listed_maintainer_s_reaction_needs_no_org_probe(self):
        fake = FakeGh(comments=[crossref_comment(101, "Q36161", plus=1)],
                      reactions={101: [reaction("jcommelin", "+1", LATER)]},
                      issue_comments=[AUTHOR_REJECTS_SET])
        r = self.run_with(fake, settle.classify, PR, REPO)
        self.assertEqual(self.verdict(r), ("green", "maintainer approved"))
        self.assertFalse([c for c in fake.calls if c[1] == "api" and c[2].startswith("orgs/")])

    def test_a_maintainer_s_later_thumbs_down_replaces_their_written_approve(self):
        # Written approve, then the author's written reject, then the maintainer's 👎:
        # the maintainer now rejects, so the tag recycles, with or without a blanket approval.
        for blanket in ([], [review("SnirBroshi", "APPROVED")]):
            with self.subTest(blanket=bool(blanket)):
                r = self.classify(
                    reviews=blanket,
                    comments=[written_verdict("jcommelin", "Q36161", "approve", at=APPROVE_AT),
                              crossref_comment(101, "Q36161", minus=1)],
                    reactions={101: [reaction("jcommelin", "-1", LATER)]},
                    issue_comments=[AUTHOR_REJECTS_SET])
                self.assertEqual(self.verdict(r), ("recycle", "maintainer rejected/revised"))

    def test_listed_maintainers_match_whatever_the_login_case_or_membership(self):
        for login in ("jcommelin", "JCommelin", "grunweg"):  # grunweg's membership is private
            with self.subTest(login=login):
                r = self.classify(comments=[written_verdict(login, "Q36161", "approve")],
                                  issue_comments=[AUTHOR_REJECTS_SET])
                self.assertEqual(self.verdict(r), ("green", "maintainer approved"))

    def test_any_maintainer_s_objection_recycles_whatever_the_author_says(self):
        # A listed maintainer's written approve, but another maintainer objects: a listed
        # one by 👎, a listed MEMBER in writing earlier, or a collaborator by 👎.
        approve_after = written_verdict("jcommelin", "Q36161", "approve")
        shapes = {
            "listed 👎 later": dict(
                comments=[approve_after, crossref_comment(101, "Q36161", minus=1)],
                reactions={101: [reaction("kbuzzard", "-1", LATER)]}),
            "listed written reject earlier": dict(
                comments=[written_verdict("sgouezel", "Q36161", "reject", at=APPROVE_AT,
                                          assoc="MEMBER"), approve_after]),
            "collaborator 👎 later": dict(
                reviews=[review("SnirBroshi", assoc="COLLABORATOR")],
                comments=[approve_after, crossref_comment(101, "Q36161", minus=1)],
                reactions={101: [reaction("SnirBroshi", "-1", LATER)]}),
        }
        for name, shape in shapes.items():
            for author_rejects in (False, True):
                with self.subTest(shape=name, author_rejects=author_rejects):
                    r = self.classify(**shape,
                                      issue_comments=[AUTHOR_REJECTS_SET] if author_rejects else [])
                    self.assertEqual(self.verdict(r), ("recycle", "maintainer rejected/revised"))

    def test_an_author_with_an_org_role_is_a_maintainer_like_anyone_else(self):
        # If GitHub shows the author as a collaborator, their reject is a maintainer's,
        # and one maintainer's objection recycles the tag whoever approves it.
        r = self.classify(reviews=[review(AUTHOR, assoc="COLLABORATOR")],
                          comments=[written_verdict("jcommelin", "Q36161", "approve")],
                          issue_comments=[AUTHOR_REJECTS_SET])
        self.assertEqual(self.verdict(r), ("recycle", "maintainer rejected/revised"))

    def test_a_private_listed_maintainer_s_reject_beats_a_collaborator_s_thumbs_up(self):
        # grunweg's org membership is private, so GitHub shows CONTRIBUTOR and the
        # members probe 404s; only the list makes the reject a maintainer's.
        r = self.classify(reviews=[review("SnirBroshi", assoc="COLLABORATOR")],
                          comments=[crossref_comment(101, "Q36161", plus=1),
                                    written_verdict("grunweg", "Q36161", "reject")],
                          reactions={101: [reaction("SnirBroshi")]})
        self.assertEqual(self.verdict(r), ("recycle", "maintainer rejected/revised"))
        self.assertEqual(r["maintainer_reviewers"], ["SnirBroshi", "grunweg"])

    def test_a_collaborator_s_thumbs_up_overrides_a_reject_and_opens_the_gate_with_the_author(self):
        # Unchanged: an association-only maintainer trumps every non-maintainer, and the
        # gate still opens on the author plus that collaborator.
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
    def test_every_tag_stays_green(self):
        # mathlib4#42608's verdict shape: a Mathlib reviewer (association COLLABORATOR)
        # approved three tags (as 👍 here) before the author's pasted WikiLean review,
        # which approved seven and rejected Set and PartialOrder. The reviewer's 👍 is a
        # maintainer's verdict, so it beats the author's rejects.
        tags = ["Q166154", "Q994399", "Q124247109", "Q24743362", "Q332791", "Q36161", "Q474715",
                "Q1474074", "Q1070472"]
        approved = [q for q in tags if q not in ("Q36161", "Q474715")]
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
        self.assertEqual([g["qid"] for g in r["green"]], tags)
        self.assertEqual(r["recycle"], [])
        h = settle.ready_header(r)
        self.assertIn("**9** `@[wikidata]` tags", h)
        self.assertNotIn("recycled", h)


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
