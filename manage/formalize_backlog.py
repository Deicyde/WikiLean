#!/usr/bin/env python3
"""Verify the formalize backlog against LIVE D1, then emit a targeted slug list.

The formalize worklist (manage/data/moderation_worklist.json) is computed from
the on-disk annotation layer, which lags the canonical D1 store — an article can
already be formalized in D1 (Agent-2 ran) while disk still shows only Agent-1
statements. Feeding those stale slugs to the moderation runner would waste tokens
re-reviewing done work. So here we GET each candidate's LIVE state and keep only
the ones D1 still reports as extracted (annotations present, none status-bearing).

Output: manage/data/formalize_slugs.txt — one D1-verified slug per line, in
worklist (centrality) order. Consumed by `moderate.py review --slugs`.

    python3 manage/formalize_backlog.py            # verify all, write the file
    python3 manage/formalize_backlog.py --limit 8  # cap the emitted list

Read-only against the public API; no token, no LLM, no writes upstream.
"""
from __future__ import annotations

import json
import subprocess
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
WORKLIST = DATA / "moderation_worklist.json"
OUT = DATA / "formalize_slugs.txt"
API_BASE = "https://wikilean.jackmccarthy.org"
_COUNTED = ("formalized", "partial", "not_formalized", "rejected")


def _live_state(slug: str) -> dict:
    """Return {slug, ok, n, n_status, extracted} for the live D1 article.

    Uses curl (system cert store) — matching bot/pool.py — because macOS Python
    can hit SSL CERTIFICATE_VERIFY_FAILED against the live host."""
    url = f"{API_BASE}/api/article/{urllib.parse.quote(slug)}.json"
    try:
        out = subprocess.run(
            ["curl", "-sS", "--retry", "2", "--retry-delay", "1", "-m", "20",
             "-H", "User-Agent: WikiLean-manage/1.0", url],
            capture_output=True, text=True, timeout=40)
        d = json.loads(out.stdout)
    except Exception as e:
        return {"slug": slug, "ok": False, "err": type(e).__name__}
    anns = d.get("annotations") or []
    n_status = sum(1 for a in anns if a.get("status") in _COUNTED)
    return {"slug": slug, "ok": True, "n": len(anns), "n_status": n_status,
            "extracted": len(anns) > 0 and n_status == 0, "version": d.get("version")}


def verify(limit: int | None = None) -> list[dict]:
    if not WORKLIST.exists():
        raise SystemExit("no moderation_worklist.json — run manage/refresh.py first")
    items = json.loads(WORKLIST.read_text())["formalize"]["items"]
    slugs = [it["slug"] for it in items if it.get("slug")]
    with ThreadPoolExecutor(max_workers=8) as ex:
        states = list(ex.map(_live_state, slugs))
    # Preserve worklist (centrality) order.
    verified = [s for s in states if s.get("ok") and s.get("extracted")]
    if limit:
        verified = verified[:limit]
    return states, verified


def main() -> None:
    limit = None
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a == "--limit" and i + 1 < len(argv):
            limit = int(argv[i + 1])

    # Clear any stale list FIRST. If verify() fails below (missing/corrupt
    # worklist), leaving no file makes the nightly skip the formalize review
    # rather than re-run it on a prior run's slugs and burn tokens on
    # already-done work (the /api/work ladder can't self-correct that).
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.unlink(missing_ok=True)

    states, verified = verify(limit)   # raises on bad input -> no file, exit != 0
    total_genuine = sum(1 for s in states if s.get("ok") and s.get("extracted"))
    stale = [s for s in states if s.get("ok") and not s.get("extracted")]
    errs = [s for s in states if not s.get("ok")]

    tmp = OUT.with_name(OUT.name + ".tmp")   # atomic: only a fully-written list appears
    tmp.write_text("".join(s["slug"] + "\n" for s in verified))
    tmp.replace(OUT)

    cap = f", cap {limit}" if limit and total_genuine > len(verified) else ""
    print(f"formalize backlog verified against live D1 -> {OUT.relative_to(ROOT)}")
    print(f"  worklist candidates : {len(states)}")
    print(f"  genuinely extracted : {total_genuine}   ({len(verified)} emitted{cap})")
    print(f"  already done in D1  : {len(stale)}   (stale on disk — skipped)")
    if errs:
        print(f"  unreachable         : {len(errs)}")
    for s in verified[:10]:
        print(f"    → {s['slug']}  ({s['n']} statements, v{s['version']})")


if __name__ == "__main__":
    main()
