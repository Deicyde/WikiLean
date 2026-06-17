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

MAINTAINERS = {"jcommelin"}          # explicit verdict trumps everything
OBJECT = ("reject", "revise", "flag")  # hard objections (a note is NOT one)
BOTS = {"github-actions[bot]"}


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
        b = c.get("body", "") or ""
        m = re.search(r"wikilean-review:(Q\d+)", b)
        if m:
            note_explicit(m.group(1), u, status_of(b) or "(note)", c.get("created_at", ""),
                          note_text_inline(b))
    for c in gh_list(repo, f"issues/{pr}/comments"):
        u = (c.get("user") or {}).get("login", "")
        b = c.get("body", "") or ""
        if u in BOTS:
            continue
        if re.search(r"##\s*WikiLean review", b) or "wikilean.jackmccarthy.org/review" in b:
            for q, info in parse_pasted(b).items():
                note_explicit(q, u, info["status"] or "(note)", c.get("created_at", ""), info["note"])

    pr_state = {}
    for r in gh_list(repo, f"pulls/{pr}/reviews"):
        u = (r.get("user") or {}).get("login", "")
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
        mver = [stt for u, stt in real.items() if u in MAINTAINERS]
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

    gate_ok = len(reviewers) >= 2 and age_h >= 24
    return {
        "pr": pr, "repo": repo, "head_sha": head_sha, "age_h": round(age_h, 1),
        "reviewers": sorted(reviewers), "blanket_approvers": sorted(blanket),
        "gate": gate_ok, "gate_reasons": {"two_reviewers": len(reviewers) >= 2, "24h": age_h >= 24},
        "tags": tags, "green": green, "recycle": recycle,
    }


if __name__ == "__main__":
    pr = int([a for a in sys.argv[1:] if a.isdigit()][0]) if any(a.isdigit() for a in sys.argv[1:]) else 40682
    r = classify(pr)
    print(f"PR #{r['pr']} · age {r['age_h']}h · reviewers {r['reviewers']} · maintainers {sorted(MAINTAINERS)}")
    print(f"GATE: 2-reviewers={r['gate_reasons']['two_reviewers']} 24h={r['gate_reasons']['24h']} -> {'OPEN' if r['gate'] else 'WAIT'}")
    print(f"\nGREEN ({len(r['green'])}/{len(r['tags'])}): {[g['qid'] for g in r['green']]}")
    print(f"\nRECYCLE ({len(r['recycle'])}):")
    for e in r["recycle"]:
        print(f"  {e['qid']} ({e['file']}): {e['reason']}  {e['verdicts']}")
        for nt in e["notes"]:
            print(f"      {nt['login']} [{nt['status']}]: {nt['text'][:100]}")
