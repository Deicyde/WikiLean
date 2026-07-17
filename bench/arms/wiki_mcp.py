#!/usr/bin/env python3
"""wiki_mcp — arm B (informal-only) stdio MCP server for the Bridge Experiment.

Gives an agent RAW informal mathematical references and NOTHING formal: English
Wikipedia + the nLab. There is deliberately no Lean, no Mathlib, no Wikidata QID
lookup, no concept↔decl mapping anywhere in this file — arm B isolates "informal
reasoning is faster" with no bridge (docs/research/BRIDGE-EXPERIMENT.md).

Tools:
  wiki_search(q)            MediaWiki search over en.wikipedia (titles + snippets)
  wiki_get(title, section?) plaintext article extract, capped ~4k chars
  nlab_search(q)            ncatlab.org page text for a title guess, capped; fail-soft

Protocol: JSON-RPC 2.0 over stdio, newline-delimited (the MCP stdio transport).
Handles initialize / tools/list / tools/call / ping; notifications get no reply.
Every tool response is size-capped and every tool error is returned AS a tool
result (isError:true), never as a crash or a protocol error.

Stdlib only (urllib) — no third-party deps, no venv, matches bench/ style.

Self-test:  python3 wiki_mcp.py --selftest   (exercises the 3 tools + dispatch)
"""
from __future__ import annotations

import json
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

SERVER_NAME = "wiki"
SERVER_VERSION = "1.0.0"
DEFAULT_PROTOCOL = "2024-11-05"
UA = ("WikiLean-bridge-bench/1.0 "
      "(https://github.com/Deicyde/WikiLean; wikilean@jackmccarthy.org)")

WIKI_API = "https://en.wikipedia.org/w/api.php"
NLAB_SHOW = "https://ncatlab.org/nlab/show/"
NLAB_SEARCH = "https://ncatlab.org/nlab/search"

EXTRACT_CAP = 4000   # wiki_get plaintext cap
SEARCH_CAP = 8       # wiki_search hits
NLAB_CAP = 4000      # nlab_search text cap
HTTP_TIMEOUT = 25


# --------------------------------------------------------------------------- #
# HTTP (stdlib urllib + certifi fallback, same shape as bench/run_benchmark)   #
# --------------------------------------------------------------------------- #
def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    try:
        import certifi
        ctx.load_verify_locations(certifi.where())
    except (ImportError, ssl.SSLError):
        pass
    return ctx


def http_get(url: str, params: dict | None = None, timeout: int = HTTP_TIMEOUT) -> str:
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept": "application/json, text/html"})
    last = None
    for _ in range(2):  # one retry
        try:
            with urllib.request.urlopen(req, timeout=timeout,
                                        context=_ssl_context()) as resp:
                return resp.read().decode("utf-8", "replace")
        except (urllib.error.URLError, OSError, ssl.SSLError) as e:  # noqa: PERF203
            last = e
    raise RuntimeError(f"GET failed: {url[:120]} :: {last}")


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]+")


def strip_html(s: str) -> str:
    s = _TAG_RE.sub("", s)
    s = (s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
         .replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " "))
    return _WS_RE.sub(" ", s).strip()


def cap(s: str, n: int) -> str:
    s = s or ""
    return s if len(s) <= n else s[:n].rstrip() + f"\n…[truncated at {n} chars]"


# --------------------------------------------------------------------------- #
# Tools                                                                        #
# --------------------------------------------------------------------------- #
def tool_wiki_search(args: dict) -> str:
    q = (args.get("q") or "").strip()
    if not q:
        return "error: 'q' is required"
    raw = http_get(WIKI_API, {
        "action": "query", "format": "json", "list": "search",
        "srsearch": q, "srlimit": SEARCH_CAP, "srprop": "snippet"})
    data = json.loads(raw)
    hits = data.get("query", {}).get("search", [])
    if not hits:
        return f"No English Wikipedia results for {q!r}."
    out = [f"{len(hits)} result(s) for {q!r}:"]
    for h in hits:
        title = h.get("title", "?")
        slug = title.replace(" ", "_")
        snip = strip_html(h.get("snippet", ""))
        out.append(f"- {title}  (slug: {slug})\n    https://en.wikipedia.org/wiki/{slug}"
                   + (f"\n    {snip}" if snip else ""))
    return cap("\n".join(out), 3500)


def tool_wiki_get(args: dict) -> str:
    title = (args.get("title") or "").strip()
    if not title:
        return "error: 'title' is required"
    section = (args.get("section") or "").strip()
    raw = http_get(WIKI_API, {
        "action": "query", "format": "json", "prop": "extracts",
        "explaintext": 1, "redirects": 1, "titles": title})
    pages = json.loads(raw).get("query", {}).get("pages", {})
    page = next(iter(pages.values()), {}) if pages else {}
    if not page or "missing" in page or not page.get("extract"):
        return f"No article extract for {title!r} (missing or empty)."
    text = page["extract"]
    real_title = page.get("title", title)
    if section:
        # explaintext extracts render section headings on their own bare line;
        # slice from the first heading equal to `section` to the next heading.
        lines = text.splitlines()
        low = section.casefold()
        start = None
        for i, ln in enumerate(lines):
            if ln.strip().casefold() == low:
                start = i + 1
                break
        if start is not None:
            body = []
            for ln in lines[start:]:
                # a bare short line with no sentence punctuation ~ next heading
                st = ln.strip()
                if st and len(st) < 60 and not st.endswith((".", ":", ",", ";")) \
                        and body and not body[-1].strip():
                    break
                body.append(ln)
            text = f"== {section} ==\n" + "\n".join(body).strip()
        else:
            text = f"[section {section!r} not found; returning article head]\n" + text
    return cap(f"# {real_title}\n{text}", EXTRACT_CAP)


def tool_nlab_search(args: dict) -> str:
    q = (args.get("q") or "").strip()
    if not q:
        return "error: 'q' is required"
    # No clean nLab API: title-guess the /nlab/show/ page, else the search page.
    slug = urllib.parse.quote(q.replace(" ", "+"), safe="+")
    try:
        html = http_get(NLAB_SHOW + slug)
        text = _nlab_text(html)
        if text and "no such page" not in text.casefold():
            return cap(f"nLab page for {q!r} ({NLAB_SHOW}{slug}):\n{text}", NLAB_CAP)
    except Exception:  # noqa: BLE003  — fall through to search, fail-soft
        pass
    try:
        html = http_get(NLAB_SEARCH, {"query": q})
        links = re.findall(r'/nlab/show/([^"\'<>]+)', html)
        seen, uniq = set(), []
        for ln in links:
            if ln not in seen:
                seen.add(ln)
                uniq.append(ln)
            if len(uniq) >= 12:
                break
        if uniq:
            body = "\n".join(f"- {urllib.parse.unquote(u).replace('+', ' ')}"
                             for u in uniq)
            return cap(f"nLab search for {q!r} — candidate pages:\n{body}", NLAB_CAP)
    except Exception as e:  # noqa: BLE003
        return f"nLab unavailable for {q!r}: {e}"
    return f"No nLab page or search results for {q!r}."


def _nlab_text(html: str) -> str:
    # Grab the main content div if present, else the whole body; strip markup.
    m = re.search(r'<div[^>]*id=["\']Content["\'][^>]*>(.*?)</div>\s*</div>',
                  html, re.DOTALL | re.IGNORECASE)
    frag = m.group(1) if m else html
    frag = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", frag, flags=re.DOTALL | re.I)
    frag = re.sub(r"</(p|div|li|h[1-6]|br)>", "\n", frag, flags=re.IGNORECASE)
    return re.sub(r"\n{3,}", "\n\n", strip_html_lines(frag)).strip()


def strip_html_lines(html: str) -> str:
    out = []
    for ln in html.splitlines():
        t = strip_html(ln)
        if t:
            out.append(t)
    return "\n".join(out)


TOOLS = {
    "wiki_search": {
        "description": ("Search English Wikipedia for math articles. Returns matching "
                        "article titles, their slugs, and one-line snippets. Use to "
                        "find the canonical informal source for a concept."),
        "inputSchema": {"type": "object",
                        "properties": {"q": {"type": "string",
                                             "description": "search query"}},
                        "required": ["q"]},
        "handler": tool_wiki_search,
    },
    "wiki_get": {
        "description": ("Fetch the plaintext of an English Wikipedia article (capped "
                        "~4000 chars). Optionally pass a section heading to return just "
                        "that section. Use to read a concept's informal definition."),
        "inputSchema": {"type": "object",
                        "properties": {
                            "title": {"type": "string",
                                      "description": "article title or slug"},
                            "section": {"type": "string",
                                        "description": "optional section heading"}},
                        "required": ["title"]},
        "handler": tool_wiki_get,
    },
    "nlab_search": {
        "description": ("Look up a page on the nLab (ncatlab.org), the research-level "
                        "wiki for category theory and higher mathematics. Returns page "
                        "text for a title guess, or candidate page names. Informal only."),
        "inputSchema": {"type": "object",
                        "properties": {"q": {"type": "string",
                                             "description": "concept/page name"}},
                        "required": ["q"]},
        "handler": tool_nlab_search,
    },
}


# --------------------------------------------------------------------------- #
# Minimal JSON-RPC 2.0 stdio MCP loop (shared shape with formal_mcp.py)         #
# --------------------------------------------------------------------------- #
def _result(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def handle(msg: dict):
    """Return a response dict, or None for notifications (no id)."""
    method = msg.get("method")
    mid = msg.get("id")
    if method is None:
        return None
    if method == "initialize":
        proto = (msg.get("params") or {}).get("protocolVersion") or DEFAULT_PROTOCOL
        return _result(mid, {
            "protocolVersion": proto,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
    if method.startswith("notifications/"):
        return None  # notifications never get a response
    if method == "ping":
        return _result(mid, {})
    if method == "tools/list":
        return _result(mid, {"tools": [
            {"name": n, "description": t["description"], "inputSchema": t["inputSchema"]}
            for n, t in TOOLS.items()]})
    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        tool = TOOLS.get(name)
        if tool is None:
            return _result(mid, {"content": [{"type": "text",
                                              "text": f"error: unknown tool {name!r}"}],
                                 "isError": True})
        try:
            text = tool["handler"](args)
            is_err = isinstance(text, str) and text.startswith("error:")
        except Exception as e:  # noqa: BLE003 — tool errors are results, not crashes
            text, is_err = f"tool {name!r} failed: {e}", True
        return _result(mid, {"content": [{"type": "text", "text": str(text)}],
                             "isError": bool(is_err)})
    if mid is not None:
        return _error(mid, -32601, f"method not found: {method}")
    return None


def serve() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = handle(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


def selftest() -> int:
    ok = True
    assert handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                   "params": {}})["result"]["serverInfo"]["name"] == SERVER_NAME
    assert len(handle({"jsonrpc": "2.0", "id": 2,
                       "method": "tools/list"})["result"]["tools"]) == 3
    for name, args in [("wiki_search", {"q": "compact group"}),
                       ("wiki_get", {"title": "Compact group"}),
                       ("nlab_search", {"q": "compact space"})]:
        r = handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                    "params": {"name": name, "arguments": args}})["result"]
        text = r["content"][0]["text"]
        ok = ok and bool(text)
        print(f"  {name}: isError={r.get('isError', False)} len={len(text)} "
              f":: {text[:70].replace(chr(10), ' ')}…")
    print("wiki_mcp selftest OK" if ok else "wiki_mcp selftest: empty tool output")
    return 0 if ok else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    sys.exit(serve())
