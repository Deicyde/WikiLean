#!/usr/bin/env python3
"""Deterministic reviewer table for a PR's @[wikidata] tags.

Consumes the review tool's /api/review payload — which already derives each
tag's namespace-qualified declaration (extractDeclName) and Wikidata label
deterministically from the PR — and renders a Markdown table. No hand-curation,
no judgement: same PR in → same table out.

  pr_table.py <pr> [--repo owner/name] [--header "…"]
"""
import argparse, json, re, subprocess, sys
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


def sync_body_count(pr, repo, n=None):
    """Keep the PR body's 'adds a batch of N `@[wikidata]` attributes' line in sync with
    the tags ACTUALLY in the PR's current diff. That line is written once at open; the
    settle-trim (split.py) and the conflict-rebuild (poll.resolve_conflicts) both shrink
    the diff but never touch the body, so without this it overstates after a trim.

    n defaults to a fresh count of @[wikidata] attribute occurrences in `gh pr diff`:
    anchored to `@[` so a QID mentioned in added prose isn't counted, and via findall so a
    co-located `@[wikidata Q1, wikidata Q2]` counts as 2 — matching the ENTRY counts the
    explicit-n callers pass. Callers that already know the exact post-edit count (settle,
    conflict-resolver) pass it to skip the fetch AND any GitHub diff-recompute lag right
    after a force-push. Idempotent; best-effort (never raises), but a real `gh pr edit`
    failure is surfaced loudly — it would otherwise leave a settled PR's body wrong with no
    later re-sync (refresh_table skips settled PRs)."""
    try:
        if n is None:
            diff = subprocess.run(["gh", "pr", "diff", str(pr), "--repo", repo],
                                  capture_output=True, text=True).stdout
            n = sum(len(re.findall(r"wikidata\s+Q\d+", l)) for l in diff.splitlines()
                    if l.startswith("+") and not l.startswith("+++") and "@[" in l)
        if not n:
            return  # empty / failed diff — don't clobber the body with "batch of 0"
        body = subprocess.run(["gh", "pr", "view", str(pr), "--repo", repo, "--json", "body", "--jq", ".body"],
                              capture_output=True, text=True).stdout
        new = re.sub(r"adds a batch of \d+", f"adds a batch of {n}", body, count=1)
        if new != body and "adds a batch of" in body:
            r = subprocess.run(["gh", "pr", "edit", str(pr), "--repo", repo, "--body", new],
                               capture_output=True, text=True)
            if r.returncode == 0:
                print(f"  synced PR body tag count -> {n}")
            else:
                print(f"  WARNING: PR body count sync to {n} failed: {(r.stderr or r.stdout).strip()[:150]}")
    except Exception as e:
        print(f"  (body-count sync skipped: {e})")


def sync_review_link(pr, repo):
    """Self-heal a blank reviewer-UI `?pr=` in the PR body: the open-time fill
    (open_batch.fill_review_link) can lose the fresh cross-fork editability race, so run
    this on every table post — a later tick fills any link the open missed. Idempotent;
    best-effort (never raises)."""
    try:
        body = subprocess.run(["gh", "pr", "view", str(pr), "--repo", repo, "--json", "body", "--jq", ".body"],
                              capture_output=True, text=True).stdout
        if "/review?pr=" in body and f"/review?pr={pr}" not in body:
            new = body.replace("/review?pr=", f"/review?pr={pr}")
            r = subprocess.run(["gh", "pr", "edit", str(pr), "--repo", repo, "--body", new],
                               capture_output=True, text=True)
            if r.returncode == 0:
                print(f"  filled reviewer-UI link (?pr={pr})")
    except Exception as e:
        print(f"  (review-link sync skipped: {e})")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("pr", type=int)
    ap.add_argument("--repo", default="leanprover-community/mathlib4")
    ap.add_argument("--header", default=None)
    ap.add_argument("--fresh", action="store_true", help="freshly-opened batch header (adds N tags)")
    ap.add_argument("--post", action="store_true", help="post/update the comment on the PR (idempotent)")
    ap.add_argument("--no-body-sync", action="store_true",
                    help="skip the PR-body 'batch of N' count sync (caller syncs it with an exact count)")
    args = ap.parse_args()
    md = table(args.pr, args.repo, args.header, args.fresh)
    if args.post:
        action, rc, msg = post(args.pr, args.repo, md)
        print(f"{action} tag-table comment on #{args.pr}" + (f" (error: {msg})" if rc else ""))
        # Re-sync the PR body's tag count on every table post (open / every tick), so a trim
        # that shrinks the diff can't leave the body overstating. Callers that JUST
        # force-pushed (settle, conflict-resolve) pass --no-body-sync and sync the exact
        # count themselves, to avoid recounting a possibly-lagged post-push `gh pr diff`.
        if not args.no_body_sync:
            sync_body_count(args.pr, args.repo)
        sync_review_link(args.pr, args.repo)   # self-heal a blank ?pr= the open-time fill missed
    else:
        print(md)
