#!/usr/bin/env python3
"""Local review server for correcting WikiLean annotations in context.

Serves each rendered article with an injected edit panel. Click a highlight to
change its status / decl / module / note (or delete it); select text to add a
new annotation. Saving writes straight back to annotations/<slug>.json and
re-renders the page — our JSON stays the single source of truth.

Local-only (binds 127.0.0.1). Run:
    python serve_review.py            # http://127.0.0.1:8742
    python serve_review.py --port 9000
"""
from __future__ import annotations

import argparse
import html as htmllib
import json
import subprocess
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ANNOT = HERE / "annotations"
OUT = HERE / "out"
ASSETS = HERE / "assets"


def final_slugs() -> list[str]:
    return sorted(p.stem for p in ANNOT.glob("*.json")
                  if not p.name.endswith(".agent1.json"))


def inject_editor(html: str, slug: str, annots: dict) -> str:
    payload = json.dumps(annots, ensure_ascii=False).replace("</", "<\\/")
    inject = (
        f'<script>window.__WL_SLUG__={json.dumps(slug)};'
        f'window.__WL_FULL_ANNOS__={payload};</script>\n'
        '<link rel="stylesheet" href="/assets/review.css">\n'
        '<script src="/assets/review.js"></script>\n'
    )
    idx = html.rfind("</body>")
    return html[:idx] + inject + html[idx:] if idx != -1 else html + inject


def index_page() -> bytes:
    rows = []
    for slug in final_slugs():
        try:
            d = json.loads((ANNOT / f"{slug}.json").read_text())
            n = len(d.get("annotations") or [])
            title = d.get("display_title") or slug
        except (json.JSONDecodeError, OSError):
            n, title = 0, slug
        rows.append(f'<li><a href="/article/{urllib.parse.quote(slug)}">'
                    f'{htmllib.escape(title)}</a> <span>({n})</span></li>')
    body = (
        "<!doctype html><meta charset=utf-8><title>WikiLean review</title>"
        "<style>body{font:15px -apple-system,sans-serif;max-width:760px;"
        "margin:40px auto;padding:0 20px}h1{font-size:20px}li{margin:3px 0}"
        "span{color:#888;font-size:12px}</style>"
        f"<h1>WikiLean review — {len(rows)} articles</h1>"
        '<p><a href="/concepts">→ concept-layer coverage dashboard</a></p><ul>'
        + "".join(rows) + "</ul>"
    )
    return body.encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/":
            return self._send(200, index_page())
        if path.startswith("/assets/"):
            f = ASSETS / Path(path).name
            if f.exists():
                ctype = "text/css" if f.suffix == ".css" else "application/javascript"
                return self._send(200, f.read_bytes(), ctype)
            return self._send(404, b"not found")
        if path.startswith("/article/"):
            slug = urllib.parse.unquote(path[len("/article/"):])
            html_path = OUT / f"{slug}.html"
            annot_path = ANNOT / f"{slug}.json"
            if not html_path.exists() or not annot_path.exists():
                return self._send(404, b"no rendered article / annotations")
            annots = json.loads(annot_path.read_text())
            page = inject_editor(html_path.read_text(), slug, annots)
            return self._send(200, page.encode("utf-8"))
        if path == "/concepts" or path == "/concepts.html":
            # Concept-layer dashboard. Regenerate so it reflects the latest
            # concept_layer.jsonl, then serve the same file that deploys static.
            page = OUT / "concepts.html"
            subprocess.run([sys.executable, str(HERE / "build_concepts_page.py")],
                           capture_output=True, cwd=str(HERE))
            if page.exists():
                return self._send(200, page.read_bytes())
            return self._send(500, b"could not build concepts page")
        if path.endswith(".html"):
            # Read-only static article view (so dashboard links resolve locally).
            f = OUT / Path(path).name
            if f.exists():
                return self._send(200, f.read_bytes())
        return self._send(404, b"not found")

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if not path.startswith("/api/save/"):
            return self._send(404, b"not found")
        slug = urllib.parse.unquote(path[len("/api/save/"):])
        annot_path = ANNOT / f"{slug}.json"
        if not annot_path.exists():
            return self._send(404, b'{"ok":false,"error":"unknown slug"}',
                              "application/json")
        length = int(self.headers.get("Content-Length", "0"))
        try:
            posted = json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            return self._send(400, b'{"ok":false,"error":"bad json"}',
                              "application/json")
        # Preserve the on-disk envelope; replace only the annotations list.
        envelope = json.loads(annot_path.read_text())
        envelope["annotations"] = posted.get("annotations", [])
        annot_path.write_text(
            json.dumps(envelope, ensure_ascii=False, indent=2), encoding="utf-8")
        # Re-render so the highlights reflect the edit.
        proc = subprocess.run([sys.executable, str(HERE / "render.py"), slug],
                              capture_output=True, text=True, cwd=str(HERE))
        ok = proc.returncode == 0
        import re
        m = re.search(r"(\d+)/(\d+) matched", proc.stdout)
        resp = {"ok": ok, "matched": m.group(0) if m else None,
                "render_output": (proc.stdout + proc.stderr)[-400:]}
        return self._send(200 if ok else 500,
                          json.dumps(resp).encode("utf-8"), "application/json")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8742)
    args = ap.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"WikiLean review server → http://127.0.0.1:{args.port}/  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
