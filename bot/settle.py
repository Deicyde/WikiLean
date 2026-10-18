#!/usr/bin/env python3
"""Settle the brain of the daily @[wikidata] batch bot — DETERMINISTIC.

Reads an upstream PR's reviews and computes the gate + per-tag green/recycle
split. No mutations. Importable (`classify(pr)`) and runnable (prints a report).

Rule (see bot/README.md):
  - Gate: >=2 distinct people with a recorded review on the PR, the PR author
    included, >=1 of them a "maintainer": MAINTAINERS, an OWNER/MEMBER/COLLABORATOR
    association on the PR, or a public org member who reacted. No PR age is checked.
  - A bare note/comment = "defer to the other reviewer" -> ignored.
  - A maintainer's explicit approve/reject/revise/flag (a 👍/👎 reaction included) TRUMPS.
  - Else: any reject/revise/flag -> recycle; else >=1 approve (the author's own
    included) -> green; else recycle.
"""
import json, subprocess, re, sys, datetime as dt

# Always treated as maintainer: the "Mathlib maintainers" team in
# leanprover-community/leanprover-community.github.io data/teams.yaml (commit
# 5b8381a4, 2026-09-13), as GitHub logins (each checked with `gh api users/<login>`).
# Needed for maintainers whose org membership is PRIVATE: their author_association
# shows CONTRIBUTOR and the members probe 404s, so without the list their verdicts
# lose the maintainer trump — batch 6 bug: faenuccio's post-settle rejects were
# trumped by an earlier approve and never re-trimmed the PR. Update it when the
# team changes.
MAINTAINERS = frozenset({
    "adamtopaz", "ADedecker", "adomani", "b-mehta", "bryangingechen", "digama0",
    "dupuisf", "eric-wieser", "faenuccio", "fpvandoorn", "grunweg", "hrmacbeth",
    "j-loreaux", "jcommelin", "joelriou", "kbuzzard", "kim-em", "kmill",
    "mattrobball", "ocfnash", "PatrickMassot", "RemyDegenne", "riccardobrasca",
    "robertylewis", "sgouezel", "TwoFX", "urkud", "Vierkantor",
})
_MAINTAINERS_LC = frozenset(m.lower() for m in MAINTAINERS)
# GitHub author_association values that mark a Mathlib maintainer/reviewer.
ORG_ROLES = {"OWNER", "MEMBER", "COLLABORATOR"}
OBJECT = ("reject", "revise", "flag")  # hard objections (a note is NOT one)
BOTS = {"github-actions[bot]"}
REACTION_VERDICT = {"+1": "approve", "-1": "reject"}  # 👍 = approve, 👎 = reject (REST content names)


def is_maintainer(login):
    """Is `login` on MAINTAINERS? GitHub logins are case-insensitive."""
    return bool(login) and login.lower() in _MAINTAINERS_LC


class GhError(RuntimeError):
    """A gh call failed or answered with nothing usable. Never read it as 'wait':
    an expired token once kept the poller green and blind for three weeks."""


def gh_failure(args, r):
    """One-line reason for a failed gh call. gh puts the reason on stderr; `gh api`
    also prints the JSON error body on stdout. Neither carries the token itself."""
    detail = " ".join((r.stderr or "").split()) or " ".join((r.stdout or "").split()) or "no output"
    msg = f"gh {' '.join(str(a) for a in args[:3])} failed (exit {r.returncode}): {detail[:300]}"
    if "HTTP 401" in detail or "Bad credentials" in detail or '"status":"401"' in detail:
        msg += (" -- GH_TOKEN is missing, expired or revoked (CI: MATHLIB_FORK_PAT;"
                " see 'Token rotation' in bot/README.md)")
    return msg


def gh_run(args):
    """stdout of `gh <args>`; GhError on a non-zero exit."""
    r = subprocess.run(["gh", *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise GhError(gh_failure(args, r))
    return r.stdout


def gh_list(repo, path):
    s = gh_run(["api", f"repos/{repo}/{path}", "--paginate", "-q", ".[]"])
    items, dec, i, n = [], json.JSONDecoder(), 0, len(s)
    while i < n:
        while i < n and s[i] in " \t\r\n":
            i += 1
        if i >= n:
            break
        try:
            obj, i = dec.raw_decode(s, i)
        except json.JSONDecodeError as ex:
            raise GhError(f"gh api {path}: unparseable output ({ex})") from ex
        items.append(obj)
    return items


def gh_obj(repo, path):
    out = gh_run(["api", f"repos/{repo}/{path}"])
    if not out.strip():
        raise GhError(f"gh api {path}: empty output")
    try:
        return json.loads(out)
    except json.JSONDecodeError as ex:
        raise GhError(f"gh api {path}: unparseable output ({ex})") from ex


def gh_reactions(repo, comment_id):
    """Reactions on an inline PR review comment. A failed fetch raises: a dropped
    thumbs-down would turn a rejected tag green."""
    return gh_list(repo, f"pulls/comments/{comment_id}/reactions")


_ORG_MEMBER = {}
def is_org_member(repo, login):
    """Is `login` a PUBLIC member of the repo's org? Reaction objects carry no
    author_association, so this is how a react-only reviewer is recognised as a
    maintainer (private members 404 — they fall back to the MAINTAINERS list).
    Cached per run so N reactions by one maintainer cost one API call."""
    org = repo.split("/")[0]
    k = (org, login)
    if k not in _ORG_MEMBER:
        args = ["api", f"orgs/{org}/members/{login}", "--silent"]
        r = subprocess.run(["gh", *args], capture_output=True, text=True)
        if r.returncode != 0 and "HTTP 404" not in (r.stderr or ""):
            raise GhError(gh_failure(args, r))
        _ORG_MEMBER[k] = (r.returncode == 0)  # 204 -> 0 (member), 404 -> not a public member
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

    diff = gh_run(["pr", "diff", str(pr), "--repo", repo])
    if not diff.strip():
        # A batch PR always adds tags; an empty one had every tag recycled by hand.
        raise GhError(f"gh pr diff {pr}: empty diff (if every tag was recycled, close the PR)")
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
    # Maintainers/reviewers: everyone on MAINTAINERS, plus anyone whose GitHub
    # author_association on the PR is an org role. Their verdict trumps, and the
    # settle gate needs >=1 of them.
    maint = set()
    def is_maint(login):
        return is_maintainer(login) or login in maint
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
                if not is_maint(ru) and is_org_member(repo, ru):
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
        mver = [stt for u, stt in real.items() if is_maint(u)]
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

    maint_reviewers = sorted(u for u in reviewers if is_maint(u))
    gate_ok = len(reviewers) >= 2 and bool(maint_reviewers)
    return {
        "pr": pr, "repo": repo, "head_sha": head_sha, "age_h": round(age_h, 1),
        "reviewers": sorted(reviewers), "blanket_approvers": sorted(blanket),
        "maintainer_reviewers": maint_reviewers,
        "gate": gate_ok,
        "gate_reasons": {"two_reviewers": len(reviewers) >= 2, "has_maintainer": bool(maint_reviewers)},
        "tags": tags, "green": green, "recycle": recycle,
    }


def ready_header(cls):
    """The settle comment's first line, stating the rule classify() applied. It names
    reviewers in code font, without @, so it adds no mentions of its own (the tag
    table below it already lists reviewers)."""
    green, recycle = cls["green"], cls["recycle"]
    who = ", ".join(f"`{u}`" for u in cls["reviewers"]) or "none"
    return (f"This PR was trimmed to the **{len(green)}** `@[wikidata]` tags with at least one approval 🟢 "
            f"and no reject/revise/flag recorded by this bot that a maintainer's verdict did not override "
            f"(GitHub \"Request changes\" reviews and free-text comments are not counted). Under this bot's "
            f"rule one approval per tag is enough, and it can be the PR author's own or a single 👍 reaction. "
            f"Only a per-tag verdict or 👍/👎 from a Mathlib maintainer listed in the bot or someone GitHub "
            f"shows the bot as a mathlib4 owner, member or collaborator overrides the others; a GitHub PR "
            f"approval never does. "
            f"Reviews recorded from: {who}. **Ready for a maintainer's review.**"
            + (f" {len(recycle)} removed in this trim." if recycle else "")
            + " <!-- wikilean-bot-ready -->")


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
