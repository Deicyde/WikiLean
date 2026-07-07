#!/usr/bin/env python3
"""
wikipedia.py — lightweight Wikipedia lookup for a WikiLean annotation reviewer.

Quick context for checking an annotation against its English-Wikipedia source
article. This is the COMPLEMENT to site/render.py's full annotated render:
render.py reproduces the whole anchored page; this tool answers small,
targeted questions during review:

  search   "<q>"                   find the canonical article + alternatives
  summary  <Title>                 one-paragraph intro + Wikidata QID + revid
  section  <Title> "<section>"     plaintext of one section (verify an anchor)
  revid    <Title>                 current revision id (drift vs pinned revid)

All calls go to the English Wikipedia action/REST APIs over HTTPS, send the
WikiLean reviewer User-Agent (an empty UA returns HTTP 403), add maxlag=5 on
action-API calls, and retry once on 429/5xx with backoff. STDLIB ONLY
(urllib.request + json) — runnable with plain `python3`, no venv.

Add --json to any subcommand for machine-readable output.
"""
from __future__ import annotations

import argparse
import gzip
import html
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://en.wikipedia.org/w/api.php"
REST_SUMMARY = "https://en.wikipedia.org/api/rest_v1/page/summary/"
# Per the COMMON note: a descriptive UA is mandatory (empty UA -> HTTP 403).
UA = (
    "WikiLean-reviewer/1.0 "
    "(https://github.com/Deicyde/WikiLean; wikilean@jackmccarthy.org)"
)
TIMEOUT = 30  # short, polite timeout

# Reused-from-render.py plaintext helpers (re-implemented stdlib-only so the
# skill is self-contained; behaviour matches site/render.py's recipe of
# stripping <math> elements, then tags, then html.unescape, then collapse ws).
_MATH_ELEMENT_RE = re.compile(r"<math\b[^>]*>[\s\S]*?</math>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_SEARCHMATCH_RE = re.compile(r"</?span[^>]*>", re.IGNORECASE)


# ---------------------------------------------------------------------------
# HTTP layer: mirrors catalog/fetch_catalog.py api_get (maxlag, retry-on-429,
# retry-on-5xx with backoff) but uses urllib so no third-party deps.
# ---------------------------------------------------------------------------

class ApiError(Exception):
    """Clean, user-facing failure (printed without a traceback)."""


def _make_ssl_context() -> ssl.SSLContext:
    """TLS verification that survives the common python.org-macOS case where
    Python's expected CA file doesn't exist. Honour SSL_CERT_FILE first, then
    the default store, then well-known system bundles, then certifi if it
    happens to be importable. Verification stays ON throughout."""
    # 1) Default context (uses SSL_CERT_FILE / SSL_CERT_DIR if set, else store).
    try:
        ctx = ssl.create_default_context()
        if ctx.cert_store_stats().get("x509") or os.environ.get("SSL_CERT_FILE"):
            return ctx
    except Exception:
        ctx = None
    # 2) Known system / distro bundle locations.
    for path in (
        "/etc/ssl/cert.pem",                       # macOS, BSD
        "/etc/pki/tls/certs/ca-bundle.crt",        # RHEL/Fedora
        "/etc/ssl/certs/ca-certificates.crt",      # Debian/Ubuntu
    ):
        if os.path.exists(path):
            try:
                return ssl.create_default_context(cafile=path)
            except Exception:
                continue
    # 3) certifi, only if already installed (kept optional; not a dependency).
    try:
        import certifi  # type: ignore
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass
    # 4) Last resort: the default context as-is (may still verify on some boxes).
    return ctx or ssl.create_default_context()


_SSL_CTX = _make_ssl_context()


def _http_get(url: str, *, accept_json: bool = True, max_retries: int = 4) -> bytes:
    delay = 1.0
    last_err = None
    for attempt in range(max_retries):
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": UA,
                "Accept-Encoding": "gzip",
                "Accept": "application/json" if accept_json else "*/*",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT, context=_SSL_CTX) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return raw
        except urllib.error.HTTPError as e:
            code = e.code
            # 404 is a definitive answer (missing page) — surface immediately.
            if code == 404:
                raise ApiError(f"HTTP 404 (page not found): {url}") from e
            if code == 429:
                wait = _retry_after(e) or 10
                _note(f"[429 rate-limited; sleep {min(wait, 60)}s]")
                time.sleep(min(wait, 60))
                last_err = e
                continue
            if code >= 500:
                _note(f"[HTTP {code}; backoff {delay:.0f}s]")
                time.sleep(delay)
                delay = min(delay * 2, 30)
                last_err = e
                continue
            raise ApiError(f"HTTP {code} for {url}: {e.reason}") from e
        except (urllib.error.URLError, TimeoutError) as e:
            reason = getattr(e, "reason", e)
            _note(f"[network err: {reason}; retry in {delay:.0f}s]")
            time.sleep(delay)
            delay = min(delay * 2, 30)
            last_err = e
            continue
    raise ApiError(f"request failed after {max_retries} attempts: {url} ({last_err})")


def _retry_after(e: urllib.error.HTTPError) -> int | None:
    v = e.headers.get("Retry-After") if e.headers else None
    if v and v.isdigit():
        return int(v)
    return None


def api_get(params: dict) -> dict:
    """action-API GET with format=json + maxlag=5 + one maxlag retry."""
    for attempt in range(4):
        q = {**params, "format": "json", "maxlag": "5"}
        url = API + "?" + urllib.parse.urlencode(q)
        raw = _http_get(url)
        try:
            data = json.loads(raw)
        except ValueError as e:
            raise ApiError(f"non-JSON response from {url}") from e
        err = data.get("error") or {}
        if err.get("code") == "maxlag":
            _note("[maxlag exceeded; sleep 5s]")
            time.sleep(5)
            continue
        if err:
            raise ApiError(f"API error {err.get('code')}: {err.get('info')}")
        return data
    raise ApiError("API call kept hitting maxlag; giving up")


def rest_get(title: str) -> dict:
    """REST summary GET (no maxlag param; CDN-cached). 404 -> ApiError."""
    enc = urllib.parse.quote(title.replace(" ", "_"), safe="")
    raw = _http_get(REST_SUMMARY + enc)
    try:
        return json.loads(raw)
    except ValueError as e:
        raise ApiError("non-JSON response from REST summary endpoint") from e


def _note(msg: str) -> None:
    print(f"  {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Plaintext helpers (render.py recipe)
# ---------------------------------------------------------------------------

def strip_snippet(s: str) -> str:
    """CirrusSearch snippet -> plain text (drop <span class=searchmatch>)."""
    s = _SEARCHMATCH_RE.sub("", s)
    s = _TAG_RE.sub("", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def html_to_text(s: str) -> str:
    """Rendered section HTML -> plain prose, render.py style: kill <math>
    elements first (MathML is noise for snippet matching), then tags, then
    unescape entities and collapse whitespace. Also drops the '[ edit ]'
    heading artifact action=parse leaves in section HTML."""
    s = _MATH_ELEMENT_RE.sub(" ", s)
    s = _TAG_RE.sub(" ", s)
    s = html.unescape(s)
    s = re.sub(r"\[\s*edit\s*\]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().lower()


# ---------------------------------------------------------------------------
# Subcommand: search
# ---------------------------------------------------------------------------

def cmd_search(args) -> int:
    data = api_get(
        {
            "action": "query",
            "list": "search",
            "srsearch": args.query,
            "srlimit": str(args.limit),
            "srnamespace": "0",
            "srprop": "snippet|size|wordcount|timestamp",
        }
    )
    q = data.get("query", {})
    results = q.get("search", [])
    info = q.get("searchinfo", {})
    suggestion = info.get("suggestion")

    # Prefer an exact title match as "canonical" (disambiguation pages can
    # otherwise out-rank the article the reviewer actually wants).
    nq = _norm(args.query)
    canonical = None
    for r in results:
        if _norm(r.get("title", "")) == nq:
            canonical = r
            break
    if canonical is None and results:
        canonical = results[0]

    rows = [
        {
            "title": r.get("title"),
            "pageid": r.get("pageid"),
            "wordcount": r.get("wordcount"),
            "snippet": strip_snippet(r.get("snippet", "")),
        }
        for r in results
    ]

    if args.json:
        print(json.dumps(
            {
                "query": args.query,
                "totalhits": info.get("totalhits"),
                "suggestion": suggestion,
                "canonical": canonical.get("title") if canonical else None,
                "exact_title_match": bool(
                    canonical and _norm(canonical.get("title", "")) == nq
                ),
                "results": rows,
            },
            indent=2,
            ensure_ascii=False,
        ))
        return 0

    if not rows:
        print(f"No results for: {args.query}")
        if suggestion:
            print(f'Did you mean: "{suggestion}"?')
        return 0
    print(f'Search: "{args.query}"  (totalhits={info.get("totalhits")})')
    if suggestion:
        print(f'  did-you-mean: "{suggestion}"')
    if canonical:
        tag = "exact-title" if _norm(canonical.get("title", "")) == nq else "top-hit"
        print(f"  canonical [{tag}]: {canonical.get('title')} "
              f"(pageid {canonical.get('pageid')})")
    print()
    for i, r in enumerate(rows, 1):
        print(f"{i}. {r['title']}  (pageid {r['pageid']}, {r['wordcount']} words)")
        if r["snippet"]:
            print(f"     {r['snippet']}")
    return 0


# ---------------------------------------------------------------------------
# Subcommand: summary
# ---------------------------------------------------------------------------

def cmd_summary(args) -> int:
    try:
        data = rest_get(args.title)
    except ApiError as e:
        if "404" in str(e):
            raise ApiError(f'No article titled "{args.title}" (REST summary 404)') from e
        raise

    out = {
        "title": data.get("title"),
        "description": data.get("description"),
        "wikibase_item": data.get("wikibase_item"),  # Wikidata QID — R5 bridge
        "pageid": data.get("pageid"),
        "revision": data.get("revision"),
        "timestamp": data.get("timestamp"),
        "url": (data.get("content_urls", {})
                .get("desktop", {}).get("page")),
        "extract": data.get("extract", "").strip(),
    }
    if data.get("type") == "disambiguation":
        out["disambiguation"] = True

    if args.json:
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0

    print(out["title"])
    if out.get("disambiguation"):
        print("  [disambiguation page — not a single article]")
    if out["description"]:
        print(f"  {out['description']}")
    print()
    print(out["extract"])
    print()
    print(f"  QID:      {out['wikibase_item']}")
    print(f"  revision: {out['revision']}   ({out['timestamp']})")
    print(f"  url:      {out['url']}")
    return 0


# ---------------------------------------------------------------------------
# Subcommand: section
# ---------------------------------------------------------------------------

def _resolve_section_index(title: str, name: str, oldid: str | None):
    """Map a section display-name (WikiLean anchors use the 'line' value) to
    the numeric index action=parse needs. Returns (index, matched_line,
    all_sections). The lead is section 0 and is NOT in the TOC list."""
    params = {
        "action": "parse",
        "prop": "sections",
        "formatversion": "2",
        "redirects": "1",
    }
    if oldid:
        params["oldid"] = oldid
    else:
        params["page"] = title
    data = api_get(params)
    sections = data.get("parse", {}).get("sections", [])
    target = _norm(name)
    if target in ("", "0", "lead", "intro", "introduction"):
        return "0", "(lead)", sections
    # exact line match, then anchor match, then case-insensitive contains.
    for s in sections:
        if _norm(s.get("line", "")) == target:
            return s.get("index"), s.get("line"), sections
    for s in sections:
        if _norm(s.get("anchor", "")) == target or s.get("anchor") == name:
            return s.get("index"), s.get("line"), sections
    for s in sections:
        if target in _norm(s.get("line", "")):
            return s.get("index"), s.get("line"), sections
    return None, None, sections


def cmd_section(args) -> int:
    idx, matched_line, sections = _resolve_section_index(
        args.title, args.section, args.oldid
    )
    if idx is None:
        avail = ", ".join(s.get("line", "?") for s in sections) or "(none)"
        raise ApiError(
            f'Section "{args.section}" not found in "{args.title}". '
            f"Available sections: {avail}"
        )

    params = {
        "action": "parse",
        "prop": "text",
        "section": idx,
        "formatversion": "2",
        "redirects": "1",
        "disablelimitreport": "1",
    }
    if args.oldid:
        params["oldid"] = args.oldid
    else:
        params["page"] = args.title
    data = api_get(params)
    parse = data.get("parse", {})
    raw_html = parse.get("text", "") if isinstance(parse.get("text"), str) else ""
    text = html_to_text(raw_html)

    contains = None
    if args.contains:
        contains = _norm(args.contains) in _norm(text)

    out = {
        "title": parse.get("title") or args.title,
        "pageid": parse.get("pageid"),
        "section_index": idx,
        "section_line": matched_line,
        "oldid": args.oldid,
        "text": text,
    }
    if contains is not None:
        out["contains"] = contains
        out["needle"] = args.contains

    if args.json:
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0 if contains is not False else 2

    pin = f" @oldid {args.oldid}" if args.oldid else " (live)"
    print(f"{out['title']} — section [{idx}] {matched_line}{pin}")
    print()
    print(text if text else "(empty section)")
    if contains is not None:
        print()
        verdict = "FOUND" if contains else "NOT FOUND"
        print(f'  snippet "{args.contains}": {verdict}')
        return 0 if contains else 2
    return 0


# ---------------------------------------------------------------------------
# Subcommand: revid
# ---------------------------------------------------------------------------

def cmd_revid(args) -> int:
    data = api_get(
        {
            "action": "query",
            "prop": "info|revisions",
            "rvprop": "ids|timestamp|user|comment",
            "rvlimit": "1",
            "titles": args.title,
            "formatversion": "2",
            "redirects": "1",
        }
    )
    pages = data.get("query", {}).get("pages", [])
    if not pages:
        raise ApiError(f'No page data returned for "{args.title}"')
    page = pages[0]
    if page.get("missing"):
        raise ApiError(f'No article titled "{args.title}" (missing)')
    revs = page.get("revisions", [{}])
    rev = revs[0] if revs else {}

    out = {
        "title": page.get("title"),
        "pageid": page.get("pageid"),
        "lastrevid": page.get("lastrevid"),
        "length": page.get("length"),
        "touched": page.get("touched"),
        "last_edit": {
            "revid": rev.get("revid"),
            "user": rev.get("user"),
            "timestamp": rev.get("timestamp"),
            "comment": rev.get("comment"),
        },
    }

    drift = None
    if args.pinned is not None:
        drift = int(args.pinned) != int(page.get("lastrevid"))
        out["pinned"] = int(args.pinned)
        out["drift"] = drift

    if args.json:
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0 if drift is not True else 3

    print(f"{out['title']}  (pageid {out['pageid']})")
    print(f"  lastrevid: {out['lastrevid']}   touched {out['touched']}")
    le = out["last_edit"]
    print(f"  last edit: {le['revid']} by {le['user']} @ {le['timestamp']}")
    if le["comment"]:
        print(f"             {le['comment']}")
    if drift is not None:
        print()
        if drift:
            print(f"  DRIFT: pinned {args.pinned} != live {out['lastrevid']} "
                  f"— annotation may be stale.")
            return 3
        print(f"  OK: pinned {args.pinned} == live {out['lastrevid']} (no drift).")
    return 0


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="wikipedia.py",
        description="Lightweight Wikipedia lookup for WikiLean annotation review.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    ps = sub.add_parser("search", help="full-text search for the canonical article")
    ps.add_argument("query")
    ps.add_argument("--limit", type=int, default=5, help="1-50 (default 5)")
    ps.add_argument("--json", action="store_true")
    ps.set_defaults(func=cmd_search)

    pm = sub.add_parser("summary", help="one-paragraph intro + Wikidata QID + revid")
    pm.add_argument("title")
    pm.add_argument("--json", action="store_true")
    pm.set_defaults(func=cmd_summary)

    pe = sub.add_parser("section", help="plaintext of one section (verify an anchor)")
    pe.add_argument("title")
    pe.add_argument("section", help='section display name, e.g. "Definition" (or 0/lead)')
    pe.add_argument("--oldid", help="pin to a revision id (use the annotation's revid)")
    pe.add_argument("--contains", help="snippet to check for; sets exit code 2 if absent")
    pe.add_argument("--json", action="store_true")
    pe.set_defaults(func=cmd_section)

    pr = sub.add_parser("revid", help="current revision id (drift vs pinned)")
    pr.add_argument("title")
    pr.add_argument("--pinned", help="pinned revid; exit code 3 on drift")
    pr.add_argument("--json", action="store_true")
    pr.set_defaults(func=cmd_revid)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ApiError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
