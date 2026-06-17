#!/usr/bin/env python3
"""Deterministic reviewer table for a PR's @[wikidata] tags.

Consumes the review tool's /api/review payload — which already derives each
tag's namespace-qualified declaration (extractDeclName) and Wikidata label
deterministically from the PR — and renders a Markdown table. No hand-curation,
no judgement: same PR in → same table out.

  pr_table.py <pr> [--repo owner/name] [--header "…"]
"""
import argparse, json, subprocess, sys

WIKI = "https://wikilean.jackmccarthy.org"


def table(pr, repo="leanprover-community/mathlib4", header=None):
    owner, name = repo.split("/")
    url = f"{WIKI}/api/review/{owner}/{name}/{pr}"
    out = subprocess.run(["curl", "-s", "-H", "User-Agent: WikiLean-bot/1.0", url],
                         capture_output=True, text=True).stdout
    data = json.loads(out)
    if not data.get("ok", True) and "decls" not in data:
        raise SystemExit(f"/api/review error: {data.get('error')}")
    decls = data.get("decls", [])
    rows = []
    for i, d in enumerate(decls, 1):
        qid = d["qid"]
        decl = d.get("decl") or "?"
        wd = d.get("wd") or {}
        concept = wd.get("enwikiTitle") or wd.get("label") or ""
        wp = wd.get("enwikiUrl")
        c = f"[{concept}]({wp})" if (concept and wp) else (concept or "—")
        rows.append(f"| {i} | {c} | [{qid}](https://www.wikidata.org/wiki/{qid}) | `{decl}` | `{d.get('file','')}:{d.get('line','')}` |")
    head = header or f"The **{len(decls)}** `@[wikidata]` tags in this PR:"
    return (head + "\n\n| # | Concept | Wikidata | Mathlib declaration | Source |\n"
            "|--:|:--|:--|:--|:--|\n" + "\n".join(rows))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("pr", type=int)
    ap.add_argument("--repo", default="leanprover-community/mathlib4")
    ap.add_argument("--header", default=None)
    args = ap.parse_args()
    print(table(args.pr, args.repo, args.header))
