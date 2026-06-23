#!/usr/bin/env python3
"""Settle the brain of the daily @[wikidata] batch bot — DETERMINISTIC.

Reads an upstream PR's reviews and computes the gate + per-tag green/recycle
split. No mutations. Importable (`classify(pr)`) and runnable (prints a report).

Rule (see bot/README.md):
  - Gate: >=2 distinct human reviewers on the PR AND >=24h since it opened.
  - A bare note/comment = "defer to the other reviewer" -> ignored.
  - A maintainer's explicit approve/reject/revise/flag TRUMPS.
  - Else: any reject/revise/flag -> recycle; else >=1 approve -> green; else recycle.
"""
import json, subprocess, re, sys, datetime as dt

MAINTAINERS = {"jcommelin"}          # allowlist seed (always treated as maintainer)
# GitHub author_association values that mark a Mathlib maintainer/reviewer.
ORG_ROLES = {"OWNER", "MEMBER", "COLLABORATOR"}
OBJECT = ("reject", "revise", "flag")  # hard objections (a note is NOT one)
BOTS = {"github-actions[bot]"}
REACTION_VERDICT = {"+1": "approve", "-1": "reject"}  # 👍 = approve, 👎 = reject (REST content names)


def gh_list(repo, path):
    out = subprocess.run(["gh", "api", f"repos/{repo}/{path}", "--paginate", "-q", ".[]"],
                         capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"gh api {path}: {out.stderr[:200]}")
    items, dec, s, i, n = [], json.JSONDecoder(), out.stdout, 0, len(out.stdout)
    while i < n:
        while i < n and s[i] in " \t\r\n":
            i += 1
        if i >= n:
            break
        obj, i = dec.raw_decode(s, i)
        items.append(obj)
    return items


def gh_obj(repo, path):
    return json.loads(subprocess.run(["gh", "api", f"repos/{repo}/{path}"],
                                     capture_output=True, text=True).stdout)


def gh_reactions(repo, comment_id):
    """Reactions on an inline PR review comment. [] on error — reactions are an
    additive verdict source, so a fetch hiccup must never fail the settle."""
    try:
        return gh_list(repo, f"pulls/comments/{comment_id}/reactions")
    except RuntimeError:
        return []


_ORG_MEMBER = {}
def is_org_member(repo, login):
    """Is `login` a PUBLIC member of the repo's org? Reaction objects carry no
    author_association, so this is how a react-only reviewer is recognised as a
    maintainer (private members 404 — they fall back to the MAINTAINERS seed).
    Cached per run so N reactions by one maintainer cost one API call."""
    org = repo.split("/")[0]
    k = (org, login)
    if k not in _ORG_MEMBER:
        r = subprocess.run(["gh", "api", f"orgs/{org}/members/{login}", "--silent"],
                           capture_output=True, text=True)
        _ORG_MEMBER[k] = (r.returncode == 0)  # 204 -> 0 (member), 404 -> nonzero
    return _ORG_MEMBER[k]


def is_merged(pr, repo="leanprover-community/mathlib4"):
    """True if the PR merged. Mathlib merges via BORS, which closes the PR (state
    'closed', merged=false) and prefixes the title '[Merged by Bors] - …' —
    GitHub's own 'merged'/MERGED only covers the green-button path."""
    j = gh_obj(repo, f"pulls/{pr}")
    if j.get("merged") or j.get("merged_at"):
        return True
    return j.get("state") == "closed" and j.get("title", "").startswith("[Merged by Bors]")


def status_of(body):
    if re.search(r"Deletion candidate", body):
        return "flag"
    m = re.search(r"\((approve|revise|reject)\)", body)
    return m.group(1) if m else ""


def note_text_inline(body):
    # The blockquoted reviewer note in a wikilean-review comment.
    lines = [l[2:] if l.startswith("> ") else "" for l in body.split("\n")]
    return "\n".join(l for l in lines if l).strip()


def parse_pasted(body):
    body = body.replace("\r\n", "\n").replace("\r", "\n")
    out, cur, note, st = {}, None, [], [""]
    def flush():
        if cur:
            out[cur] = {"status": st[0], "note": " ".join(note).strip()}
    for ln in body.split("\n"):
        h = re.match(r"^-\s*\*\*\[(Q\d+)\]", ln)
        if h:
            flush(); cur = h.group(1); st = [""]; note = []; continue
        if not cur:
            continue
        sm = re.match(r"^\s*-\s*status:.*\*\*(approve|revise|reject|flag)\*\*", ln)
        if sm:
            st[0] = sm.group(1); note = []; continue
        nb = re.match(r"^\s*-\s+(.*)$", ln)
        if nb:
            note = [nb.group(1)]; continue
        if note and re.match(r"^\s+\S", ln):
            note.append(ln.strip())
    flush()
    return out


def classify(pr, repo="leanprover-community/mathlib4"):
    meta = gh_obj(repo, f"pulls/{pr}")
    created = dt.datetime.fromisoformat(meta["created_at"].replace("Z", "+00:00"))
    age_h = (dt.datetime.now(dt.timezone.utc) - created).total_seconds() / 3600
    head_sha = meta["head"]["sha"]

    diff = subprocess.run(["gh", "pr", "diff", str(pr), "--repo", repo],
                          capture_output=True, text=True).stdout
    tags, decl_line, f = [], {}, None
    lines = diff.split("\n")
    for idx, ln in enumerate(lines):
        if ln.startswith("+++ b/"):
            f = ln[6:]
        elif ln.startswith("+") and not ln.startswith("++"):
            m = re.search(r"wikidata\s+(Q\d+)", ln)
            if m:
                tags.append(m.group(1))
                decl_line[m.group(1)] = (f, idx)
    tags = list(dict.fromkeys(tags))

    # explicit[(qid, login)] = (status, ts); notes[(qid, login)] = text
    explicit, notes, reviewers = {}, {}, set()
    # Maintainers/reviewers: anyone whose GitHub author_association on the PR is an
    # org role, seeded by the explicit allowlist. Their verdict trumps, and the
    # settle gate needs >=1 of them.
    maint = set(MAINTAINERS)
    def record_assoc(login, assoc):
        if login and login not in BOTS and assoc in ORG_ROLES:
            maint.add(login)
    def note_explicit(qid, login, status, ts, text=""):
        if not login or login in BOTS:
            return
        reviewers.add(login)
        k = (qid, login)
        if k not in explicit or ts > explicit[k][1]:
            explicit[k] = (status, ts)
            if text:
                notes[k] = text

    for c in gh_list(repo, f"pulls/{pr}/comments"):
        u = (c.get("user") or {}).get("login", "")
        record_assoc(u, c.get("author_association"))
        b = c.get("body", "") or ""
        m = re.search(r"wikilean-review:(Q\d+)", b)
        if m:
            note_explicit(m.group(1), u, status_of(b) or "(note)", c.get("created_at", ""),
                          note_text_inline(b))
        # 👍/👎 reactions on the crossref bot's per-tag comment count as approve/reject
        # by the REACTOR, for that tag — a lightweight alternative to a written verdict.
        # Latest-wins (note_explicit keys by ts), so a reaction can overturn an earlier
        # comment by the same login and vice-versa. Only the crossref per-tag comment
        # (`crossref-bot:Q…`) is harvested — reacting to a verdict note would be ambiguous.
        cr = re.search(r"crossref-bot:(Q\d+)", b)
        rx = c.get("reactions") or {}
        if cr and (rx.get("+1", 0) + rx.get("-1", 0)) > 0:
            qid = cr.group(1)
            for r in gh_reactions(repo, c.get("id")):
                v = REACTION_VERDICT.get(r.get("content"))
                ru = (r.get("user") or {}).get("login", "")
                if not v or not ru or ru in BOTS:
                    continue
                if ru not in maint and is_org_member(repo, ru):
                    maint.add(ru)  # a maintainer's reaction trumps, like a maintainer comment
                note_explicit(qid, ru, v, r.get("created_at", ""))
    for c in gh_list(repo, f"issues/{pr}/comments"):
        u = (c.get("user") or {}).get("login", "")
        record_assoc(u, c.get("author_association"))
        b = c.get("body", "") or ""
        if u in BOTS:
            continue
        if re.search(r"##\s*WikiLean review", b) or "wikilean.jackmccarthy.org/review" in b:
            for q, info in parse_pasted(b).items():
                note_explicit(q, u, info["status"] or "(note)", c.get("created_at", ""), info["note"])

    pr_state = {}
    for r in gh_list(repo, f"pulls/{pr}/reviews"):
        u = (r.get("user") or {}).get("login", "")
        record_assoc(u, r.get("author_association"))
        if not u or u in BOTS:
            continue
        ts = r.get("submitted_at", "") or ""
        if u not in pr_state or ts > pr_state[u][1]:
            pr_state[u] = (r.get("state", ""), ts)
    blanket = {u for u, (stt, _) in pr_state.items() if stt == "APPROVED"}
    reviewers |= blanket

    def decide(qid):
        ex = {u: stt for (q, u), (stt, _) in explicit.items() if q == qid}
        real = {u: stt for u, stt in ex.items() if stt != "(note)"}
        mver = [stt for u, stt in real.items() if u in maint]
        if mver:
            if any(s in OBJECT for s in mver):
                return "recycle", ex, "maintainer rejected/revised"
            if "approve" in mver:
                return "green", ex, "maintainer approved"
        st = dict(real)
        for u in blanket:
            st.setdefault(u, "approve")
        vals = list(st.values())
        if any(s in OBJECT for s in vals):
            return "recycle", ex, "objection (reject/revise)"
        if "approve" in vals:
            return "green", ex, "approved (>=1 review, no objection)"
        return "recycle", ex, "no review"

    green, recycle = [], []
    for q in tags:
        verdict, ex, why = decide(q)
        rec = {
            "qid": q,
            "file": decl_line.get(q, (None, None))[0],
            "verdicts": ex,
            "reason": why,
            "notes": [
                {"login": u, "status": ex[u], "text": notes.get((q, u), "")}
                for u in ex if notes.get((q, u))
            ],
        }
        (green if verdict == "green" else recycle).append(rec)

    maint_reviewers = sorted(reviewers & maint)
    gate_ok = len(reviewers) >= 2 and bool(maint_reviewers)
    return {
        "pr": pr, "repo": repo, "head_sha": head_sha, "age_h": round(age_h, 1),
        "reviewers": sorted(reviewers), "blanket_approvers": sorted(blanket),
        "maintainer_reviewers": maint_reviewers,
        "gate": gate_ok,
        "gate_reasons": {"two_reviewers": len(reviewers) >= 2, "has_maintainer": bool(maint_reviewers)},
        "tags": tags, "green": green, "recycle": recycle,
    }


if __name__ == "__main__":
    pr = int([a for a in sys.argv[1:] if a.isdigit()][0]) if any(a.isdigit() for a in sys.argv[1:]) else 40682
    r = classify(pr)
    print(f"PR #{r['pr']} · reviewers {r['reviewers']} · maintainer-reviewers {r['maintainer_reviewers']}")
    print(f"GATE: 2-reviewers={r['gate_reasons']['two_reviewers']} >=1-maintainer={r['gate_reasons']['has_maintainer']} -> {'OPEN' if r['gate'] else 'WAIT'}")
    print(f"\nGREEN ({len(r['green'])}/{len(r['tags'])}): {[g['qid'] for g in r['green']]}")
    print(f"\nRECYCLE ({len(r['recycle'])}):")
    for e in r["recycle"]:
        print(f"  {e['qid']} ({e['file']}): {e['reason']}  {e['verdicts']}")
        for nt in e["notes"]:
            print(f"      {nt['login']} [{nt['status']}]: {nt['text'][:100]}")
