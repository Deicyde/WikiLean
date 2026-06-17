#!/usr/bin/env python3
"""Stage-0 deterministic Wikipedia-update re-pin for WikiLean.

For each article whose pinned Wikipedia revision trails upstream, fetch the
article HTML at `latest_revid` (render.py's revid-aware fetch), run the
EXISTING anchor matcher (render.wrap_annotations) against the new HTML, and:

  - every non-tombstone annotation still anchors  →  POST the annotations
    back UNCHANGED (echoed verbatim, ids included — any mutation of a stored
    human annotation would 422) with revid=latest_revid. The Worker re-pins
    atomically (one UPDATE advances version + revid together) and resets
    wp_drifted.
  - any anchor fails  →  NO write. The slug + failing anchors are appended to
    site/cache/.wp_update_report.jsonl — these are the candidates for future
    stage-1/2 re-anchoring (fuzzy / AI semantic), which stay unbuilt until
    anchor-rot telemetry justifies them.

Fully deterministic — no AI is ever invoked here.

Usage:
    python update_from_upstream.py                       # work queue (mode=wp-update)
    python update_from_upstream.py --limit 20
    python update_from_upstream.py --slug Group_(mathematics)
    python update_from_upstream.py --slug X --dry-run    # everything except the POST
    # Testing before the drift cron has flagged anything: treat revid N as if
    # it were latest_revid. Requires --slug AND --dry-run (a live write could
    # otherwise re-pin an article to an arbitrary — even older — revision).
    python update_from_upstream.py --slug X --force-revid 123456789 --dry-run

Auth: bearer token from $WIKILEAN_API_TOKEN, falling back to the
PIPELINE_TOKEN line in wiki/.dev.vars. The token now rides EVERY article GET
too (F15): the Worker filters tombstones from anonymous responses, and this
script must echo them back verbatim — so even --slug + --dry-run needs the
token.

Report file (one JSON line per needs-work slug, append-only):
    {"ts", "run_id", "slug", "old_revid", "new_revid", "matched", "total",
     "dry_run", "failing": [{"index", "id", "anchor"}]}
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import requests

import render

# render.fetch_article_html uses urllib, which under the catalog/.venv
# python.org-framework build can't find system SSL roots (CERTIFICATE_VERIFY_
# FAILED). requests ships certifi; route urllib through the same CA bundle.
try:
    import ssl

    import certifi

    urllib.request.install_opener(
        urllib.request.build_opener(
            urllib.request.HTTPSHandler(
                context=ssl.create_default_context(cafile=certifi.where()))))
except ImportError:  # certifi absent — fall back to system certs
    pass

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
REPORT_PATH = HERE / "cache" / ".wp_update_report.jsonl"

DEFAULT_API_BASE = "https://wikilean.jackmccarthy.org"
UA = render.UA

# EDIT_LIMITER allows 30 writes/min per user (bot included) — pace under it.
WRITE_PACE_SECONDS = 2.1
# Light pace between non-writing articles too: a fast burst of GET/POSTs trips
# Cloudflare's per-IP rate limit and 503-storms the whole sweep (the review
# stage is immune only because each agent review is naturally slow).
GET_PACE_SECONDS = 0.5
# Backoff schedule for transient 5xx / network errors on a single request.
RETRY_BACKOFFS = [2, 5, 12]


# ---------------------------------------------------------------------------
# Auth + HTTP helpers
# ---------------------------------------------------------------------------

def load_token() -> str:
    """Bearer token: $WIKILEAN_API_TOKEN, else PIPELINE_TOKEN in wiki/.dev.vars."""
    tok = os.environ.get("WIKILEAN_API_TOKEN")
    if tok and tok.strip():
        return tok.strip()
    dev_vars = ROOT / "wiki" / ".dev.vars"
    if dev_vars.exists():
        for line in dev_vars.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("PIPELINE_TOKEN="):
                val = line.split("=", 1)[1].strip().strip('"').strip("'")
                if val:
                    return val
    sys.exit("error: no token — set WIKILEAN_API_TOKEN or put PIPELINE_TOKEN in wiki/.dev.vars")


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    return s


def get_work(s: requests.Session, base: str, token: str, limit: int) -> list[dict]:
    r = s.get(
        f"{base}/api/work",
        params={"mode": "wp-update", "limit": str(limit)},
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json().get("jobs", [])


def get_article(s: requests.Session, base: str, slug: str,
                token: str | None = None) -> dict | None:
    # F15: the bearer header rides every runner GET — the Worker filters
    # tombstones (status='rejected') from anonymous responses, and a stage-0
    # echo that misses a tombstone would 422 (or worse, silently drop a veto).
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    url = f"{base}/api/article/{urllib.parse.quote(slug, safe='')}.json"
    backoffs = list(RETRY_BACKOFFS)
    while True:
        try:
            r = s.get(url, headers=headers, timeout=30)
        except requests.exceptions.RequestException as e:
            if backoffs:
                w = backoffs.pop(0)
                print(f"  [GET network error ({type(e).__name__}); retry {w}s]", flush=True)
                time.sleep(w); continue
            raise
        if r.status_code == 404:
            return None
        # Transient edge/server errors (incl. Cloudflare 503 rate-limit pages)
        # are retried with backoff rather than raised as a fetch-error.
        if (r.status_code == 429 or 500 <= r.status_code < 600) and backoffs:
            w = _retry_after(r) or backoffs.pop(0)
            print(f"  [GET {r.status_code}; retry in {w}s]", flush=True)
            time.sleep(min(w, 60)); continue
        r.raise_for_status()
        return r.json()


def post_repin(s: requests.Session, base: str, token: str, slug: str,
               payload: dict) -> tuple[int, dict]:
    """POST with retry on 429 (Retry-After), 5xx, and network errors (backoff).
    Returns (status, body). A bare Cloudflare 503 rate-limit page no longer
    becomes a terminal http-503 — it's retried like any transient 5xx."""
    url = f"{base}/api/article/{urllib.parse.quote(slug, safe='')}"
    headers = {"Authorization": f"Bearer {token}"}
    n429 = 0
    backoffs = list(RETRY_BACKOFFS)
    while True:
        try:
            r = s.post(url, json=payload, headers=headers, timeout=60)
        except requests.exceptions.RequestException as e:
            if backoffs:
                w = backoffs.pop(0)
                print(f"  [POST network error ({type(e).__name__}); retry {w}s]", flush=True)
                time.sleep(w); continue
            return 0, {"error": f"network: {e}"}
        if r.status_code == 429 and n429 < 3:
            n429 += 1
            wait = _retry_after(r) or 30
            print(f"  [429 rate-limited; sleep {wait}s]", flush=True)
            time.sleep(min(wait, 120)); continue
        if 500 <= r.status_code < 600 and backoffs:
            wait = _retry_after(r) or backoffs.pop(0)
            print(f"  [{r.status_code} from edge; retry in {wait}s]", flush=True)
            time.sleep(min(wait, 60)); continue
        try:
            body = r.json()
        except ValueError:
            body = {"error": r.text[:200]}
        return r.status_code, body


def _retry_after(r: requests.Response) -> int | None:
    v = r.headers.get("Retry-After")
    if not v:
        return None
    try:
        return int(v)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Per-slug stage-0 decision
# ---------------------------------------------------------------------------

def _anchor_summary(a: dict) -> str:
    """Human-readable one-liner for a failing annotation's anchor (mirrors
    render.main's unmatched-anchor warning formatting)."""
    anchor = a.get("anchor") or (a.get("anchors") or [{}])[0] or {}
    if "snippet" in anchor:
        label = f"section={anchor.get('section')!r} snippet={anchor.get('snippet')!r}"
    elif anchor.get("type") == "prose_range" or "from" in anchor or "from_snippet" in anchor:
        frm = anchor.get("from") or anchor.get("from_snippet")
        label = f"section={anchor.get('section')!r} from={frm!r}"
    else:
        label = str(anchor.get("value", anchor))
    return label[:200]


def process_slug(s: requests.Session, base: str, slug: str, args,
                 run_id: str, token_getter) -> dict:
    """Run stage-0 for one slug. Returns a summary record for the final table."""
    rec: dict = {"slug": slug, "outcome": "?", "matched": None, "total": None,
                 "old_revid": None, "new_revid": None}

    art = get_article(s, base, slug, token_getter())
    if art is None:
        rec["outcome"] = "unknown-slug"
        return rec

    old_revid = art.get("revid")
    latest = art.get("latest_revid")
    target = args.force_revid if args.force_revid is not None else latest
    rec["old_revid"] = old_revid
    rec["new_revid"] = target

    if target is None:
        # Drift cron hasn't recorded latest_revid yet (or it's genuinely
        # unknown) — nothing to re-pin against.
        rec["outcome"] = "no-latest-revid"
        return rec
    if args.force_revid is None and old_revid is not None and target == old_revid:
        rec["outcome"] = "up-to-date"
        return rec

    try:
        html = render.fetch_article_html(slug, art["wikipedia_title"],
                                         target_revid=target)
    except Exception as e:  # network / no-parse-block — record, never write
        rec["outcome"] = f"fetch-error ({e})"
        return rec

    annotations = art["annotations"]
    src = render.absolutize_wikipedia_urls(html)
    _, matched_flags = render.wrap_annotations(src, annotations)

    # Tombstones (status='rejected') are human vetoes: the engines report them
    # matched=True ("excluded, not anchor rot"), so they never count as
    # failures and are excluded from the matched/total telemetry.
    non_tomb = [(i, a) for i, a in enumerate(annotations)
                if a.get("status") != "rejected"]
    total = len(non_tomb)
    matched = sum(1 for i, _ in non_tomb if matched_flags[i])
    failing = [i for i, _ in non_tomb if not matched_flags[i]]
    rec["matched"] = matched
    rec["total"] = total

    if failing:
        # NO write — log the stage-1/2 candidate.
        report = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "run_id": run_id,
            "slug": slug,
            "old_revid": old_revid,
            "new_revid": target,
            "matched": matched,
            "total": total,
            "dry_run": bool(args.dry_run),
            "failing": [
                {"index": i,
                 "id": annotations[i].get("id"),
                 "anchor": _anchor_summary(annotations[i])}
                for i in failing
            ],
        }
        REPORT_PATH.parent.mkdir(exist_ok=True)
        with REPORT_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(report, ensure_ascii=False) + "\n")
        rec["outcome"] = "needs-work"
        return rec

    if args.dry_run:
        rec["outcome"] = "would-repin"
        return rec

    # All non-tombstone anchors survive at the new revision → atomic re-pin.
    # Annotations are echoed VERBATIM (ids included): the Worker's 422
    # human-preservation check deep-equals stored human annotations.
    payload = {
        "annotations": annotations,
        "base_version": art["version"],
        "revid": target,
        "comment": f"wp-update:stage0:{run_id}",
        "meta": {
            "run_id": run_id,
            "mode": "wp-update",
            "stage": 0,
            "old_revid": old_revid,
            "new_revid": target,
            "matched": matched,
            "total": total,
        },
    }
    status, body = post_repin(s, base, token_getter(), slug, payload)
    if status == 200:
        rec["outcome"] = "repinned"
    elif status == 409:
        # Someone saved between our GET and POST — rebase by re-running.
        rec["outcome"] = "stale (409) — re-run to rebase"
    elif status == 422:
        # Should be impossible for a verbatim echo; signals a server/client
        # contract mismatch worth investigating.
        rec["outcome"] = f"422 human-preservation: {body.get('missing')!r}"
    else:
        rec["outcome"] = f"http-{status}: {body.get('error')!r}"
    return rec


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Stage-0 deterministic Wikipedia-update re-pin (no AI).")
    ap.add_argument("--slug", action="append", default=None,
                    help="process this slug instead of the work queue (repeatable)")
    ap.add_argument("--limit", type=int, default=10,
                    help="work-queue size when --slug is not given (default 10)")
    ap.add_argument("--dry-run", action="store_true",
                    help="do everything except the POST")
    ap.add_argument("--force-revid", type=int, default=None,
                    help="TESTING ONLY: treat this revid as latest_revid; "
                         "requires --slug and --dry-run")
    ap.add_argument("--api-base",
                    default=os.environ.get("WIKILEAN_API_BASE", DEFAULT_API_BASE),
                    help=f"API origin (default {DEFAULT_API_BASE})")
    args = ap.parse_args()

    if args.force_revid is not None and not args.slug:
        ap.error("--force-revid requires --slug")
    if args.force_revid is not None and not args.dry_run:
        ap.error("--force-revid requires --dry-run (it bypasses the drift "
                 "cron's latest_revid and could re-pin to an older revision)")

    base = args.api_base.rstrip("/")
    s = make_session()
    run_id = secrets.token_hex(4)
    print(f"run_id={run_id}  mode=wp-update stage=0  api={base}"
          f"{'  [DRY RUN]' if args.dry_run else ''}")

    # Token is loaded lazily, but every path needs it now: article GETs are
    # bearer-authenticated (F15) so the runner keeps seeing tombstones.
    _token: list[str] = []

    def token_getter() -> str:
        if not _token:
            _token.append(load_token())
        return _token[0]

    if args.slug:
        slugs = args.slug
    else:
        jobs = get_work(s, base, token_getter(), args.limit)
        slugs = [j["slug"] for j in jobs]
        print(f"work queue (mode=wp-update, limit={args.limit}): {len(slugs)} job(s)")
        if not slugs:
            print("nothing drifted — done.")
            return 0

    results: list[dict] = []
    for i, slug in enumerate(slugs):
        print(f"[{i + 1}/{len(slugs)}] {slug}", flush=True)
        rec = process_slug(s, base, slug, args, run_id, token_getter)
        results.append(rec)
        if i + 1 < len(slugs):
            # Pace EVERY article (not just after a write): a full write-pace
            # after a re-pin to stay under EDIT_LIMITER, a light pace otherwise
            # so even GET-only articles don't burst Cloudflare's per-IP limit.
            time.sleep(WRITE_PACE_SECONDS if rec["outcome"] == "repinned"
                       else GET_PACE_SECONDS)

    # Summary table.
    print(f"\n{'slug':40} {'revid old→new':>24} {'anchors':>9}  outcome")
    for r in results:
        rev = f"{r['old_revid']}→{r['new_revid']}"
        anch = (f"{r['matched']}/{r['total']}"
                if r["matched"] is not None else "-")
        print(f"{r['slug'][:40]:40} {rev:>24} {anch:>9}  {r['outcome']}")
    n_needs = sum(1 for r in results if r["outcome"] == "needs-work")
    if n_needs:
        print(f"\n{n_needs} slug(s) need stage-1/2 — recorded in {REPORT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
