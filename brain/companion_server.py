#!/usr/bin/env python3
"""Read-only, loopback-only browser for an imported companion SQLite index."""
from __future__ import annotations

import argparse
import html
import json
import sqlite3
import sys
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from brain import store
from brain.ingest.companion import PROFILE

MAX_LIMIT = 100
MAX_OFFSET = 1_000_000
FILTERS = {"q", "source", "target", "offset", "limit", "generation"}

CSS = """
:root{color-scheme:light;font-family:ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#193143;background:#f4f7f8}
*{box-sizing:border-box}body{margin:0}main{max-width:1080px;margin:0 auto;padding:36px 24px 64px}header{padding-bottom:24px;border-bottom:1px solid #d5e0e4}
.brand{font-size:13px;letter-spacing:.11em;text-transform:uppercase;color:#49747b;font-weight:650}h1{font-size:36px;letter-spacing:-.035em;margin:10px 0}header p{margin:0;color:#587080;max-width:680px;line-height:1.6}
.summary{display:flex;gap:9px;flex-wrap:wrap;margin-top:20px}.badge{border:1px solid #cbdcdf;border-radius:20px;padding:5px 10px;font-size:12px;background:#fff;color:#375c68}.mode{background:#e7f2f1;border-color:#c8dfdc;color:#176256}
form{margin:24px 0;padding:20px;border:1px solid #d5e0e4;border-radius:12px;background:white}label{display:block;font-size:13px;font-weight:600;margin-bottom:7px}input{width:100%;font:inherit;border:1px solid #b9cbd2;border-radius:7px;padding:10px 12px;color:inherit;background:#fff}input:focus{outline:3px solid #b8e1dd;outline-offset:1px}
.search-row{display:flex;align-items:end;gap:10px}.search-row>div{flex:1}button,.button{display:inline-block;background:#146b61;color:white;border:0;border-radius:7px;padding:11px 16px;font:inherit;cursor:pointer;text-decoration:none;white-space:nowrap}button:hover,.button:hover{background:#10554e}.reset{font-size:13px;margin-left:12px}.exact{margin-top:16px}.exact-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:14px}
a{color:#136e77;text-decoration-thickness:1px;text-underline-offset:3px}a:hover{color:#084b51}.result-line{display:flex;justify-content:space-between;align-items:center;color:#587080;font-size:14px;margin:26px 0 12px}.cards{display:grid;gap:16px}.card{background:#fff;border:1px solid #d5e0e4;border-radius:12px;padding:22px}.card-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:17px}.card-head h2{font-size:16px;margin:0;font-weight:650;overflow-wrap:anywhere}.pair{display:grid;grid-template-columns:1fr 1fr;gap:26px}.eyebrow{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:#667c89;font-weight:650;margin:0 0 7px}.reference{font-size:14px;line-height:1.6;overflow-wrap:anywhere;margin:0}.unresolved{color:#925b20;font-size:12px;margin:5px 0}.location{margin-top:19px;padding-top:17px;border-top:1px solid #edf1f3}.origin{margin-top:15px;font-size:12px;color:#667c89;overflow-wrap:anywhere;line-height:1.7}details{margin-top:15px}summary{cursor:pointer;font-size:13px;color:#375c68}pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:460px;overflow:auto;font:12px/1.6 ui-monospace,SFMono-Regular,Consolas,monospace;background:#f4f7f8;border-radius:7px;padding:13px;color:#294857}.location pre{max-height:160px;margin-bottom:0}.empty{padding:42px 20px;text-align:center;background:white;border:1px dashed #bbced5;border-radius:12px}.empty p{color:#587080}.pagination{display:flex;justify-content:space-between;margin-top:22px;gap:12px}.pagination .button{font-size:14px}footer{font-size:12px;color:#718592;line-height:1.7;margin-top:32px}.error{margin-top:25px;background:#fff5ed;border:1px solid #ead3bc;border-radius:12px;padding:24px}.error pre{background:white}
@media(max-width:680px){main{padding:24px 14px 40px}h1{font-size:29px}.pair,.exact-grid{grid-template-columns:1fr;gap:19px}.card{padding:18px}.search-row{flex-wrap:wrap}.search-row>div{flex-basis:100%}.card-head{align-items:start}.result-line{align-items:start;gap:12px}}
"""


def _json(value, *, pretty=False):
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      indent=2 if pretty else None, separators=None if pretty else (",", ":"))


def _escape(value):
    return html.escape(str(value), quote=True)


def _link(value: str, label: str) -> str:
    """Only deliberate clicks may open ordinary web links; never file/data/script URIs."""
    try:
        parts = urlsplit(value)
        safe = parts.scheme in {"http", "https"} and bool(parts.hostname)
    except ValueError:
        safe = False
    if safe:
        return f'<a href="{_escape(value)}" target="_blank" rel="noopener noreferrer">{_escape(label)}</a>'
    return _escape(label)


@contextmanager
def open_index(data_dir: Path, *, generation_root: Path | None = None):
    # Freeze one selection before opening any files. A coordinator may atomically
    # advance the current symlink while a request continues on this generation.
    selected = data_dir.resolve(strict=True)
    if generation_root is not None and (selected == generation_root or not selected.is_relative_to(generation_root)):
        raise store.StoreError("The selected index is outside the workspace generation directory")
    database = store.open_store(data_dir=selected, backend="sqlite")
    try:
        metadata = database.metadata("nodes")
        if metadata.get("profile") != PROFILE:
            raise store.StoreError("Select a local companion import, not a production Brain snapshot")
        yield database, metadata
    finally:
        database.close()


class GenerationChanged(ValueError):
    pass


def read_page(database, metadata, params):
    """Page the existing edge table with parameterized, bounded SQL reads."""
    if params.get("generation") and params["generation"] != metadata["generated_at"]:
        raise GenerationChanged("The local workspace has changed. Refresh to browse the new links.")
    try:
        limit = int(params.get("limit", "25"))
        offset = int(params.get("offset", "0"))
    except ValueError as exc:
        raise ValueError("Page size and offset must be integers") from exc
    if not 1 <= limit <= MAX_LIMIT or not 0 <= offset <= MAX_OFFSET:
        raise ValueError(f"Page size must be 1–{MAX_LIMIT}; offset must be 0–{MAX_OFFSET}")
    conditions = ["stream = 'links'", "kind = 'links'"]
    values = []
    for name, column in (("source", "src"), ("target", "dst")):
        if params.get(name):
            conditions.append(f"{column} = ?")
            values.append(params[name])
    if params.get("q"):
        # instr treats %, _, and quotes literally. SQLite lower() supplies ASCII
        # case folding; Unicode and encoded URI characters remain literal.
        conditions.append("(instr(lower(src), lower(?)) > 0 OR instr(lower(dst), lower(?)) > 0)")
        values.extend([params["q"], params["q"]])
    where = " AND ".join(conditions)
    total = database.connection.execute(f"SELECT count(*) FROM edges WHERE {where}", values).fetchone()[0]
    rows = database.connection.execute(
        f"SELECT ordinal, payload_json FROM edges WHERE {where} ORDER BY ordinal LIMIT ? OFFSET ?",
        [*values, limit, offset])
    records = []
    for ordinal, payload in rows:
        edge = json.loads(payload)
        evidence = edge["evidence"]
        records.append({
            "ordinal": ordinal,
            "annotation": json.loads(evidence["annotation_json"]),
            "origin": evidence["origin"], "source": edge["src"], "target": edge["dst"],
            "resolution": evidence.get("resolution", {
                "source": {"status": "legacy"}, "target": {"status": "legacy"}}),
        })
    return {"records": records, "offset": offset, "limit": limit, "total": total,
            "next_offset": offset + limit if offset + limit < min(total, MAX_OFFSET + 1) else None,
            "generation": metadata["generated_at"]}


def report(database, metadata):
    total = database.connection.execute("SELECT count(*) FROM edges WHERE stream = 'links' AND kind = 'links'").fetchone()[0]
    return {"annotations": total, "source_key": metadata.get("source_key"),
            "generation": metadata["generated_at"], "profile": PROFILE,
            "publication_status": metadata.get("publication_status")}


def _document(content: str) -> str:
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<title>Companion links · WikiLean</title><link rel="stylesheet" href="/assets/viewer.css">'
            '</head><body><main><header><div class="brand">WikiLean · Local companion</div>'
            '<h1>Companion links</h1><p>Explore informal sources and their formal counterparts in your local workspace.</p>'
            '</header>' + content + '<footer>Read-only local view. Imported associations retain their source locations and original annotations.</footer></main></body></html>')


def _page_url(params, *, offset=None, generation=None):
    query = {key: value for key, value in params.items() if value and key in FILTERS}
    if offset is not None:
        query["offset"] = str(offset)
    if generation is not None:
        query["generation"] = generation
    return "/?" + urlencode(query)


def page_html(page, metadata, params):
    cards = []
    for row in page["records"]:
        annotation = row["annotation"]
        extensions = annotation.get("extensions", {})
        identity = None
        for namespace, field in (("org.autoform", "title"), ("org.wikilean", "label")):
            data = extensions.get(namespace)
            label = data.get(field) if isinstance(data, dict) else None
            if isinstance(label, str) and label.strip():
                identity = label[:160]
                break
        opaque_id = annotation.get("id")
        identity = identity or (opaque_id if isinstance(opaque_id, str) and len(opaque_id) <= 80 else None)
        identity = identity or f"Link {row['ordinal'] + 1}"
        mode = annotation["source"]["type"]
        resources = []
        for field, title, label in (("source", "Informal source", annotation["source"]["uri"]),
                                     ("target", "Formal result", annotation["target"])):
            state = row["resolution"][field]["status"]
            link = _link(row[field], label) if state != "unresolved" else _escape(label)
            resources.append(f'<div><p class="eyebrow">{title}</p><p class="reference">{link}</p>'
                             + ('<p class="unresolved">Reference needs a resolver</p>' if state == "unresolved" else '') + '</div>')
        location = ('<pre>' + _escape(_json(annotation["location"], pretty=True)) + '</pre>'
                    if "location" in annotation else '<p class="reference">Whole source</p>')
        origin = row["origin"]
        entry = f"Line {origin['line']}" if "line" in origin else (origin.get("pointer") or "Single annotation")
        details = {"origin": origin, "source": row["source"], "target": row["target"], "resolution": row["resolution"]}
        cards.append(
            '<article class="card"><div class="card-head"><h2>' + _escape(identity) + '</h2><span class="badge mode">'
            + _escape(mode.upper()) + '</span></div><div class="pair">' + ''.join(resources) + '</div>'
            + '<div class="location"><p class="eyebrow">Location in the source</p>' + location + '</div>'
            + '<div class="origin">Stored in ' + _escape(origin['base_uri']) + '<br>' + _escape(entry) + '</div>'
            + '<details><summary>Full annotation</summary><pre>' + _escape(_json(annotation, pretty=True)) + '</pre></details>'
            + '<details><summary>Reference and origin details</summary><pre>' + _escape(_json(details, pretty=True)) + '</pre></details></article>')
    filters = ('<form action="/" method="get"><div class="search-row"><div>'
               '<label for="q">Find a source or formal result</label><input id="q" name="q" maxlength="2048" value="'
               + _escape(params.get("q", "")) + '" placeholder="A theorem name, URL, or source filename"></div>'
               '<button type="submit">Search</button><a class="reset" href="/">Clear</a></div>'
               '<details class="exact"' + (' open' if params.get('source') or params.get('target') else '')
               + '><summary>Match a specific link</summary><div class="exact-grid">'
               + ''.join('<div><label for="' + name + '">' + label + '</label><input id="' + name + '" name="' + name
                         + '" maxlength="4096" value="' + _escape(params.get(name, "")) + '"></div>'
                         for name, label in (("source", "Source link (exact)"), ("target", "Formal link (exact)")))
               + '</div></details></form>')
    start = page['offset'] + 1 if page['records'] else 0
    end = page['offset'] + len(page['records']) if page['records'] else 0
    count = f"{page['total']:,} link" + ("" if page['total'] == 1 else "s")
    summary = '<div class="summary"><span class="badge">' + _escape(metadata.get('source_key', 'Local workspace')) + '</span><span class="badge">Imported associations</span></div>'
    results = '<div class="result-line"><span>' + count + '</span><span>' + f"Showing {start:,}–{end:,}" + '</span></div>'
    if not cards:
        results += '<div class="empty"><h2>No matching links</h2><p>Try a shorter name or clear the search.</p></div>'
    else:
        results += '<div class="cards">' + ''.join(cards) + '</div>'
    previous = next_page = '<span></span>'
    if page['offset']:
        previous = '<a class="button" href="' + _escape(_page_url(params, offset=max(0, page['offset'] - page['limit']), generation=page['generation'])) + '">Previous</a>'
    if page['next_offset'] is not None:
        next_page = '<a class="button" href="' + _escape(_page_url(params, offset=page['next_offset'], generation=page['generation'])) + '">Next</a>'
    return _document(summary + filters + results + '<nav class="pagination" aria-label="Results pages">' + previous + next_page + '</nav>')


class CompanionServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, data_dir: Path, port: int, generation_root: Path | None = None):
        self.data_dir = Path(data_dir).absolute()  # Keep a current symlink live between requests.
        self.generation_root = generation_root.resolve(strict=True) if generation_root is not None else None
        super().__init__(("127.0.0.1", port), Handler)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(10)
        return connection, address


class Handler(BaseHTTPRequestHandler):
    server: CompanionServer

    def log_message(self, *args):
        pass  # Do not log private annotation query strings.

    def _send(self, status, body, content_type="application/json; charset=utf-8", *, head=False):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        if status == 405:
            self.send_header("Allow", "GET, HEAD")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-DNS-Prefetch-Control", "off")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        if not head:
            self.wfile.write(data)

    def _guard(self, *, head=False):
        hosts = self.headers.get_all("Host", [])
        origins = self.headers.get_all("Origin", [])
        port = self.server.server_address[1]
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if len(hosts) != 1 or hosts[0] not in allowed:
            self._send(403, _json({"error": "Only the local viewer host is accepted"}), head=head)
            return False
        if len(origins) > 1 or (origins and origins[0] != "http://" + hosts[0]):
            self._send(403, _json({"error": "Cross-origin requests are not accepted"}), head=head)
            return False
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            self._send(403, _json({"error": "Cross-site requests are not accepted"}), head=head)
            return False
        return True

    def _get(self, *, head=False):
        if not self._guard(head=head):
            return
        try:
            if len(self.path) > 16384:
                raise ValueError("Request URL is too long")
            request = urlsplit(self.path)
            if request.scheme or request.netloc:
                raise ValueError("Use a local relative request path")
            if request.path == "/assets/viewer.css":
                return self._send(200, CSS, "text/css; charset=utf-8", head=head)
            if request.path not in {"/", "/api/annotations", "/api/report"}:
                return self._send(404, _json({"error": "Not found"}), head=head)
            query = parse_qs(request.query, keep_blank_values=True, max_num_fields=12)
            if any(key not in FILTERS or len(value) != 1 for key, value in query.items()):
                raise ValueError("Unknown or repeated query parameter")
            params = {key: values[0] for key, values in query.items()}
            for key, value in params.items():
                if len(value) > (2048 if key == "q" else 4096):
                    raise ValueError("Search value is too long")
            with open_index(self.server.data_dir, generation_root=self.server.generation_root) as (database, metadata):
                if params.get("generation") and params["generation"] != metadata["generated_at"]:
                    raise GenerationChanged("The local workspace has changed. Refresh to browse the new links.")
                if request.path == "/api/report":
                    return self._send(200, _json(report(database, metadata)), head=head)
                page = read_page(database, metadata, params)
                if request.path == "/api/annotations":
                    return self._send(200, _json(page), head=head)
                return self._send(200, page_html(page, metadata, params), "text/html; charset=utf-8", head=head)
        except GenerationChanged as exc:
            if request.path == "/":
                params.pop("generation", None)
                content = '<div class="error"><h2>New links are ready</h2><p>' + _escape(exc) + '</p><a class="button" href="' + _escape(_page_url(params, offset=0)) + '">Refresh results</a></div>'
                return self._send(409, _document(content), "text/html; charset=utf-8", head=head)
            self._send(409, _json({"error": str(exc)}), head=head)
        except (ValueError, KeyError) as exc:
            self._send(400, _json({"error": str(exc)}), head=head)
        except (OSError, store.StoreError, sqlite3.Error) as exc:
            self._send(503, _json({"error": "The local companion index is unavailable", "detail": str(exc)}), head=head)

    def do_GET(self):
        self._get()

    def do_HEAD(self):
        self._get(head=True)

    def _read_only(self):
        self.close_connection = True
        if self._guard():
            self._send(405, _json({"error": "The companion viewer is read-only"}))

    do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _read_only


def create_server(data_dir: Path | str, *, port=0, generation_root: Path | str | None = None) -> CompanionServer:
    return CompanionServer(Path(data_dir), port, Path(generation_root) if generation_root is not None else None)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True,
                        help="Imported index directory, optionally reached through a current-generation symlink")
    parser.add_argument("--port", type=int, default=8743)
    parser.add_argument("--generation-root", type=Path,
                        help="Optionally restrict every selected index to this workspace generation directory")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("port must be between 0 and 65535")
    try:
        generation_root = args.generation_root.resolve(strict=True) if args.generation_root is not None else None
        with open_index(args.data_dir, generation_root=generation_root):
            pass
        server = create_server(args.data_dir, port=args.port, generation_root=generation_root)
    except (OSError, store.StoreError, ValueError) as exc:
        print(f"companion viewer: {exc}", file=sys.stderr)
        return 2
    print(f"Companion viewer: http://127.0.0.1:{server.server_address[1]}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
