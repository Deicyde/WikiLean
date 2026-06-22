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


def table(pr, repo="leanprover-community/mathlib4", header=None, fresh=False):
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
    head = header or (
        f"This PR adds **{len(decls)}** `@[wikidata]` cross-reference tags." if fresh
        else f"This PR was trimmed to the **{len(decls)}** `@[wikidata]` tags that were approved 🟢 in review.")
    return (head + "\n\n| # | Concept | Wikidata | Mathlib declaration | Reviews |\n"
            "|--:|:--|:--|:--|:--|\n" + "\n".join(rows) +
            "\n\n<sub>Reviews: 🟢 approve · 🟡 revise · 🔴 reject · ⚠️ deletion-candidate · 💬 comment. \\* = maintainer. "
            "Recycled tags: https://wikilean.jackmccarthy.org/queue</sub>"
            "\n<!-- wikilean-tag-table -->")


MARKER = "<!-- wikilean-tag-table -->"


def post(pr, repo, body):
    """Idempotent: update the existing wikilean-tag-table comment, else create it.
    Skips the PATCH when the rendered body is unchanged, so a periodic refresh that
    finds no new reviews doesn't churn the comment's 'edited' timestamp."""
    existing = subprocess.run(
        ["gh", "api", f"repos/{repo}/issues/{pr}/comments", "--paginate",
         "--jq", f'.[] | select(.body | contains("{MARKER}")) | {{id, body}}'],
        capture_output=True, text=True).stdout.strip()
    cid, cur = None, ""
    if existing:
        o = json.loads(existing.splitlines()[0]); cid, cur = o.get("id"), o.get("body") or ""
    norm = lambda s: (s or "").replace("\r\n", "\n").strip()
    if cid is not None:
        if norm(cur) == norm(body):
            return "unchanged", 0, ""
        cmd = ["gh", "api", "--method", "PATCH", f"repos/{repo}/issues/comments/{cid}"]
        action = "updated"
    else:
        cmd = ["gh", "api", "--method", "POST", f"repos/{repo}/issues/{pr}/comments"]
        action = "created"
    p = subprocess.run(cmd + ["-F", "body=@-"], input=body, capture_output=True, text=True)
    return action, p.returncode, (p.stderr or p.stdout)[:200]


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("pr", type=int)
    ap.add_argument("--repo", default="leanprover-community/mathlib4")
    ap.add_argument("--header", default=None)
    ap.add_argument("--fresh", action="store_true", help="freshly-opened batch header (adds N tags)")
    ap.add_argument("--post", action="store_true", help="post/update the comment on the PR (idempotent)")
    args = ap.parse_args()
    md = table(args.pr, args.repo, args.header, args.fresh)
    if args.post:
        action, rc, msg = post(args.pr, args.repo, md)
        print(f"{action} tag-table comment on #{args.pr}" + (f" (error: {msg})" if rc else ""))
    else:
        print(md)
