#!/usr/bin/env python3
"""Deterministic reviewer table for a PR's @[wikidata] tags.

Consumes the review tool's /api/review payload — which already derives each
tag's namespace-qualified declaration (extractDeclName) and Wikidata label
deterministically from the PR — and renders a Markdown table. No hand-curation,
no judgement: same PR in → same table out.

  pr_table.py <pr> [--repo owner/name] [--header "…"]
"""
import argparse, json, subprocess, sys
import settle

WIKI = "https://wikilean.jackmccarthy.org"
# what reviewers put -> dot. A bare note (no verdict) shows as a comment marker.
EMO = {"approve": "🟢", "revise": "🟡", "reject": "🔴", "flag": "⚠️", "(note)": "💬"}


def reviews_cell(verdicts):
    """e.g. '🟡 @Deicyde · 🟢 @jcommelin' from {login: status}."""
    if not verdicts:
        return "—"
    parts = []
    for login, status in verdicts.items():
        mark = "\\*" if login in settle.MAINTAINERS else ""  # maintainer marked with *
        parts.append(f"{EMO.get(status, '·')} @{login}{mark}")
    return " · ".join(parts)


def table(pr, repo="leanprover-community/mathlib4", header=None):
    owner, name = repo.split("/")
    url = f"{WIKI}/api/review/{owner}/{name}/{pr}"
    out = subprocess.run(["curl", "-s", "-H", "User-Agent: WikiLean-bot/1.0", url],
                         capture_output=True, text=True).stdout
    data = json.loads(out)
    if not data.get("ok", True) and "decls" not in data:
        raise SystemExit(f"/api/review error: {data.get('error')}")
    decls = data.get("decls", [])
    # per-tag reviewer verdicts (deterministic — same logic as the settler)
    cls = settle.classify(pr, repo)
    verdicts = {e["qid"]: e["verdicts"] for e in cls["green"] + cls["recycle"]}
    rows = []
    for i, d in enumerate(decls, 1):
        qid = d["qid"]
        decl = d.get("decl") or "?"
        wd = d.get("wd") or {}
        concept = wd.get("enwikiTitle") or wd.get("label") or ""
        wp = wd.get("enwikiUrl")
        c = f"[{concept}]({wp})" if (concept and wp) else (concept or "—")
        rows.append(f"| {i} | {c} | [{qid}](https://www.wikidata.org/wiki/{qid}) | `{decl}` | {reviews_cell(verdicts.get(qid, {}))} |")
    head = header or f"The **{len(decls)}** `@[wikidata]` tags in this PR:"
    return (head + "\n\n| # | Concept | Wikidata | Mathlib declaration | Reviews |\n"
            "|--:|:--|:--|:--|:--|\n" + "\n".join(rows) +
            "\n\n<sub>Reviews: 🟢 approve · 🟡 revise · 🔴 reject · ⚠️ deletion-candidate · 💬 comment. \\* = maintainer.</sub>")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("pr", type=int)
    ap.add_argument("--repo", default="leanprover-community/mathlib4")
    ap.add_argument("--header", default=None)
    args = ap.parse_args()
    print(table(args.pr, args.repo, args.header))
