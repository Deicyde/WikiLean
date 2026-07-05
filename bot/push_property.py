#!/usr/bin/env python3
"""Auto-populate Wikidata P14534 (Mathlib Declaration ID) from merged @[wikidata] tags.

Reads bot/data/property_seed_autopush.csv — the PURE source-verified subset
(export_property_seed.py's AUTO_OK: decl read straight from the merged tag site,
no catalog tiebreaks) — diffs it against LIVE Wikidata, and POSTs only the
net-new statements to the QuickStatements batch API as the property steward.
Deterministic (no LLM), matching the bot pipeline invariant.

The merge IS the human-review gate: a merged @[wikidata] tag passed >=2
reviewers + a maintainer + CI upstream, so auto-submitting it respects the
"review before Wikidata" rule. Anti-slop guards on top:
  - GATED: no-op unless WIKILEAN_PUSH_PROPERTY=1 AND QUICKSTATEMENTS_TOKEN set.
  - DRY-RUN by default: computes/prints net-new, POSTs nothing. --submit to push.
  - NEVER OVERWRITES: a QID that already carries a DIFFERENT P14534 value is a
    CONFLICT (logged, skipped), never auto-changed — that's a human decision.
  - SAFETY CAP: refuses to auto-submit more than MAX_AUTO net-new at once.
  - Every edit lands in EditGroups (visible, undoable) under the account.
  - Existence was already verified at export time (qualify() vs the doc-gen4
    declaration index); the live net-new diff here is the idempotency oracle.

Run: WIKILEAN_PUSH_PROPERTY=1 QUICKSTATEMENTS_TOKEN=… python3 bot/push_property.py --submit
"""
import json
import os
import subprocess
import sys
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
AUTOPUSH = HERE / "data" / "property_seed_autopush.csv"
WDQS = "https://query.wikidata.org/sparql"
QS_API = "https://quickstatements.toolforge.org/api.php"
UA = "WikiLean-push/1.0 (https://wikilean.jackmccarthy.org; jack.mccarthy.1@stonybrook.edu)"
PROP = "P14534"
MAX_AUTO = 50  # a net-new batch bigger than this is suspicious → refuse, submit by hand


def curl(args, timeout=90, stdin=None):
    return subprocess.run(["curl", "-sS", "-m", str(timeout)] + args,
                          input=stdin, capture_output=True, text=True, check=True).stdout


def sparql(query: str) -> list[dict]:
    # --fail so a WDQS 4xx/5xx exits non-zero (raises) instead of returning an
    # error-page body that json.loads might mis-handle.
    out = curl(["--fail", "-H", "Accept: application/sparql-results+json",
                "-H", f"User-Agent: {UA}", "--data-urlencode", f"query={query}", WDQS])
    return json.loads(out)["results"]["bindings"]


def live_p14534() -> tuple[dict[str, set[str]], int, int]:
    """(qid → set of live decls, rows_returned, count). The row listing is
    cross-checked against a SEPARATE COUNT aggregate: a truncated/partial WDQS
    200 (the silent under-report failure mode) makes them disagree, so the
    caller aborts rather than treating already-live rows as net-new — which
    would append duplicate reference blocks to public statements."""
    rows = sparql(f"SELECT ?i ?v WHERE {{ ?i wdt:{PROP} ?v }}")
    live: dict[str, set[str]] = {}
    for r in rows:
        live.setdefault(r["i"]["value"].rsplit("/", 1)[1], set()).add(str(r["v"]["value"]))
    cnt = sparql(f"SELECT (COUNT(*) AS ?c) WHERE {{ ?i wdt:{PROP} ?v }}")
    expected = int(cnt[0]["c"]["value"]) if cnt else -1
    return live, len(rows), expected


def parse_autopush() -> list[tuple[str, str, str]]:
    """[(qid, decl, full_v1_line)] from the autopush csv (comments skipped)."""
    rows = []
    for line in AUTOPUSH.read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 3 and parts[0].startswith("Q"):
            rows.append((parts[0], parts[2].strip('"'), line))
    return rows


def main() -> int:
    submit = "--submit" in sys.argv
    gated = os.environ.get("WIKILEAN_PUSH_PROPERTY") == "1"
    token = os.environ.get("QUICKSTATEMENTS_TOKEN")
    user = os.environ.get("QUICKSTATEMENTS_USER", "Mynus grey")
    if not AUTOPUSH.exists():
        print("push_property: no autopush file — run export_property_seed.py first")
        return 0
    rows = parse_autopush()
    try:
        live, got, expected = live_p14534()
    except Exception as e:  # noqa: BLE001 — never crash the poller
        print(f"push_property: SPARQL failed ({type(e).__name__}) — skipping this tick")
        return 0
    if expected < 0 or got != expected:
        print(f"  ABORT: WDQS row listing has {got} P14534 statements but COUNT says "
              f"{expected} — truncated/partial result; not submitting this tick.")
        return 2

    net_new, conflicts, done = [], [], 0
    for qid, decl, line in rows:
        cur = live.get(qid, set())
        if decl in cur:
            done += 1                    # already live → idempotent skip
        elif cur:
            conflicts.append((qid, decl, sorted(cur)))  # different value → human
        else:
            net_new.append((qid, decl, line))
    print(f"push_property: {len(rows)} pure rows | {done} already live | "
          f"{len(net_new)} net-new | {len(conflicts)} conflicts")
    for qid, decl, cur in conflicts:
        print(f"  CONFLICT {qid}: autopush wants {decl!r} but Wikidata has {cur} "
              f"— SKIPPED (needs human review)")
    if not net_new:
        print("  nothing to push.")
        return 0
    for qid, decl, _ in net_new[:20]:
        print(f"  + {qid}  {decl}")
    if len(net_new) > MAX_AUTO:
        print(f"  REFUSING: {len(net_new)} net-new exceeds MAX_AUTO={MAX_AUTO} — "
              f"suspicious (bad diff?); submit manually or raise the cap after checking.")
        return 2
    if not (gated and token and submit):
        why = [w for w, ok in (("WIKILEAN_PUSH_PROPERTY=1", gated),
                                ("QUICKSTATEMENTS_TOKEN", token),
                                ("--submit", submit)) if not ok]
        print(f"  DRY-RUN — not pushing (missing: {', '.join(why)}).")
        return 0

    # POST net-new to the QuickStatements batch API. The token rides in the POST
    # body via stdin (not argv), so it never appears in a process list / log.
    body = urllib.parse.urlencode({
        "action": "import", "format": "v1", "submit": "1",
        "username": user, "token": token,
        "batchname": "WikiLean P14534 auto (merged @[wikidata] tags)",
        "data": "\n".join(line for _, _, line in net_new),
    })
    try:
        resp = curl(["-X", "POST", "-H", f"User-Agent: {UA}", "--data", "@-", QS_API],
                    timeout=120, stdin=body)
        j = json.loads(resp)
    except Exception as e:  # noqa: BLE001
        print(f"  QS API call failed ({type(e).__name__}) — will retry next merge")
        return 1
    if j.get("status") == "OK" and j.get("batch_id"):
        print(f"  ✓ submitted QS batch {j['batch_id']} ({len(net_new)} statements) as {user}")
        print(f"    https://quickstatements.toolforge.org/#/batch/{j['batch_id']}")
        return 0
    print(f"  QS API returned an error: {j}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
