#!/usr/bin/env python3
"""Harvest reviewer corrections of @[wikidata] tags into a learning dataset — DETERMINISTIC.

Part of the tagging-pipeline feedback loop. Two kinds of reviewer feedback are
captured into `bot/data/corrections.jsonl` (one JSON record per line, discriminated
by a `kind` field):
  - "correction": a tag a reviewer marked **reject**/**revise**, with the suggested
    fix (a more specific QID and/or a different declaration).
  - "addition": an **approved** tag whose note still proposes tagging a RELATED
    declaration too (e.g. "we should also tag `…OneVariable`") — a queue candidate.
So the pipeline can both learn from mistakes and harvest reviewer-proposed new tags.

NO LLM calls. It only reads GitHub via the `gh` CLI (through settle.py) and
extracts everything with regex. The rejected tags are usually TRIMMED out of the
PR diff once it's settled, so we parse the immutable pasted "## WikiLean review"
COMMENTS (not the diff) — those survive the trim.

  harvest_corrections.py <pr> [--repo leanprover-community/mathlib4]
                              [--out bot/data/corrections.jsonl]

Idempotent: dedupe by (pr, qid, reviewer) — re-running upserts, never duplicates.
"""
import argparse, json, re, sys
from pathlib import Path

import settle

HERE = Path(__file__).resolve().parent
DEFAULT_OUT = HERE / "data" / "corrections.jsonl"
REPO = "leanprover-community/mathlib4"

# A review line looks like:
#   - **[Q11210](https://www.wikidata.org/wiki/Q11210)** Coordinate system — `Mathlib/.../Defs.lean:88`
# Capture qid, the label (between the link and the em-dash), and the first
# backticked `path:line` (the file). The dash can be an em-dash (—), en-dash, or
# a plain hyphen surrounded by spaces.
LINE_RE = re.compile(
    r"^-\s*\*\*\[(Q\d+)\]\([^)]*\)\*\*\s*"      # - **[Qxxx](url)**
    r"(?P<label>.*?)\s*"                          # label (lazy, up to the dash)
    r"(?:[—–]|\s-\s)\s*"                          # — / – / " - "
    r"`(?P<file>[^`]+)`"                          # `file:line`
)
QID_RE = re.compile(r"\bQ\d+\b")
# A QID immediately followed by a parenthetical label: Q189569 (Basis)
QID_PAREN_RE = re.compile(r"\b(Q\d+)\s*\(([^)]+)\)")
# An intent phrase that flags the QID/decl that follows as the SUGGESTION.
INTENT_BEFORE = re.compile(
    r"(?:tag\b.*?\bwith|tag\b|instead\b|want\b|use\b|prefer\b|should\b)",
    re.IGNORECASE,
)
NARROW_RE = re.compile(r"\bnarrow\b|\bjust the\b|\bspecial case\b|\bonly\b|\bmore general\b",
                       re.IGNORECASE)
# An APPROVED tag can still carry a note proposing an ADDITIONAL, related tag.
# Capture those as kind="addition" (queue candidates), e.g.
#   "We should also tag `…JacobiTheta.OneVariable`"
#   "Do we also want to tag `Quotient` or `Quot`?"
#   "we could add another `attribute` command below this def, that tags those"
ADDITION_RE = re.compile(
    r"\balso\b[^.\n]*\btag\b|\bshould\s+also\b|\badd(?:ing)?\s+another\b[^.\n]*\b(?:attribute|tag)",
    re.IGNORECASE)


def _normalize(body):
    return body.replace("\r\n", "\n").replace("\r", "\n")


def parse_line_meta(body):
    """Per-qid {"label", "file"} parsed from each review line's header.

    Complements settle.parse_pasted (which gives status + note) with the two
    extra fields that live on the bullet line itself.
    """
    meta = {}
    for ln in _normalize(body).split("\n"):
        m = LINE_RE.match(ln)
        if m:
            label = m.group("label").strip()
            # The file token is `path:line` — keep the path, drop the line no.
            f = m.group("file").strip()
            f = re.sub(r":\d+\s*$", "", f)
            meta[m.group(1)] = {"label": label or None, "file": f or None}
    return meta


def extract_suggested_qid(note, orig_qid):
    """A Q-number in the note that is NOT the original qid, plus its label.

    Preference order:
      1. A `Qxxx (Label)` parenthetical whose qid != orig.
      2. A Qxxx that follows an intent phrase ("tag … with", "instead", …).
      3. Any other Qxxx != orig.
    Returns (suggested_qid | None, suggested_qid_label | None).
    """
    if not note:
        return None, None

    # 1) parenthetical-labelled candidates first (strongest signal).
    for qid, label in QID_PAREN_RE.findall(note):
        if qid != orig_qid:
            return qid, label.strip() or None

    # 2) a qid that sits after an intent phrase.
    intent_hit = None
    for m in QID_RE.finditer(note):
        qid = m.group(0)
        if qid == orig_qid:
            continue
        prefix = note[:m.start()]
        if INTENT_BEFORE.search(prefix):
            intent_hit = qid
            break
    if intent_hit:
        return intent_hit, _label_for(note, intent_hit)

    # 3) any remaining qid != orig.
    for m in QID_RE.finditer(note):
        qid = m.group(0)
        if qid != orig_qid:
            return qid, _label_for(note, qid)

    return None, None


def _label_for(note, qid):
    """A parenthetical label for `qid` if one happens to follow it."""
    m = re.search(re.escape(qid) + r"\s*\(([^)]+)\)", note)
    return m.group(1).strip() if m else None


def extract_suggested_decl(note, orig_decl):
    """A backticked decl the reviewer wants tagged instead — best effort.

    Looks for the backticked token in "tag `X` with" / "use `X`" / "should tag
    `X`" / "prefer `X`" / "instead … `X`" shapes. Returns the decl or None.
    Skips file paths and anything that isn't a Lean identifier (dotted
    identifiers like `Module.Basis`, `trapezoidal_integral`) — that rules out
    math notation such as `G(V,p)` and the original decl.
    """
    if not note:
        return None
    # A Lean decl name: dotted identifiers, letters/digits/_/' / unicode-subscript.
    is_decl = re.compile(r"^[A-Za-z_][\w.'₀-₉]*$")
    candidates = []
    for m in re.finditer(r"`([^`]+)`", note):
        tok = m.group(1).strip()
        if not is_decl.match(tok) or tok.endswith(".lean"):
            continue  # file path or math notation, not a decl
        prefix = note[max(0, m.start() - 60):m.start()]
        if re.search(r"\b(tag|use|prefer|instead|should)\b", prefix, re.IGNORECASE):
            candidates.append(tok)
    for tok in candidates:
        if orig_decl is None or tok != orig_decl:
            return tok
    return None


def classify_failure_mode(note, suggested_qid, suggested_decl, orig_decl):
    """Best-effort bucket for WHY the tag was wrong (nullable).

    Spec priority order — a present `suggested_qid` is the dominant signal that
    the QID itself was too broad (the reviewer named a more specific one), so it
    is checked before the narrow/general note heuristic. This is what makes the
    canonical Q11210 case ("too general … tag `Basis` with Q189569") classify as
    `qid_too_broad` even though it also carries a suggested_decl.
    """
    diff_decl = suggested_decl is not None and suggested_decl != orig_decl
    if suggested_qid:
        return "qid_too_broad"
    if diff_decl:
        return "wrong_decl"
    if note and NARROW_RE.search(note):
        return "decl_too_narrow"
    if note:
        return "other"
    return None


def harvest(pr, repo=REPO):
    """Return a list of correction records for the PR (no I/O)."""
    maint = set(settle.MAINTAINERS)
    records = []
    for c in settle.gh_list(repo, f"issues/{pr}/comments"):
        login = (c.get("user") or {}).get("login", "")
        assoc = c.get("author_association")
        body = c.get("body", "") or ""
        if login in settle.BOTS:
            continue
        if "## WikiLean review" not in body and not re.search(r"##\s*WikiLean review", body):
            continue

        # Maintainer if org-role association OR on the allowlist.
        is_maint = (assoc in settle.ORG_ROLES) or (login in settle.MAINTAINERS)
        if is_maint:
            maint.add(login)

        per_qid = settle.parse_pasted(body)     # {qid: {status, note}}
        line_meta = parse_line_meta(body)       # {qid: {label, file}}

        for qid, info in per_qid.items():
            status = info.get("status") or ""
            note = info.get("note") or ""
            if status in ("reject", "revise"):
                kind = "correction"
            elif note and ADDITION_RE.search(note) and status in ("approve", ""):
                # An approved (or bare-note/defer) tag whose note proposes a
                # related tag too. ADDITION_RE is specific enough that a bare note
                # only qualifies when it actually says "also tag …".
                kind = "addition"
            else:
                continue
            meta = line_meta.get(qid, {})
            label = meta.get("label")
            file = meta.get("file")
            decl = None  # the review line does NOT carry the decl name (see header)

            sug_qid, sug_qid_label = extract_suggested_qid(note, qid)
            sug_decl = extract_suggested_decl(note, decl)
            fmode = (classify_failure_mode(note, sug_qid, sug_decl, decl)
                     if kind == "correction" else None)

            records.append({
                "pr": pr,
                "kind": kind,
                "qid": qid,
                "label": label,
                "decl": decl,
                "file": file,
                "status": status,
                "reviewer": login,
                "is_maintainer": is_maint,
                "note": note,
                "suggested_qid": sug_qid,
                "suggested_qid_label": sug_qid_label,
                "suggested_decl": sug_decl,
                "failure_mode": fmode,
            })
    return records


def upsert(out_path, records):
    """Append/update records in the jsonl, deduped by (pr, qid, reviewer).

    Reads the existing file, keys every row by (pr, qid, reviewer), overwrites
    matching keys with the freshly-harvested rows, and rewrites the file —
    preserving insertion order (existing rows stay put; new rows append).
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    order, by_key = [], {}
    if out_path.exists():
        for ln in out_path.read_text().splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                row = json.loads(ln)
            except json.JSONDecodeError:
                continue
            k = (row.get("pr"), row.get("qid"), row.get("reviewer"))
            if k not in by_key:
                order.append(k)
            by_key[k] = row

    added = updated = 0
    for rec in records:
        k = (rec["pr"], rec["qid"], rec["reviewer"])
        if k in by_key:
            updated += 1
        else:
            order.append(k)
            added += 1
        by_key[k] = rec

    with out_path.open("w") as fh:
        for k in order:
            fh.write(json.dumps(by_key[k], ensure_ascii=False) + "\n")
    return added, updated


def print_summary(pr, records):
    n_corr = sum(1 for r in records if r.get("kind") == "correction")
    n_add = sum(1 for r in records if r.get("kind") == "addition")
    print(f"Harvested {len(records)} feedback record(s) from PR #{pr} "
          f"({n_corr} correction, {n_add} addition):\n")
    if not records:
        print("  (none)")
        return
    cols = [("kind", 10), ("qid", 10), ("status", 7), ("reviewer", 12),
            ("suggested_qid", 13), ("suggested_decl", 24), ("failure_mode", 15)]
    header = "  " + "  ".join(name.ljust(w) for name, w in cols)
    print(header)
    print("  " + "  ".join("-" * w for _, w in cols))
    for r in records:
        row = []
        for name, w in cols:
            v = r.get(name)
            row.append(("" if v is None else str(v)).ljust(w))
        print("  " + "  ".join(row))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pr", type=int, help="Mathlib PR number")
    ap.add_argument("--repo", default=REPO)
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="corrections.jsonl path")
    args = ap.parse_args()

    records = harvest(args.pr, args.repo)
    added, updated = upsert(args.out, records)
    print_summary(args.pr, records)
    print(f"\n-> {args.out}: {added} added, {updated} updated "
          f"({len(records)} harvested this run)")


if __name__ == "__main__":
    main()
