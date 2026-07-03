#!/usr/bin/env python3
"""
wikidata.py -- resolve a concept to a Wikidata QID and inspect its formal-library
cross-references + structured graph, for WikiLean reviewers / the @[wikidata] and
property-proposal pipelines.

Stdlib only (urllib.request + json). No third-party deps, no venv.

Subcommands:
  search    "<label>"            wbsearchentities -> ranked candidate QIDs
  entity    <QID>                labels/description + key claims (REST API, flat)
  xrefs     <QID>                JUST the formal/reference cross-ref properties a
                                 WikiLean reviewer cares about (Metamath, nLab,
                                 MathWorld, ProofWiki, defining formula) + sitelinks
  sitelinks <QID>               Wikipedia sitelinks for a QID (verify the concept
                                 maps back to the expected article)
  by_slug   "<enwiki title>"    exact enwiki article title/slug -> QID (sitelink
                                 lookup, NOT a search; the article's own QID)
  semantic  "<description>"     LOCAL embedding search over the math-QID universe
                                 (meaning-based; fixes the broad-QID failure mode)
  sparql    "<query>"           WDQS main-graph SPARQL -> JSON bindings
  reconcile "<label>"           reconciliation API (optional, 3rd-party WMCloud)

All Wikimedia calls send the agreed UA, maxlag=5 (Action API) and back off once on
429/5xx. See SKILL.md for WHEN to use which subcommand and ready SPARQL templates.
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# Agreed WikiLean reviewer User-Agent (Wikimedia requires a descriptive UA; WDQS
# returns HTTP 403 without one).
UA = (
    "WikiLean-reviewer/1.0 "
    "(https://github.com/Deicyde/WikiLean; wikilean@jackmccarthy.org)"
)

ACTION_API = "https://www.wikidata.org/w/api.php"
REST_BASE = "https://www.wikidata.org/w/rest.php/wikibase/v1"
WDQS = "https://query.wikidata.org/sparql"
RECONCILE = "https://wikidata.reconci.link/en/api"

TIMEOUT = 30  # short, polite

# The formal/reference cross-ref properties a WikiLean reviewer cares about.
# Order matters -- this is the display order.
XREF_PROPS = [
    ("P12888", "Metamath", "https://us.metamath.org/mpeuni/{}.html"),
    ("P4215", "nLab", "https://ncatlab.org/nlab/show/{}"),
    ("P2812", "MathWorld", "https://mathworld.wolfram.com/{}.html"),
    ("P6781", "ProofWiki", "https://proofwiki.org/wiki/{}"),
    ("P10283", "OpenAlex", "https://openalex.org/{}"),
]
# P2534 (defining formula) is a math literal, handled separately (no URL).
FORMULA_PROP = ("P2534", "defining formula")
# Structural claims worth surfacing in `entity`.
STRUCT_PROPS = [
    ("P31", "instance of"),
    ("P279", "subclass of"),
]


# ---------------------------------------------------------------------------
# TLS: build a verifying SSL context. Some Python installs (notably the
# python.org macOS framework build, where "Install Certificates.command" was
# never run) ship without a usable CA store, so urllib raises
# CERTIFICATE_VERIFY_FAILED on every HTTPS call. We locate a real CA bundle
# rather than disabling verification:
#   1. honor SSL_CERT_FILE / SSL_CERT_DIR (standard OpenSSL override);
#   2. use the default trust store if it actually loaded any CAs;
#   3. else fall back to well-known system bundles (macOS, Linux, Homebrew).
# Verification is ALWAYS on (CERT_REQUIRED) -- we never set check_hostname=False.
# ---------------------------------------------------------------------------

# Common CA-bundle locations, in preference order. /etc/ssl/cert.pem ships on
# every modern macOS; the others cover mainstream Linux distros and Homebrew.
_CA_BUNDLES = (
    "/etc/ssl/cert.pem",                        # macOS (LibreSSL) + some BSD
    "/etc/ssl/certs/ca-certificates.crt",       # Debian/Ubuntu
    "/etc/pki/tls/certs/ca-bundle.crt",         # RHEL/CentOS/Fedora
    "/etc/ssl/ca-bundle.pem",                   # OpenSUSE
    "/opt/homebrew/etc/ca-certificates/cert.pem",  # Homebrew (Apple silicon)
    "/usr/local/etc/ca-certificates/cert.pem",     # Homebrew (Intel)
    "/opt/homebrew/etc/openssl@3/cert.pem",
    "/usr/local/etc/openssl@3/cert.pem",
)

_SSL_CTX: ssl.SSLContext | None = None


def _ssl_context() -> ssl.SSLContext:
    """Return a verifying SSLContext, locating a CA bundle if the default lacks one."""
    global _SSL_CTX
    if _SSL_CTX is not None:
        return _SSL_CTX

    # (1) Explicit override via env -- respected by create_default_context too,
    # but we also let it pick our fallback path below if it points nowhere.
    env_file = os.environ.get("SSL_CERT_FILE")
    if env_file and os.path.isfile(env_file):
        _SSL_CTX = ssl.create_default_context(cafile=env_file)
        return _SSL_CTX

    # (2) Default context -- good on most installs. Check it actually loaded CAs.
    ctx = ssl.create_default_context()
    try:
        if ctx.cert_store_stats().get("x509_ca", 0) > 0:
            _SSL_CTX = ctx
            return _SSL_CTX
    except Exception:
        pass

    # (3) Fall back to the first system bundle that exists.
    for path in _CA_BUNDLES:
        if os.path.isfile(path):
            try:
                _SSL_CTX = ssl.create_default_context(cafile=path)
                return _SSL_CTX
            except Exception:
                continue

    # Nothing found: return the (empty) default. The first request will fail
    # with a clear CERTIFICATE_VERIFY_FAILED message, and fetch() tells the
    # user to set SSL_CERT_FILE -- we do NOT silently disable verification.
    _SSL_CTX = ctx
    return _SSL_CTX


# ---------------------------------------------------------------------------
# HTTP layer: urllib with UA, short timeout, one retry on 429/5xx w/ backoff.
# ---------------------------------------------------------------------------

def _http(url: str, *, data: bytes | None = None, accept: str = "application/json",
          method: str | None = None) -> tuple[int, bytes, dict]:
    """Single request. Returns (status, body_bytes, headers). Raises on transport."""
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("User-Agent", UA)
    req.add_header("Accept", accept)
    req.add_header("Accept-Encoding", "identity")
    if data is not None and method == "POST":
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=_ssl_context()) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers or {})


def _retry_after(headers: dict) -> int | None:
    v = headers.get("Retry-After") or headers.get("retry-after")
    if not v:
        return None
    try:
        return int(v)
    except ValueError:
        return None


def fetch(url: str, *, data: bytes | None = None, method: str | None = None,
          accept: str = "application/json", what: str = "request",
          allow_404: bool = False) -> bytes:
    """Fetch with one retry on 429/5xx (backoff). Exits cleanly on hard failure.

    allow_404=True turns a 404 into an empty-bytes return instead of an error
    exit -- used for "absent is normal" lookups (e.g. an item with no enwiki
    sitelink), so a benign miss is never reported as an error.
    """
    for attempt in range(2):  # initial + one retry
        try:
            status, body, headers = _http(url, data=data, accept=accept, method=method)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            # TLS cert-verification failures are not transient -- don't retry;
            # tell the user how to point at a CA bundle.
            if isinstance(getattr(e, "reason", None), ssl.SSLCertVerificationError) \
                    or "CERTIFICATE_VERIFY_FAILED" in str(e):
                _die(f"{what} failed: TLS certificate verification failed. "
                     "Your Python has no usable CA store. Fix with one of:\n"
                     "  - python.org macOS build: run "
                     "'/Applications/Python 3.x/Install Certificates.command'\n"
                     "  - or set SSL_CERT_FILE to a CA bundle, e.g. "
                     "SSL_CERT_FILE=/etc/ssl/cert.pem python3 wikidata.py ...")
            if attempt == 0:
                time.sleep(2)
                continue
            _die(f"{what} failed: network error: {e}")
        if status == 200:
            return body
        if status == 404 and allow_404:
            return b""
        if status in (429,) or status >= 500:
            if attempt == 0:
                wait = _retry_after(headers) or 5
                time.sleep(min(wait, 30))
                continue
            _die(f"{what} failed: HTTP {status} after retry "
                 f"(body: {body[:200].decode('utf-8', 'replace')})", code=status)
        # Other 4xx: no retry.
        _die(f"{what} failed: HTTP {status} "
             f"(body: {body[:300].decode('utf-8', 'replace')})", code=status)
    _die(f"{what} failed: exhausted retries")


def _die(msg: str, code: int = 1) -> "None":
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(2 if code == 0 else (1 if code < 2 else code if code < 256 else 1))


def _get_json(url: str, what: str, allow_404: bool = False) -> dict:
    body = fetch(url, what=what, allow_404=allow_404)
    if allow_404 and not body:
        return {}
    try:
        return json.loads(body)
    except ValueError:
        _die(f"{what}: response was not JSON (got {body[:200]!r})")


def _action_url(params: dict) -> str:
    params = {**params, "format": "json", "maxlag": "5"}
    return ACTION_API + "?" + urllib.parse.urlencode(params)


# ---------------------------------------------------------------------------
# search: wbsearchentities -> ranked candidate QIDs
# ---------------------------------------------------------------------------

def cmd_search(args) -> None:
    url = _action_url({
        "action": "wbsearchentities",
        "search": args.label,
        "language": "en",
        "uselang": "en",
        "type": args.type,
        "limit": str(args.limit),
    })
    data = _get_json(url, what="wbsearchentities")
    if "error" in data:
        _die(f"wbsearchentities API error: {data['error'].get('info', data['error'])}")
    hits = data.get("search", [])
    results = [
        {
            "qid": h.get("id"),
            "label": h.get("label", ""),
            "description": h.get("description", ""),
            "match": h.get("match", {}).get("type", ""),
            "concepturi": h.get("concepturi", ""),
        }
        for h in hits
    ]
    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
        return
    if not results:
        print(f"(no candidates for {args.label!r})")
        return
    print(f"{len(results)} candidate(s) for {args.label!r} "
          f"(DISAMBIGUATE by description/claims -- do not trust the top hit):")
    for r in results:
        desc = r["description"] or "(no description)"
        print(f"  {r['qid']:<12} {r['label']}")
        print(f"  {'':<12} {desc}  [matched: {r['match']}]")


# ---------------------------------------------------------------------------
# REST helpers
# ---------------------------------------------------------------------------

def _rest_entity(qid: str) -> dict:
    url = f"{REST_BASE}/entities/items/{urllib.parse.quote(qid)}"
    return _get_json(url, what=f"REST entity {qid}")


def _rest_statements(qid: str, prop: str | None = None) -> dict:
    url = f"{REST_BASE}/entities/items/{urllib.parse.quote(qid)}/statements"
    if prop:
        url += "?" + urllib.parse.urlencode({"property": prop})
    return _get_json(url, what=f"REST statements {qid}")


def _stmt_value(stmt: dict):
    """Read a REST statement's value, handling novalue/somevalue and item refs."""
    val = stmt.get("value", {})
    vtype = val.get("type")
    if vtype != "value":
        return None, vtype  # 'novalue' / 'somevalue' / None
    content = val.get("content")
    # wikibase-item via REST: content is a plain QID string ("Q1142699"); some
    # variants nest it as {"id": "Q.."}. external-id/string: plain string;
    # quantity/time/monolingualtext: dict.
    if isinstance(content, dict) and "id" in content:
        return content["id"], "item"
    if isinstance(content, str) and len(content) > 1 and content[0] in "QPL" \
            and content[1:].isdigit():
        return content, "item"
    return content, "value"


# ---------------------------------------------------------------------------
# entity: labels/description + key claims (flat, via REST)
# ---------------------------------------------------------------------------

def cmd_entity(args) -> None:
    ent = _rest_entity(args.qid)
    labels = ent.get("labels", {})
    descs = ent.get("descriptions", {})
    statements = ent.get("statements", {})

    struct = {}
    for pid, name in STRUCT_PROPS:
        items = []
        for st in statements.get(pid, []):
            v, kind = _stmt_value(st)
            if kind == "item" and v:
                items.append(v)
        if items:
            struct[name] = items

    out = {
        "qid": ent.get("id", args.qid),
        "label": labels.get("en", ""),
        "description": descs.get("en", ""),
        "aliases_en": [a for a in ent.get("aliases", {}).get("en", [])],
        "instance_of": struct.get("instance of", []),
        "subclass_of": struct.get("subclass of", []),
        "num_statements": sum(len(v) for v in statements.values()),
        "sitelink_enwiki": ent.get("sitelinks", {}).get("enwiki", {}).get("title", ""),
    }
    if args.json:
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return
    print(f"{out['qid']}  {out['label']}")
    if out["description"]:
        print(f"  {out['description']}")
    if out["aliases_en"]:
        print(f"  aliases: {', '.join(out['aliases_en'][:8])}")
    if out["instance_of"]:
        print(f"  instance of (P31):  {', '.join(out['instance_of'])}")
    if out["subclass_of"]:
        print(f"  subclass of (P279): {', '.join(out['subclass_of'])}")
    if out["sitelink_enwiki"]:
        print(f"  enwiki: {out['sitelink_enwiki']}")
    print(f"  ({out['num_statements']} statements total)")


# ---------------------------------------------------------------------------
# xrefs: just the formal/reference cross-ref properties + enwiki sitelink
# ---------------------------------------------------------------------------

def cmd_xrefs(args) -> None:
    statements = _rest_statements(args.qid)
    if not isinstance(statements, dict):
        _die("unexpected REST statements shape")

    xrefs = []
    for pid, name, url_tpl in XREF_PROPS:
        for st in statements.get(pid, []):
            v, kind = _stmt_value(st)
            if kind == "value" and v is not None:
                xrefs.append({
                    "property": pid,
                    "source": name,
                    "value": v,
                    "url": url_tpl.format(urllib.parse.quote(str(v))) if url_tpl else "",
                })

    formula = []
    for st in statements.get(FORMULA_PROP[0], []):
        v, kind = _stmt_value(st)
        if kind == "value" and v is not None:
            formula.append(v if isinstance(v, str) else json.dumps(v))

    # enwiki sitelink as the @[wikidata] inverse sanity check. A 404 here just
    # means the item has no English Wikipedia article -- absent, not an error.
    sl = _get_json(
        f"{REST_BASE}/entities/items/{urllib.parse.quote(args.qid)}/sitelinks/enwiki",
        what=f"REST sitelink {args.qid}",
        allow_404=True,
    )
    enwiki = sl.get("title", "")

    out = {
        "qid": args.qid,
        "enwiki": enwiki,
        "xrefs": xrefs,
        "defining_formula": formula,
    }
    if args.json:
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return
    print(f"{args.qid}  formal cross-references"
          + (f"  (enwiki: {enwiki})" if enwiki else "  (no enwiki sitelink)"))
    if not xrefs and not formula:
        print("  (none of P12888/P4215/P2812/P6781/P10283/P2534 present)")
        return
    for x in xrefs:
        print(f"  {x['source']:<10} {x['property']:<8} {x['value']}")
        if x["url"]:
            print(f"  {'':<10} {'':<8} {x['url']}")
    for f in formula:
        print(f"  {FORMULA_PROP[1]:<10} {FORMULA_PROP[0]:<8} {f}")


# ---------------------------------------------------------------------------
# sitelinks: Wikipedia articles for a QID
# ---------------------------------------------------------------------------

def cmd_sitelinks(args) -> None:
    sl = _get_json(
        f"{REST_BASE}/entities/items/{urllib.parse.quote(args.qid)}/sitelinks",
        what=f"REST sitelinks {args.qid}",
    )
    # Keys like enwiki/dewiki/... ; restrict to *wiki (Wikipedias) unless --all.
    rows = []
    for site, info in sorted(sl.items()):
        if not args.all and not (site.endswith("wiki") and site not in ("commonswiki", "specieswiki")):
            continue
        rows.append({
            "site": site,
            "title": info.get("title", ""),
            "url": info.get("url", ""),
        })
    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return
    if not rows:
        print(f"({args.qid}: no matching sitelinks)")
        return
    print(f"{args.qid}  {len(rows)} sitelink(s):")
    for r in rows:
        print(f"  {r['site']:<14} {r['title']}")


# ---------------------------------------------------------------------------
# by_slug: enwiki article title/slug -> QID (exact sitelink lookup, NOT a search)
# ---------------------------------------------------------------------------

def cmd_by_slug(args) -> None:
    """Resolve an English-Wikipedia article title/slug to its QID.

    This is an EXACT sitelink -> QID lookup (Action API wbgetentities with
    sites=enwiki), not a label search. The article's own QID is the exact
    anchor for the top-level concept, so Agent 2 does not have to guess it.
    Underscores in a slug are normalized to spaces (both forms resolve).
    """
    title = args.title.replace("_", " ").strip()
    url = _action_url({
        "action": "wbgetentities",
        "sites": "enwiki",
        "titles": title,
        "props": "labels|descriptions|sitelinks",
        "languages": "en",
        "sitefilter": "enwiki",
    })
    data = _get_json(url, what="wbgetentities by slug")
    if "error" in data:
        _die(f"wbgetentities API error: {data['error'].get('info', data['error'])}")
    entities = data.get("entities", {})
    # A miss yields a synthetic "-1" key (or missing/"" flags).
    out = None
    for qid, ent in entities.items():
        if not qid.startswith("Q") or ent.get("missing") is not None:
            continue
        out = {
            "qid": qid,
            "label": ent.get("labels", {}).get("en", {}).get("value", ""),
            "description": ent.get("descriptions", {}).get("en", {}).get("value", ""),
            "enwiki": ent.get("sitelinks", {}).get("enwiki", {}).get("title", ""),
        }
        break
    if args.json:
        print(json.dumps(out or {}, indent=2, ensure_ascii=False))
        return
    if not out:
        print(f"(no Wikidata item with an enwiki article titled {title!r})")
        return
    print(f"{out['qid']}  {out['label']}"
          + (f"  (enwiki: {out['enwiki']})" if out["enwiki"] else ""))
    if out["description"]:
        print(f"  {out['description']}")


# ---------------------------------------------------------------------------
# semantic: local embedding search over the math-QID universe (no network)
# ---------------------------------------------------------------------------

# Resolved lazily relative to this file: catalog/data/wikidata_embeddings.*
_EMB_NPZ = None
_EMB_META = None


def _embed_paths():
    global _EMB_NPZ, _EMB_META
    if _EMB_NPZ is None:
        # .claude/skills/wikidata-search/wikidata.py -> repo root is parents[3].
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))))
        data = os.path.join(root, "catalog", "data")
        _EMB_NPZ = os.path.join(data, "wikidata_embeddings.npz")
        _EMB_META = os.path.join(data, "wikidata_embeddings.meta.jsonl")
    return _EMB_NPZ, _EMB_META


def cmd_semantic(args) -> None:
    """Embedding (meaning-based) search over the local math-QID universe.

    Loads catalog/data/wikidata_embeddings.npz (built offline by
    catalog/build_wikidata_embeddings.py), embeds the query with the SAME local
    model, cosine top-k. Fully local: no network, no injection surface. Fixes
    the broad-QID failure mode of the label-prefix `search`.
    """
    npz_path, meta_path = _embed_paths()
    if not os.path.isfile(npz_path):
        _die(f"embedding index not built: {npz_path} missing. Build it with:\n"
             f"  catalog/.venv/bin/python3 catalog/build_wikidata_embeddings.py")

    try:
        import numpy as np
    except Exception as e:
        _die(f"numpy not available for semantic search: {e}")
    try:
        from sentence_transformers import SentenceTransformer
    except Exception as e:
        _die(f"sentence-transformers not available for semantic search: {e}\n"
             f"install: <venv>/bin/pip install sentence-transformers")

    data = np.load(npz_path, allow_pickle=True)
    mat = data["embeddings"]                    # (N, d) float32, L2-normalized
    qids = [str(q) for q in data["qids"]]
    model_name = str(data["model"]) if "model" in data else "all-MiniLM-L6-v2"

    # Parallel meta for labels/descriptions (order matches the matrix rows).
    meta = []
    if os.path.isfile(meta_path):
        with open(meta_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    meta.append(json.loads(line))
    by_qid = {m["qid"]: m for m in meta}

    model = SentenceTransformer(model_name)
    qv = model.encode([args.query], normalize_embeddings=True,
                      convert_to_numpy=True).astype(np.float32)[0]
    scores = mat @ qv                            # cosine (both normalized)
    k = max(1, min(args.k, len(qids)))
    top = np.argpartition(-scores, k - 1)[:k]
    top = top[np.argsort(-scores[top])]

    results = []
    for i in top:
        q = qids[i]
        m = by_qid.get(q, {})
        results.append({
            "qid": q,
            "label": m.get("label", ""),
            "description": m.get("description", ""),
            "score": round(float(scores[i]), 4),
        })
    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
        return
    print(f"{len(results)} semantic match(es) for {args.query!r} "
          f"(cosine; DISAMBIGUATE by description, confirm with xrefs):")
    for r in results:
        desc = r["description"] or "(no description)"
        print(f"  {r['qid']:<12} {r['score']:<7} {r['label']}")
        print(f"  {'':<12} {'':<7} {desc}")


# ---------------------------------------------------------------------------
# sparql: WDQS main graph
# ---------------------------------------------------------------------------

def cmd_sparql(args) -> None:
    query = args.query
    if query == "-":
        query = sys.stdin.read()
    params = urllib.parse.urlencode({"query": query, "format": "json"})
    url = WDQS + "?" + params
    data = _get_json(url, what="WDQS SPARQL")
    head = data.get("head", {}).get("vars", [])
    bindings = data.get("results", {}).get("bindings", [])

    def cell(b: dict, var: str) -> str:
        if var not in b:
            return ""
        v = b[var]["value"]
        # Strip entity URIs down to the QID for readability.
        if b[var].get("type") == "uri" and "/entity/Q" in v:
            return v.rsplit("/", 1)[-1]
        return v

    if args.json:
        rows = [{var: cell(b, var) for var in head} for b in bindings]
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return
    if not bindings:
        print("(no results)")
        return
    print(f"{len(bindings)} row(s):")
    widths = {var: max(len(var), *(len(cell(b, var)) for b in bindings)) for var in head}
    print("  " + "  ".join(var.ljust(min(widths[var], 40)) for var in head))
    for b in bindings:
        print("  " + "  ".join(cell(b, var).ljust(min(widths[var], 40))[:40] for var in head))


# ---------------------------------------------------------------------------
# reconcile: reconciliation API (optional, 3rd-party WMCloud)
# ---------------------------------------------------------------------------

def cmd_reconcile(args) -> None:
    q = {"q0": {"query": args.label, "limit": args.limit}}
    if args.type:
        q["q0"]["type"] = args.type
    body = urllib.parse.urlencode({"queries": json.dumps(q)}).encode()
    # reconci.link 307-redirects to the wmcloud host; urllib follows redirects but
    # by default downgrades POST->GET only on 301/302/303, NOT 307 -- so a manual
    # GET to the same endpoint with queries in the query string is the robust path.
    get_url = RECONCILE + "?" + urllib.parse.urlencode({"queries": json.dumps(q)})
    try:
        data = _get_json(get_url, what="reconciliation API")
    except SystemExit:
        # Fall back to POST (some deployments only answer POST).
        raw = fetch(RECONCILE, data=body, method="POST", what="reconciliation API (POST)")
        try:
            data = json.loads(raw)
        except ValueError:
            _die("reconciliation API: response was not JSON")
    results = data.get("q0", {}).get("result", [])
    rows = [
        {
            "qid": r.get("id"),
            "name": r.get("name", ""),
            "description": r.get("description", ""),
            "score": r.get("score"),
            "match": r.get("match"),
        }
        for r in results
    ]
    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return
    if not rows:
        print(f"(no reconciliation candidates for {args.label!r})")
        return
    print(f"{len(rows)} candidate(s) for {args.label!r} "
          f"(3rd-party WMCloud, no SLA; score 0-100):")
    for r in rows:
        print(f"  {r['qid']:<12} score={r['score']:<6} match={r['match']}  {r['name']}")
        if r["description"]:
            print(f"  {'':<12} {r['description']}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="wikidata.py",
        description="Resolve concepts to Wikidata QIDs and inspect formal-library "
                    "cross-references + the structured graph (WikiLean reviewer tool).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    ps = sub.add_parser("search", help="wbsearchentities -> ranked candidate QIDs")
    ps.add_argument("label", help="label/alias prefix to search (e.g. 'prime ideal')")
    ps.add_argument("--limit", type=int, default=7, help="max candidates (1-50)")
    ps.add_argument("--type", default="item", choices=["item", "property"], help="entity type")
    ps.add_argument("--json", action="store_true", help="machine output")
    ps.set_defaults(func=cmd_search)

    pe = sub.add_parser("entity", help="labels/description + key claims (REST)")
    pe.add_argument("qid", help="QID, e.g. Q863912")
    pe.add_argument("--json", action="store_true", help="machine output")
    pe.set_defaults(func=cmd_entity)

    px = sub.add_parser("xrefs", help="formal cross-ref properties + enwiki sitelink")
    px.add_argument("qid", help="QID, e.g. Q863912")
    px.add_argument("--json", action="store_true", help="machine output")
    px.set_defaults(func=cmd_xrefs)

    pl = sub.add_parser("sitelinks", help="Wikipedia sitelinks for a QID")
    pl.add_argument("qid", help="QID, e.g. Q863912")
    pl.add_argument("--all", action="store_true", help="include non-Wikipedia sites")
    pl.add_argument("--json", action="store_true", help="machine output")
    pl.set_defaults(func=cmd_sitelinks)

    pb = sub.add_parser("by_slug", help="enwiki article title/slug -> QID (exact sitelink lookup)")
    pb.add_argument("title", help="enwiki article title or slug, e.g. 'Determinant'")
    pb.add_argument("--json", action="store_true", help="machine output")
    pb.set_defaults(func=cmd_by_slug)

    pm = sub.add_parser("semantic", help="local embedding search over the math-QID universe")
    pm.add_argument("query", help="prose description of the concept")
    pm.add_argument("--k", type=int, default=8, help="number of candidates (default 8)")
    pm.add_argument("--json", action="store_true", help="machine output")
    pm.set_defaults(func=cmd_semantic)

    pq = sub.add_parser("sparql", help="WDQS main-graph SPARQL (UA required)")
    pq.add_argument("query", help="SPARQL query string, or '-' to read from stdin")
    pq.add_argument("--json", action="store_true", help="machine output")
    pq.set_defaults(func=cmd_sparql)

    pr = sub.add_parser("reconcile", help="reconciliation API (optional, 3rd-party)")
    pr.add_argument("label", help="label to reconcile")
    pr.add_argument("--type", default=None, help="QID type constraint (e.g. Q24034552)")
    pr.add_argument("--limit", type=int, default=3, help="max candidates")
    pr.add_argument("--json", action="store_true", help="machine output")
    pr.set_defaults(func=cmd_reconcile)

    return p


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
