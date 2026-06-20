#!/usr/bin/env python3
"""Resolve reviewer-named concepts to Wikidata QIDs — DETERMINISTIC.

Some reject/revise notes name the concept the reviewer wants but give no QID:
  "...Asymptotic Analysis is a field of math, we want the entry specifically for 'Big O'"
`harvest_corrections.py` leaves those with suggested_qid=null. This fills them in
by searching Wikidata (wbsearchentities) for the named concept and scoring the
candidates for mathematical relevance, so the correction can flow into the queue
with the right narrow QID (e.g. Q623950 "big O notation") instead of repeating
the rejected broad one.

NO LLM. Conservative: only sets a QID when a candidate clears a confidence
threshold; otherwise leaves it null and reports it as needing review. Idempotent:
records that already have a suggested_qid are left untouched.

  resolve_concepts.py [--corrections bot/data/corrections.jsonl] [--dry-run]
"""
import argparse, json, re, subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
CORRECTIONS = HERE / "data" / "corrections.jsonl"
WD_API = "https://www.wikidata.org/w/api.php"
UA = "WikiLean-bot/1.0 (jack.mccarthy.1@stonybrook.edu)"

# P31 values that disqualify a candidate (not a math concept/object).
BAD_P31 = {
    "Q4167410",   # Wikimedia disambiguation page
    "Q5",         # human
    "Q11424",     # film
    "Q5398426",   # television series
    "Q482994",    # album
    "Q7725634",   # literary work
    "Q4167836",   # Wikimedia category
}
# Tokens in the label/description that signal a mathematical concept.
MATH_KW = re.compile(
    r"\b(math|mathematic|notation|theorem|lemma|function|space|algebra|geometr|"
    r"number|set|graph|group|ring|field|topolog|calculus|probabilit|statistic|"
    r"integral|matrix|vector|operator|asymptotic|polynomial|measure|manifold|"
    r"category|tensor|module|metric|norm|series|distribution|model|equation|"
    r"map|morphism|isomorphism|relation|order|lattice|sequence|limit)\b",
    re.IGNORECASE)
# A reviewer wraps the concept they want in quotes ("...the entry for 'Big O'").
# Double/curly quotes are reliable delimiters; the straight single quote is also a
# contraction apostrophe ("Newton's"), so only treat it as a quote when it is NOT
# flanked by letters/digits — otherwise "Newton's iterative method" parses as 's'.
_QUOTE_PATTERNS = [
    re.compile(r'"([^"]{2,40})"'),
    re.compile(r'“([^”]{2,40})”'),
    re.compile(r'‘([^’]{2,40})’'),
    re.compile(r"(?<![A-Za-z0-9])'([^']{2,40})'(?![A-Za-z0-9])"),
]
# A quoted phrase is only a CONCEPT the reviewer wants when a naming cue precedes
# it ("entry for 'Big O'", "QID for 'flow'"). This rejects emphasis-quoting like
# "the most 'canonical' one" (no cue) that would otherwise resolve to junk.
CUE_RE = re.compile(
    r"\b(for|entry|tag|tagged|use|using|want|wants|concept|called|namely|qid|"
    r"prefer|instead|specifically)\s*$", re.IGNORECASE)

THRESHOLD = 6   # minimum score to accept a resolution


def wb(params):
    url = WD_API + "?" + "&".join(f"{k}={v}" for k, v in params.items())
    try:
        out = subprocess.run(["curl", "-s", "-H", f"User-Agent: {UA}", url],
                             capture_output=True, text=True, timeout=40).stdout
        return json.loads(out)
    except Exception:
        return {}


def wb_search(term, limit=8):
    """wbsearchentities → [{id,label,description}] for an English concept term."""
    from urllib.parse import quote
    data = wb({"action": "wbsearchentities", "search": quote(term), "language": "en",
               "uselang": "en", "type": "item", "limit": str(limit), "format": "json"})
    return [{"id": r["id"], "label": r.get("label", ""), "description": r.get("description", "")}
            for r in data.get("search", [])]


def wb_meta(qids):
    """wbgetentities → {qid: {enwiki: bool, p31: set, label, description}}."""
    meta = {}
    if not qids:
        return meta
    data = wb({"action": "wbgetentities", "ids": "|".join(qids),
               "props": "sitelinks|claims|labels|descriptions", "languages": "en",
               "format": "json"})
    for q, e in data.get("entities", {}).items():
        p31 = {c["mainsnak"]["datavalue"]["value"]["id"]
               for c in e.get("claims", {}).get("P31", [])
               if c["mainsnak"].get("datavalue")}
        meta[q] = {
            "enwiki": "enwiki" in (e.get("sitelinks") or {}),
            "p31": p31,
            "label": (e.get("labels", {}).get("en", {}) or {}).get("value", ""),
            "description": (e.get("descriptions", {}).get("en", {}) or {}).get("value", ""),
        }
    return meta


def extract_concepts(note):
    """Quoted concept phrases from a free-text note.

    Only quoted phrases are used — that is the reliable "I mean THIS concept"
    signal. Phrases that look like declarations/paths/proof titles/sentences are
    dropped; a phrase must contain a real word (so a stray 's' from a contraction
    can't slip through)."""
    out, seen = [], set()

    def add(p):
        p = p.strip().strip(".,;:")
        if not p or p.lower() in seen:
            return
        if "/" in p or "`" in p or "_" in p or p.count(" ") > 4 or len(p) > 40:
            return  # decl / path / sentence shape, not a concept name
        if not any(len(w) >= 3 for w in re.findall(r"[A-Za-z]+", p)):
            return  # no real word (e.g. a leftover "s")
        seen.add(p.lower())
        out.append(p)

    for rx in _QUOTE_PATTERNS:
        for m in rx.finditer(note):
            before = note[max(0, m.start() - 20):m.start()]
            if CUE_RE.search(before):     # only quotes a naming cue points at
                add(m.group(1))
    return out


def norm(s):
    return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()


def score(meta, phrase):
    """Heuristic relevance score for a candidate entity vs the named phrase.

    Math relevance is MANDATORY — a candidate with no mathematical signal in its
    label/description is disqualified, so a popular non-math homonym (the video
    game "flOw", the chemical "sulfur") can never outrank the math concept."""
    if not meta["enwiki"] or (meta["p31"] & BAD_P31):
        return -100
    if not MATH_KW.search(meta["label"] + " " + meta["description"]):
        return -100   # not a mathematical concept
    s = 4   # math concept + enwiki article (both required above)
    lab, ph = norm(meta["label"]), norm(phrase)
    if lab == ph:
        s += 5
    elif ph and (ph in lab or lab in ph):
        s += 2
    return s


def resolve(note):
    """Return (qid, label, score, phrase) for the best math concept the note names,
    or None if nothing clears the threshold."""
    best = None
    for phrase in extract_concepts(note):
        cands = wb_search(phrase)
        meta = wb_meta([c["id"] for c in cands])
        for c in cands:
            m = meta.get(c["id"])
            if not m:
                continue
            sc = score(m, phrase)
            if best is None or sc > best[2]:
                best = (c["id"], m["label"] or c["label"], sc, phrase)
    if best and best[2] >= THRESHOLD:
        return best
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--corrections", type=Path, default=CORRECTIONS)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    rows = []
    for ln in args.corrections.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if ln:
            rows.append(json.loads(ln))

    resolved = ambiguous = 0
    for r in rows:
        if r.get("suggested_qid") or not r.get("note"):
            continue   # already has a QID (reviewer-named or previously resolved)
        hit = resolve(r["note"])
        if hit:
            qid, label, sc, phrase = hit
            conf = "high" if sc >= 8 else "medium"
            print(f"  RESOLVED {r['qid']} ({r.get('label')}) — note concept "
                  f"'{phrase}' -> {qid} ({label}) [{conf}, score {sc}]")
            if not args.dry_run:
                r["suggested_qid"] = qid
                r["suggested_qid_label"] = label
                r["resolved"] = True
                r["resolve_confidence"] = conf
                r["resolve_phrase"] = phrase
                if r.get("failure_mode") in (None, "other"):
                    r["failure_mode"] = "qid_too_broad"
            resolved += 1
        else:
            concepts = extract_concepts(r["note"])
            print(f"  ambiguous {r['qid']} ({r.get('label')}) — "
                  f"no confident match for {concepts or '(no concept phrase found)'}")
            ambiguous += 1

    print(f"\nresolved {resolved}, ambiguous {ambiguous} "
          f"(of {sum(1 for r in rows if not r.get('suggested_qid') and r.get('note'))} unresolved)")
    if not args.dry_run and resolved:
        with args.corrections.open("w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"wrote {args.corrections} (run apply_corrections.py to push fixes into the queue)")


if __name__ == "__main__":
    main()
