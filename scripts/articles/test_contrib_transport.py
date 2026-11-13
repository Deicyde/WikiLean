#!/usr/bin/env python3
"""The contributor transport (contrib_transport.py) against a loopback stand-in server:
the path allowlist, refusals and waits, no redirects, the token kept out of errors,
resumable verified downloads, and a bundle fetched into its cache."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import contrib_transport as transport  # noqa: E402

TOKEN = "wl_agent_fixture-token-123"


def SHA(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.seen.append(("GET", self.path, self.headers))
        self.server.app(self, None)

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.server.seen.append(("POST", self.path, self.headers))
        self.server.app(self, body)

    def answer(self, status: int, body: bytes = b"", headers: dict[str, str] | None = None, *, cut: int | None = None):
        self.send_response(status)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        if cut is not None:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(body if cut is None else body[:cut])

    def json(self, status: int, value: dict, headers: dict[str, str] | None = None):
        self.answer(status, json.dumps(value).encode(), {"Content-Type": "application/json", **(headers or {})})


class Server:
    def __init__(self, app):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.app, self.httpd.seen = app, []
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()

    @property
    def seen(self):
        return self.httpd.seen


def ranged(handler: Handler, data: bytes, *, cut: int | None = None, ignore_range: bool = False):
    """Serve data as bundles.ts does: one byte range when asked, else the whole file."""
    header = handler.headers.get("Range")
    if header and not ignore_range:
        start, _, end = header.removeprefix("bytes=").partition("-")
        first, last = int(start), min(int(end), len(data) - 1)
        handler.answer(206, data[first:last + 1], {"Content-Range": f"bytes {first}-{last}/{len(data)}"}, cut=cut)
    else:
        handler.answer(200, data, cut=cut)


class PathsAndTokens(unittest.TestCase):
    def test_only_the_listed_paths_are_requested(self):
        sha = "a" * 64
        for path in ("/api/packages", "/api/packages/loc-7/claims", "/api/packages/loc-7/state", "/api/claims",
                     "/api/claims/clm-" + "0" * 32 + "/package", "/api/index/bundles", f"/api/index/bundles/{sha}",
                     f"/api/index/bundles/{sha}/files/index/decl-index.jsonl", "/api/submissions/sub-" + sha + "/body"):
            self.assertTrue(transport.PATHS.fullmatch(path), path)
        for path in ("/api/agents", "/api/packages?x=1", "/api/claims/clm-x/package", f"/api/index/bundles/{sha}/files/../x",
                     f"/api/index/bundles/{sha}/files/a b", f"/api/index/bundles/{sha}/files/.hidden", "/api/articles/tag"):
            with self.assertRaises(transport.TransportError, msg=path):
                transport.call("https://wikilean.example", path, TOKEN)

    def test_the_token_never_appears_in_an_error(self):
        with Server(lambda handler, body: None) as server:
            base = server.base
        # The server is gone: the connection fails, and the error names no token.
        with self.assertRaises(transport.TransportError) as caught:
            transport.call(base, "/api/index/bundles", TOKEN)
        self.assertNotIn(TOKEN, str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        for token in ("", "two words"):
            with self.assertRaises(transport.TransportError) as caught:
                transport.call(base, "/api/index/bundles", token)
            self.assertIn("WIKILEAN_AGENT_TOKEN", str(caught.exception))

    def test_only_https_or_loopback_origins(self):
        for base in ("http://wikilean.example", "https://user:pw@wikilean.example", "https://wikilean.example/x"):
            with self.assertRaises(transport.TransportError):
                transport.call(base, "/api/index/bundles", TOKEN)


class Calls(unittest.TestCase):
    def test_a_json_answer_and_a_refusal_with_its_code(self):
        def app(handler, body):
            if handler.path == "/api/claims":
                request = json.loads(body)
                if request["request_key"] == "1" * 32:
                    handler.json(201, {"ok": True, "claim": {"id": "clm-" + "0" * 32}})
                else:
                    handler.json(409, {"ok": False, "code": "claimed", "error": "another agent holds it"})
            else:
                handler.json(503, {"ok": False, "error": "editing is paused", "write_mode": "locked"})
        with Server(app) as server:
            answer = transport.call(server.base, "/api/claims", TOKEN, {"request_key": "1" * 32})
            self.assertEqual(answer["claim"]["id"], "clm-" + "0" * 32)
            method, _, headers = server.seen[0]
            self.assertEqual((method, headers["Authorization"]), ("POST", "Bearer " + TOKEN))
            with self.assertRaises(transport.Refusal) as caught:
                transport.call(server.base, "/api/claims", TOKEN, {"request_key": "2" * 32})
            self.assertEqual((caught.exception.status, caught.exception.code), (409, "claimed"))
            with self.assertRaises(transport.Refusal) as caught:
                transport.call(server.base, "/api/packages", TOKEN)
            self.assertEqual((caught.exception.status, caught.exception.code), (503, "brake"))

    def test_a_short_wait_is_waited_out_and_a_long_one_is_not(self):
        answers = [(429, "7"), (429, "7"), (200, None)]

        def app(handler, body):
            status, wait = answers.pop(0) if answers else (429, "3600")
            if status == 200:
                handler.json(200, {"ok": True, "bundles": []})
            else:
                handler.json(429, {"ok": False, "code": "rate_limit", "error": "slow down"}, {"Retry-After": wait})
        slept: list[float] = []
        with Server(app) as server:
            self.assertEqual(transport.call(server.base, "/api/index/bundles", TOKEN, sleep=slept.append)["bundles"], [])
            self.assertEqual(slept, [7, 7])
            with self.assertRaises(transport.Refusal) as caught:
                transport.call(server.base, "/api/index/bundles", TOKEN, sleep=slept.append)
            self.assertEqual((caught.exception.code, caught.exception.retry_after), ("rate_limit", 3600))
            self.assertEqual(slept, [7, 7])

    def test_a_redirect_is_never_followed(self):
        def app(handler, body):
            handler.answer(302, b"", {"Location": "https://elsewhere.example/api/index/bundles"})
        with Server(app) as server:
            with self.assertRaises(transport.Refusal) as caught:
                transport.call(server.base, "/api/index/bundles", TOKEN)
            self.assertEqual(caught.exception.status, 302)
            self.assertEqual(len(server.seen), 1)


class Downloads(unittest.TestCase):
    DATA = bytes(range(256)) * 1200  # 307200 bytes

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="contrib-transport-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = "/api/index/bundles/" + "a" * 64 + "/files/index/decl-index.jsonl"
        self.dest = self.tmp / "index" / "decl-index.jsonl"

    def fetch(self, base):
        return transport.download(base, self.path, TOKEN, self.dest, size=len(self.DATA), sha256=SHA(self.DATA))

    def test_a_cut_transfer_resumes_by_range_and_lands_whole(self):
        cuts = [100_000]

        def app(handler, body):
            ranged(handler, self.DATA, cut=cuts.pop(0) if cuts else None)
        with Server(app) as server, patch.object(transport, "RANGE_BYTES", 128 * 1024):
            self.fetch(server.base)
            self.assertEqual(self.dest.read_bytes(), self.DATA)
            self.assertFalse(self.dest.with_name(self.dest.name + ".part").exists())
            ranges = [headers["Range"] for _, _, headers in server.seen]
            self.assertEqual(ranges, ["bytes=0-131071", "bytes=100000-231071", "bytes=231072-307199"])
            self.assertTrue(all(headers["If-Range"] == f'"{SHA(self.DATA)}"' for _, _, headers in server.seen))
            # Already there: nothing is asked for again.
            self.fetch(server.base)
            self.assertEqual(len(server.seen), 3)

    def test_a_partial_file_from_an_earlier_run_is_continued(self):
        part = self.dest.with_name(self.dest.name + ".part")
        part.parent.mkdir(parents=True)
        part.write_bytes(self.DATA[:200_000])
        with Server(lambda handler, body: ranged(handler, self.DATA)) as server:
            self.fetch(server.base)
            self.assertEqual([headers["Range"] for _, _, headers in server.seen], ["bytes=200000-307199"])
        self.assertEqual(self.dest.read_bytes(), self.DATA)

    def test_a_whole_file_answer_starts_again_from_the_first_byte(self):
        part = self.dest.with_name(self.dest.name + ".part")
        part.parent.mkdir(parents=True)
        part.write_bytes(b"x" * 50_000)
        with Server(lambda handler, body: ranged(handler, self.DATA, ignore_range=True)) as server:
            self.fetch(server.base)
        self.assertEqual(self.dest.read_bytes(), self.DATA)

    def test_wrong_bytes_or_too_many_are_refused_and_nothing_lands(self):
        wrong = bytes(reversed(self.DATA))
        with Server(lambda handler, body: ranged(handler, wrong)) as server:
            with self.assertRaises(transport.TransportError) as caught:
                self.fetch(server.base)
            self.assertIn("sha256", str(caught.exception))
        with Server(lambda handler, body: ranged(handler, self.DATA + b"!", ignore_range=True)) as server:
            with self.assertRaises(transport.TransportError) as caught:
                self.fetch(server.base)
            self.assertIn("more than", str(caught.exception))
        self.assertFalse(self.dest.exists())
        self.assertFalse(self.dest.with_name(self.dest.name + ".part").exists())

    def test_a_refusal_keeps_what_was_downloaded(self):
        cuts = [100_000]

        def app(handler, body):
            if cuts:
                ranged(handler, self.DATA, cut=cuts.pop(0))
            else:
                handler.json(429, {"ok": False, "code": "quota_bytes", "error": "today's downloads reached the quota"},
                             {"Retry-After": "40000"})
        with Server(app) as server:
            with self.assertRaises(transport.Refusal) as caught:
                self.fetch(server.base)
        self.assertEqual(caught.exception.code, "quota_bytes")
        self.assertEqual(self.dest.with_name(self.dest.name + ".part").stat().st_size, 100_000)


def bundle(files: dict[str, bytes], **overrides) -> tuple[bytes, str]:
    manifest = {"schema": transport.BUNDLE_SCHEMA, "bundle": "u-test/v1",
                "files": {rel: {"sha256": SHA(data), "bytes": len(data)} for rel, data in files.items()},
                "licences": {"use_restricted": ["atlas"]}, **overrides}
    raw = json.dumps(manifest).encode()
    return raw, SHA(raw)


class Bundles(unittest.TestCase):
    FILES = {"index/decl-index.jsonl": b'{"declaration":"Alpha.foo"}\n', "search/declarations.tsv": b"alpha\tAlpha.foo\n",
             "names/all.txt": b"Alpha.foo\n", "LICENCES.md": b"# Licences\n"}

    def serve(self, raw: bytes, sha: str, files: dict[str, bytes]):
        prefix = f"/api/index/bundles/{sha}"

        def app(handler, body):
            if handler.path == prefix:
                handler.answer(200, raw, {"Content-Type": "application/json"})
            elif handler.path.startswith(prefix + "/files/") and handler.path[len(prefix) + 7:] in files:
                ranged(handler, files[handler.path[len(prefix) + 7:]])
            else:
                handler.json(404, {"ok": False, "code": "not_found", "error": "no such file"})
        return Server(app)

    def test_a_dry_run_reads_the_manifest_only_and_execute_fills_the_cache(self):
        raw, sha = bundle(self.FILES)
        with tempfile.TemporaryDirectory() as tmp, self.serve(raw, sha, self.FILES) as server:
            preview = transport.fetch_bundle(server.base, TOKEN, sha, Path(tmp), skip=("names/",))
            self.assertEqual((preview["files"], preview["missing"], preview["skipped"], preview["use_restricted"]),
                             (3, 3, 1, ["atlas"]))
            self.assertEqual([path for _, path, _ in server.seen], [f"/api/index/bundles/{sha}"])
            done = transport.fetch_bundle(server.base, TOKEN, sha, Path(tmp), skip=("names/",), execute=True, log=lambda line: None)
            self.assertEqual((done["downloaded"], done["missing"]), (3, 0))
            root = Path(tmp) / sha
            for rel, data in self.FILES.items():
                if rel.startswith("names/"):
                    self.assertFalse((root / rel).exists())
                else:
                    self.assertEqual((root / rel).read_bytes(), data)
            self.assertEqual((root / "MANIFEST.json").read_bytes(), raw)
            # Run again: only the manifest is read.
            requests = len(server.seen)
            again = transport.fetch_bundle(server.base, TOKEN, sha, Path(tmp), skip=("names/",), execute=True, log=lambda line: None)
            self.assertEqual((again["downloaded"], len(server.seen) - requests), (0, 1))

    def test_a_manifest_that_is_not_the_one_named_or_lists_an_odd_path_is_refused(self):
        raw, sha = bundle(self.FILES)
        with tempfile.TemporaryDirectory() as tmp, self.serve(raw, sha, self.FILES) as server:
            with self.assertRaises(transport.TransportError):
                transport.fetch_bundle(server.base, TOKEN, "b" * 64 if sha != "b" * 64 else "c" * 64, Path(tmp))
        for odd in ({"../x": b"?"}, {"a b": b"?"}, {"MANIFEST.json": b"?"}):
            raw, sha = bundle({**self.FILES, **odd})
            with self.assertRaises(transport.TransportError, msg=str(odd)):
                transport.read_manifest(raw, sha)
        raw, sha = bundle(self.FILES, schema="wikilean.pipeline.index-bundle/1")
        with self.assertRaises(transport.TransportError):
            transport.read_manifest(raw, sha)
        raw, _ = bundle(self.FILES)
        with self.assertRaises(transport.TransportError):
            transport.read_manifest(raw, "d" * 64)

    def test_the_command_line_reads_the_token_from_the_environment(self):
        raw, sha = bundle(self.FILES)
        with tempfile.TemporaryDirectory() as tmp, self.serve(raw, sha, self.FILES) as server:
            out, err = io.StringIO(), io.StringIO()
            with patch.dict(os.environ, {transport.TOKEN_ENV: TOKEN}), contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(err):
                code = transport.main(["--base-url", server.base, "fetch-bundle", sha, "--cache", tmp, "--execute"])
            self.assertEqual(code, 0, err.getvalue())
            self.assertEqual(json.loads(out.getvalue())["downloaded"], len(self.FILES))
            self.assertNotIn(TOKEN, out.getvalue() + err.getvalue())
            with patch.dict(os.environ, {transport.TOKEN_ENV: ""}), contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(transport.main(["--base-url", server.base, "bundles"]), 1)


if __name__ == "__main__":
    unittest.main()
