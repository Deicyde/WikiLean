#!/usr/bin/env python3
"""mathlib-search — find/verify Mathlib4 declarations three ways.

  loogle    "<pattern>"            SYNTACTIC: name / type-pattern / conclusion search (zero hallucination)
  semantic  "<nl query>"          SEMANTIC: natural-language -> decl (LeanSearch keyless default)
  decl      <Decl.Name>           EXACT existence + module/kind via declaration-data (cached)

Stdlib only (urllib + json). No external deps, no venv. Polite: short timeout, one retry
on 429/5xx with backoff. Default human-readable output; --json for machine output.

See SKILL.md for WHEN to reach for each subcommand and the Loogle DSL cheatsheet.
"""
import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UA = ("WikiLean-mathlib-search/1.0 "
      "(https://github.com/Deicyde/WikiLean; wikilean@jackmccarthy.org)")

LOOGLE_URL = "https://loogle.lean-lang.org/json"
LEANSEARCH_URL = os.environ.get(
    "LEANSEARCHCLIENT_LEANSEARCH_API_URL", "https://leansearch.net/search")
LEAN_FINDER_URL = os.environ.get(
    "LEAN_FINDER_URL",
    "https://bxrituxuhpc70w8w.us-east-1.aws.endpoints.huggingface.cloud")
LEANEXPLORE_URL = "https://www.leanexplore.com/api/v2/search"
NUMINA_URL = "https://leandex.projectnumina.ai/api/v1/search"

DECLDATA_URL = ("https://leanprover-community.github.io/mathlib4_docs/"
                "declarations/declaration-data.bmp")
DOCS_BASE = "https://leanprover-community.github.io/mathlib4_docs/"

TIMEOUT = 30  # seconds; Loogle elaboration + 65MB decl-data fetch both fit
SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
# Prefer a writable cache in the skill dir; fall back to /tmp.
CACHE_DIR = os.environ.get("MATHLIB_SEARCH_CACHE",
                           os.path.join(SKILL_DIR, ".cache"))
# WikiLean's pre-built sharded index (optional reuse; skill works without it).
WIKILEAN_SHARDS = os.path.normpath(
    os.path.join(SKILL_DIR, "..", "..", "..", "wiki", "public", "assets",
                 "decl-index"))


class ApiError(Exception):
    """Clean, user-facing failure (printed to stderr, nonzero exit)."""


def _ssl_context():
    """Build a verifying SSL context that works even on python.org macOS builds.

    Some Python installs (notably python.org on macOS, where the user never ran
    "Install Certificates.command") ship no CA bundle wired into urllib, so the
    default context raises CERTIFICATE_VERIFY_FAILED. We try, in order: certifi
    (if importable), then well-known system CA bundle paths. Stdlib-only — we
    NEVER disable verification. Override with the SSL_CERT_FILE env var.
    """
    # Honor an explicit override first.
    env_ca = os.environ.get("SSL_CERT_FILE")
    if env_ca and os.path.isfile(env_ca):
        return ssl.create_default_context(cafile=env_ca)
    # If the default context already has a usable store, use it.
    paths = ssl.get_default_verify_paths()
    if (paths.cafile and os.path.isfile(paths.cafile)) or \
       (paths.capath and os.path.isdir(paths.capath)):
        return ssl.create_default_context()
    try:  # certifi is not stdlib, but use it transparently if present.
        import certifi  # noqa: PLC0415
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass
    for cand in ("/etc/ssl/cert.pem",                      # macOS system bundle
                 "/opt/homebrew/etc/ca-certificates/cert.pem",
                 "/usr/local/etc/ca-certificates/cert.pem",
                 "/etc/pki/tls/certs/ca-bundle.crt",       # RHEL/Fedora
                 "/etc/ssl/certs/ca-certificates.crt"):    # Debian/Ubuntu
        if os.path.isfile(cand):
            return ssl.create_default_context(cafile=cand)
    # Last resort: a default context (may still fail, but we surface a clean
    # error rather than silently dropping verification).
    return ssl.create_default_context()


_SSL_CTX = _ssl_context()


def _request(url, *, data=None, headers=None, method=None):
    """One HTTP call with the WikiLean UA. Returns (status, body_bytes, resp)."""
    hdrs = {"User-Agent": UA}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    ctx = _SSL_CTX if url.lower().startswith("https") else None
    resp = urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx)
    return resp.getcode(), resp.read(), resp


def http(url, *, data=None, headers=None, method=None, retries=1,
         allow_statuses=()):
    """Polite fetch: short timeout, one retry on 429/5xx with backoff.

    Raises ApiError on terminal HTTP/network failure. Status codes listed in
    allow_statuses (e.g. 304 for a conditional GET) are returned normally as
    (code, body, resp) instead of raising — urllib models them as HTTPError,
    but the HTTPError object behaves like a response (.headers/.read()).
    """
    attempt = 0
    while True:
        try:
            return _request(url, data=data, headers=headers, method=method)
        except urllib.error.HTTPError as e:
            if e.code in allow_statuses:
                return e.code, b"", e
            transient = e.code == 429 or 500 <= e.code < 600
            if transient and attempt < retries:
                back = 1.5 * (attempt + 1)
                ra = e.headers.get("Retry-After") if e.headers else None
                if ra:
                    try:
                        back = max(back, float(ra))
                    except ValueError:
                        pass
                time.sleep(back)
                attempt += 1
                continue
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            raise ApiError("HTTP %s from %s%s" %
                           (e.code, url, ("\n  " + body) if body else ""))
        except urllib.error.URLError as e:
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                attempt += 1
                continue
            raise ApiError("network error contacting %s: %s" % (url, e.reason))
        except TimeoutError:
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                attempt += 1
                continue
            raise ApiError("timed out after %ss contacting %s" % (TIMEOUT, url))


# ---------------------------------------------------------------------------
# loogle — SYNTACTIC search
# ---------------------------------------------------------------------------

def loogle(query, *, lucky=False):
    """Query Loogle. Returns the parsed JSON dict (or a 'lucky' redirect dict)."""
    params = {"q": query}
    if lucky:
        params["lucky"] = "true"
    url = LOOGLE_URL + "?" + urllib.parse.urlencode(params)
    if lucky:
        # lucky=true 302-redirects to the docs anchor; capture Location w/o following.
        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        # Carry the same verifying SSL context the rest of the script uses,
        # otherwise this path crashes on installs with no wired-in CA bundle.
        opener = urllib.request.build_opener(
            _NoRedirect, urllib.request.HTTPSHandler(context=_SSL_CTX))
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            r = opener.open(req, timeout=TIMEOUT)
            # No redirect => no single best hit / direct body.
            return {"lucky": True, "status": r.getcode(), "location": None}
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                return {"lucky": True, "status": e.code,
                        "location": e.headers.get("Location")}
            raise ApiError("HTTP %s from Loogle lucky query" % e.code)
        except urllib.error.URLError as e:
            raise ApiError("network error contacting Loogle (lucky): %s"
                           % e.reason)
        except TimeoutError:
            raise ApiError("timed out after %ss contacting Loogle (lucky)"
                           % TIMEOUT)
    _, body, _ = http(url)
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        raise ApiError("Loogle returned non-JSON body")


def cmd_loogle(args):
    if args.lucky:
        res = loogle(args.query, lucky=True)
        if args.json:
            print(json.dumps(res, indent=2))
            return 0
        loc = res.get("location")
        if loc:
            print(loc)
            return 0
        print("no single best hit (lucky redirect not issued)", file=sys.stderr)
        return 1

    res = loogle(args.query)
    if args.json:
        print(json.dumps(res, indent=2))
        # Still signal failure on an error payload.
        return 1 if "error" in res else 0

    if "error" in res:
        err = res["error"]
        sys.stderr.write("Loogle error: %s\n" % err)
        sugg = res.get("suggestions")
        if sugg:
            sys.stderr.write("  suggestions: %s\n" % ", ".join(sugg))
        if "unknown identifier" in err:
            sys.stderr.write(
                "  hint: that constant name is not in Mathlib; "
                "retry as a NAME substring search, e.g. loogle '\"%s\"', "
                "or confirm with: mathlib_search.py decl <Name>\n"
                % args.query.strip())
        elif "timeout" in err:
            sys.stderr.write(
                "  hint: pattern too broad. Anchor equations under |- "
                "(e.g. '|- ?a + ?b = ?b + ?a') or add a constant constraint.\n")
        return 1

    count = res.get("count", 0)
    hits = res.get("hits", [])
    if count == 0:
        print("no matches (count=0)")
        sugg = res.get("suggestions")
        if sugg:
            print("  fallback constraints tried: %s" % ", ".join(sugg))
        return 0
    print("%d match%s (heartbeats=%s)%s" %
          (count, "" if count == 1 else "es", res.get("heartbeats", "?"),
           "  [showing first %d]" % len(hits) if count > len(hits) else ""))
    for h in hits[:args.limit]:
        name = h.get("name", "?")
        typ = (h.get("type") or "").strip()
        mod = h.get("module", "?")
        print("\n  %s" % name)
        if typ:
            print("    %s" % typ.replace("\n", "\n    "))
        print("    module: %s" % mod)
        print("    docs:   %s%s.html#%s" %
              (DOCS_BASE, mod.replace(".", "/"), name))
    return 0


# ---------------------------------------------------------------------------
# semantic — natural language search
# ---------------------------------------------------------------------------

def sem_leansearch(query, n):
    body = json.dumps({"query": [query], "num_results": n}).encode("utf-8")
    _, raw, _ = http(LEANSEARCH_URL, data=body,
                     headers={"Content-Type": "application/json"},
                     method="POST")
    data = json.loads(raw)
    # Response is doubly-nested: [[ {result, distance}, ... ]]
    inner = data[0] if data and isinstance(data, list) else []
    out = []
    for it in inner:
        r = it.get("result", {})
        name = ".".join(r.get("name", [])) if isinstance(
            r.get("name"), list) else r.get("name", "?")
        mod = ".".join(r.get("module_name", [])) if isinstance(
            r.get("module_name"), list) else r.get("module_name", "")
        out.append({
            "name": name,
            "module": mod,
            "kind": r.get("kind"),
            "type": r.get("type") or r.get("signature"),
            "informal_name": r.get("informal_name"),
            "informal_description": r.get("informal_description"),
            "score": it.get("distance"),
            "score_kind": "distance(lower=closer)",
        })
    return out


def sem_leanfinder(query, n):
    body = json.dumps({"inputs": query, "top_k": n}).encode("utf-8")
    _, raw, _ = http(LEAN_FINDER_URL, data=body,
                     headers={"Content-Type": "application/json"},
                     method="POST")
    data = json.loads(raw)
    out = []
    for r in data.get("results", []):
        url = r.get("url", "") or ""
        if "mathlib4_docs" not in url and url:
            continue  # keep Mathlib hits when a url is present
        mod = (r.get("path") or "").replace("/", ".")
        out.append({
            "name": r.get("formal_name", "?"),
            "module": mod,
            "kind": r.get("kind"),
            "type": r.get("type"),
            "informal_name": r.get("informal_name"),
            "informal_description": r.get("informal_description"),
            "score": r.get("score"),
            "score_kind": "similarity(higher=closer)",
        })
    return out


def sem_leanexplore(query, n):
    key = os.environ.get("LEANEXPLORE_API_KEY")
    if not key:
        raise ApiError(
            "leanexplore is key-gated: set LEANEXPLORE_API_KEY "
            "(get a key from leanexplore.com /api-keys). The keyless default "
            "engine is leansearch.")
    url = LEANEXPLORE_URL + "?" + urllib.parse.urlencode(
        {"query": query, "limit": n})
    _, raw, _ = http(url, headers={"Authorization": "Bearer " + key})
    data = json.loads(raw)
    out = []
    for r in data.get("results", []):
        # Shape is documented but not captured live; be defensive.
        name = r.get("name") or r.get("formal_name") or "?"
        if isinstance(name, list):
            name = ".".join(name)
        out.append({
            "name": name,
            "module": r.get("module") or r.get("path") or "",
            "kind": r.get("kind"),
            "type": r.get("signature") or r.get("statement_text")
            or r.get("type"),
            "informal_name": r.get("informal_name"),
            "informal_description": r.get("informal_description"),
            "score": r.get("score"),
            "score_kind": "score(higher=closer)",
        })
    return out


def sem_numina(query, n):
    url = NUMINA_URL + "?" + urllib.parse.urlencode(
        {"q": query, "limit": n, "generate_query": "false"})
    _, raw, _ = http(url, headers={"Accept": "text/event-stream"})
    text = raw.decode("utf-8", "replace")
    results = []
    # SSE: buffer fully, take the last stage==search event's search_results.
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        try:
            ev = json.loads(line[5:].strip())
        except json.JSONDecodeError:
            continue
        if ev.get("stage") == "search":
            sr = (ev.get("data") or {}).get("search_results")
            if sr:
                results = sr
    out = []
    for r in results[:n]:
        info = r.get("informalization")
        iname = idesc = None
        if isinstance(info, dict):
            iname = info.get("informal_name")
            idesc = info.get("informal_description")
        elif isinstance(info, str):
            idesc = info
        out.append({
            "name": r.get("name", "?"),
            "module": r.get("module", ""),
            "kind": None,
            "type": None,
            "informal_name": iname,
            "informal_description": idesc or r.get("docstring"),
            "score": None,
            "score_kind": None,
        })
    return out


SEM_ENGINES = {
    "leansearch": (sem_leansearch, False),   # keyless default
    "leanfinder": (sem_leanfinder, False),   # keyless secondary
    "leanexplore": (sem_leanexplore, True),  # key-gated
    "numina": (sem_numina, False),           # keyless tertiary (SSE)
}


def cmd_semantic(args):
    fn, _gated = SEM_ENGINES[args.engine]
    hits = fn(args.query, args.num_results)
    if args.json:
        print(json.dumps({"engine": args.engine, "query": args.query,
                          "results": hits}, indent=2, ensure_ascii=False))
        return 0
    if not hits:
        print("no results from %s" % args.engine)
        return 0
    print("%d result%s from %s:" %
          (len(hits), "" if len(hits) == 1 else "s", args.engine))
    for h in hits:
        sc = ""
        if h.get("score") is not None:
            sc = "  [%s=%.4f]" % (h.get("score_kind") or "score", h["score"])
        print("\n  %s%s" % (h["name"], sc))
        if h.get("kind"):
            print("    kind:   %s" % h["kind"])
        if h.get("type"):
            print("    type:   %s" % h["type"])
        if h.get("module"):
            print("    module: %s" % h["module"])
        if h.get("informal_name"):
            print("    gloss:  %s" % h["informal_name"])
        if h.get("informal_description"):
            desc = " ".join(h["informal_description"].split())
            if len(desc) > 220:
                desc = desc[:217] + "..."
            print("    desc:   %s" % desc)
    return 0


# ---------------------------------------------------------------------------
# decl — EXACT existence + module/kind via declaration-data
# ---------------------------------------------------------------------------

def _wikilean_shard_lookup(name):
    """Try WikiLean's pre-built prefix shards. Returns (module,) or None.

    Shards hold [name, module] pairs (no kind). Longest-prefix scheme:
    lowercase the name, pad to min_len with '_', try the longest matching
    prefix present in the manifest down to min_len.
    """
    man_path = os.path.join(WIKILEAN_SHARDS, "manifest.json")
    if not os.path.isfile(man_path):
        return None
    try:
        man = json.load(open(man_path))
    except Exception:
        return None
    scheme = man.get("scheme", {})
    shards = man.get("shards", {})
    min_len = scheme.get("min_len", 2)
    max_len = scheme.get("max_len", 24)
    pad = scheme.get("pad", "_")
    key = name.lower()
    if len(key) < min_len:
        key = key + pad * (min_len - len(key))
    # Longest present prefix wins.
    for L in range(min(max_len, len(key)), min_len - 1, -1):
        pref = key[:L]
        if pref in shards:
            sp = os.path.join(WIKILEAN_SHARDS, pref + ".json")
            if os.path.isfile(sp):
                try:
                    for entry in json.load(open(sp)):
                        if entry and entry[0] == name:
                            return {"module": entry[1], "source": "wikilean-shards"}
                except Exception:
                    return None
            break
    return None


def _cache_paths():
    os.makedirs(CACHE_DIR, exist_ok=True)
    return (os.path.join(CACHE_DIR, "declaration-data.json"),
            os.path.join(CACHE_DIR, "declaration-data.etag"))


def _load_decldata(refresh=False):
    """Fetch + cache declaration-data.bmp (JSON despite image/bmp type).

    Conditional GET on the stored ETag => 304 keeps the cache. Returns the
    parsed dict with a top-level 'declarations' map.
    """
    data_path, etag_path = _cache_paths()
    etag = None
    if os.path.isfile(etag_path) and not refresh:
        try:
            etag = open(etag_path).read().strip() or None
        except Exception:
            etag = None

    headers = {}
    if etag and os.path.isfile(data_path):
        headers["If-None-Match"] = etag
    try:
        # 304 (cache still current) is a SUCCESS, not a failure — allow it
        # through instead of letting http() raise ApiError on it.
        status, body, resp = http(DECLDATA_URL, headers=headers,
                                  allow_statuses=(304,))
        if status == 304 and os.path.isfile(data_path):
            # Conditional GET hit: our cached copy is current, reuse it.
            return json.load(open(data_path))
        new_etag = resp.headers.get("ETag")
        # 65MB body is JSON; ignore the image/bmp content-type.
        obj = json.loads(body)
        with open(data_path, "wb") as f:
            f.write(body)
        if new_etag:
            with open(etag_path, "w") as f:
                f.write(new_etag)
        return obj
    except ApiError:
        # Network failed but we may have a usable cache.
        if os.path.isfile(data_path):
            sys.stderr.write(
                "warning: decl-data fetch failed; using cached copy\n")
            return json.load(open(data_path))
        raise


def decl_lookup(name, *, prefer_shards=True, refresh=False):
    """Resolve a decl name to {exists, module, kind, docs, source}."""
    # Fast path: WikiLean shards (name+module, no kind) when present.
    if prefer_shards and not refresh:
        sh = _wikilean_shard_lookup(name)
        if sh:
            mod = sh["module"]
            return {
                "name": name, "exists": True, "module": mod, "kind": None,
                "docs": "%s%s.html#%s" % (DOCS_BASE, mod.replace(".", "/"), name),
                "source": "wikilean-shards",
            }
    # Authoritative path: live declaration-data (gives kind too).
    obj = _load_decldata(refresh=refresh)
    decls = obj.get("declarations", {})
    rec = decls.get(name)
    if not rec:
        return {"name": name, "exists": False, "module": None, "kind": None,
                "docs": None, "source": "declaration-data"}
    doc_link = rec.get("docLink", "")
    full = DOCS_BASE + doc_link.lstrip("./") if doc_link else None
    module = None
    if doc_link:
        path = doc_link.lstrip("./").split("#", 1)[0]
        if path.endswith(".html"):
            module = path[:-len(".html")].replace("/", ".")
    return {"name": name, "exists": True, "module": module,
            "kind": rec.get("kind"), "docs": full, "source": "declaration-data"}


def cmd_decl(args):
    res = decl_lookup(args.name, prefer_shards=not args.live,
                      refresh=args.refresh)
    if args.json:
        print(json.dumps(res, indent=2))
        return 0 if res["exists"] else 1
    if res["exists"]:
        print("EXISTS: %s" % res["name"])
        if res.get("kind"):
            print("  kind:   %s" % res["kind"])
        elif res["source"] == "wikilean-shards":
            print("  kind:   (n/a from shards; use --live for kind)")
        print("  module: %s" % res["module"])
        print("  docs:   %s" % res["docs"])
        print("  source: %s" % res["source"])
        return 0
    print("NOT FOUND: %s" % res["name"])
    print("  This exact decl name is not in declaration-data "
          "(hallucinated or misspelled).")
    print("  Try a shape search:  mathlib_search.py loogle '\"%s\"'"
          % args.name.split(".")[-1])
    return 1


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(
        prog="mathlib_search.py",
        description="Find/verify Mathlib4 declarations: syntactic (loogle), "
                    "semantic (NL), exact (decl).")
    p.add_argument("--json", action="store_true",
                   help="machine-readable JSON output")
    sub = p.add_subparsers(dest="cmd", required=True)

    def _add_json(sp):
        # Accept --json AFTER the subcommand too (the natural place to type it).
        # SUPPRESS default => only sets json when actually passed, so the
        # top-level --json value is preserved when this one is omitted.
        sp.add_argument("--json", action="store_true",
                        default=argparse.SUPPRESS,
                        help="machine-readable JSON output")

    pl = sub.add_parser(
        "loogle",
        help="SYNTACTIC: name / type-pattern / |- conclusion search")
    _add_json(pl)
    pl.add_argument("query", help="Loogle query (see SKILL.md DSL cheatsheet)")
    pl.add_argument("--lucky", action="store_true",
                    help="return the single best hit's docs URL (302 Location)")
    pl.add_argument("--limit", type=int, default=20,
                    help="max hits to print (default 20; Loogle caps at 200)")
    pl.set_defaults(func=cmd_loogle)

    ps = sub.add_parser(
        "semantic", help="SEMANTIC: natural-language statement -> decl")
    _add_json(ps)
    ps.add_argument("query", help="informal statement / concept in prose")
    ps.add_argument("--engine", choices=sorted(SEM_ENGINES.keys()),
                    default="leansearch",
                    help="leansearch (keyless default), leanfinder (keyless), "
                         "numina (keyless SSE), leanexplore (needs key)")
    ps.add_argument("--num-results", type=int, default=6,
                    help="results to request (default 6)")
    ps.set_defaults(func=cmd_semantic)

    pd = sub.add_parser(
        "decl", help="EXACT: does this decl name exist? module + kind")
    _add_json(pd)
    pd.add_argument("name", help="exact dotted decl name, e.g. Real.continuous_sin")
    pd.add_argument("--live", action="store_true",
                    help="skip WikiLean shards; use live declaration-data "
                         "(also returns kind)")
    pd.add_argument("--refresh", action="store_true",
                    help="force re-download of declaration-data (~65MB)")
    pd.set_defaults(func=cmd_decl)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ApiError as e:
        sys.stderr.write("error: %s\n" % e)
        return 2
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
