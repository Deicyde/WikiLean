#!/usr/bin/env python3
"""Hermetic HTTP, read-only and escaping tests for the local companion viewer."""
from __future__ import annotations

import hashlib
import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from brain import companion_server as viewer
from brain.ingest import companion


def docs():
    return [
        {"id": "one", "source": {"type": "pdf", "uri": "https://example.org/book.pdf"},
         "location": {"page": 2}, "target": "https://example.org/formal#one"},
        {"id": "two", "source": {"type": "latex", "uri": "../book.tex"},
         "location": {"label": "thm:two"}, "target": "urn:lean:Example.two",
         "extensions": {"opaque": {"large": 2**100}}},
        {"id": "three", "source": {"type": "generic", "uri": "//[oops"},
         "location": {"custom": [3, 2, 1]}, "target": "file:result.html"},
    ]


def build_index(output, values=None):
    values = docs() if values is None else values
    shard = output.parent.resolve() / "shards" / "links.json"
    captured = SimpleNamespace(records=[SimpleNamespace(annotation=value, path=shard, pointer=f"/{i}", line=None)
                                       for i, value in enumerate(values)], files=[shard], problems=[])
    # This suite tests the real SQLite/browser boundary, with validation covered
    # by test_companion_ingest.py and its separately required reference suite.
    with patch.object(companion, "_validate_captured", side_effect=lambda value: value):
        return companion.import_collected(captured, source_key="viewer-fixture", output=output)


class ViewerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.index = self.root / "index"
        self.import_report = build_index(self.index)
        self.current = self.root / "current"
        self.current.symlink_to(self.index, target_is_directory=True)
        self.server = viewer.create_server(self.current, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        self.port = self.server.server_address[1]

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, path="/api/annotations", *, method="GET", headers=None, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request(method, path, headers=headers or {}, body=body)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read().decode()
        finally:
            connection.close()

    def test_binds_only_ipv4_loopback(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")

    def test_report_and_real_original_records(self):
        code, _, body = self.request("/api/report")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["annotations"], 3)
        code, _, body = self.request()
        page = json.loads(body)
        self.assertEqual(code, 200)
        self.assertEqual([row["annotation"] for row in page["records"]], docs())
        self.assertEqual(page["records"][2]["resolution"]["source"]["status"], "unresolved")
        self.assertEqual(page["records"][0]["origin"]["pointer"], "/0")
        self.assertIn(str(2**100), body)

    def test_page_boundaries_and_exact_endpoint_filters(self):
        code, _, body = self.request("/api/annotations?limit=1&offset=1")
        page = json.loads(body)
        self.assertEqual(code, 200)
        self.assertEqual(page["total"], 3)
        self.assertEqual(page["next_offset"], 2)
        self.assertEqual(page["records"][0]["annotation"]["id"], "two")
        target = "https://example.org/formal#one"
        code, _, body = self.request("/api/annotations?" + urlencode({"target": target}))
        self.assertEqual(json.loads(body)["total"], 1)
        row = json.loads(body)["records"][0]
        code, _, body = self.request("/api/annotations?" + urlencode({"source": row["source"], "target": "urn:missing"}))
        self.assertEqual(json.loads(body)["records"], [])

    def test_search_literal_characters_and_parameterized_sql(self):
        code, _, body = self.request("/api/annotations?q=EXAMPLE")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["total"], 2)
        _, _, all_body = self.request()
        all_rows = json.loads(all_body)["records"]
        for value in ("%", "_", "' OR 1=1 --"):
            code, _, body = self.request("/api/annotations?" + urlencode({"q": value}))
            self.assertEqual(code, 200)
            expected = [row for row in all_rows if value in row["source"] or value in row["target"]]
            self.assertEqual(json.loads(body)["records"], expected)
        code, _, body = self.request("/api/annotations?" + urlencode({"target": "' OR 1=1 --"}))
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["records"], [])

    def test_pagination_query_limits(self):
        for query in ("limit=0", "limit=101", "offset=-1", "offset=1000001", "limit=x", "offset=1.5",
                      "unknown=1", "limit=1&limit=2", "q=" + "x" * 2049):
            with self.subTest(query=query[:60]):
                code, _, _ = self.request("/api/annotations?" + query)
                self.assertEqual(code, 400)

    def test_nonlocal_hosts_and_cross_origins_rejected(self):
        for headers in ({"Host": "example.org"}, {"Host": f"evil.example:{self.port}"},
                        {"Origin": "https://example.org"}, {"Origin": "null"},
                        {"Origin": f"http://localhost:{self.port}"}, {"Sec-Fetch-Site": "cross-site"}):
            with self.subTest(headers=headers):
                code, _, _ = self.request(headers=headers)
                self.assertEqual(code, 403)
        for host in (f"127.0.0.1:{self.port}", f"localhost:{self.port}"):
            code, _, _ = self.request(headers={"Host": host, "Origin": "http://" + host})
            self.assertEqual(code, 200)

    def test_duplicate_host_or_origin_rejected(self):
        for key, value in (("Host", f"127.0.0.1:{self.port}"), ("Origin", f"http://127.0.0.1:{self.port}")):
            connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            try:
                connection.putrequest("GET", "/api/annotations", skip_host=key == "Host")
                connection.putheader(key, value)
                connection.putheader(key, value)
                connection.endheaders()
                response = connection.getresponse()
                self.assertEqual(response.status, 403)
                response.read()
            finally:
                connection.close()

    def test_read_only_methods_and_database_unchanged(self):
        database = self.index / "brain.sqlite3"
        before = hashlib.sha256(database.read_bytes()).digest()
        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
            code, headers, _ = self.request(method=method, body="arbitrary write")
            self.assertEqual(code, 405)
            self.assertEqual(headers["Allow"], "GET, HEAD")
        code, _, _ = self.request("/api/report")
        self.assertEqual(code, 200)
        self.assertEqual(hashlib.sha256(database.read_bytes()).digest(), before)
        self.assertEqual(sorted(path.name for path in self.index.iterdir()),
                         ["brain.sqlite3", "edges.jsonl", "edges_links.jsonl", "nodes.jsonl"])

    def test_no_arbitrary_filesystem_or_remote_fetch_endpoints(self):
        for path in ("/etc/passwd", "/assets/../../AGENTS.md", "/api/annotations/../../x", "/source?uri=file:///etc/passwd"):
            code, _, _ = self.request(path)
            self.assertEqual(code, 404)
        code, _, _ = self.request("http://example.org/api/annotations")
        self.assertEqual(code, 403)
        code, _, _ = self.request("http://example.org/api/annotations", headers={"Host": f"127.0.0.1:{self.port}"})
        self.assertEqual(code, 400)

    def test_security_headers_and_head(self):
        code, headers, body = self.request("/", method="HEAD")
        self.assertEqual(code, 200)
        self.assertEqual(body, "")
        self.assertGreater(int(headers["Content-Length"]), 0)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_html_location_origin_full_annotation_and_pagination(self):
        code, headers, body = self.request("/?limit=1")
        self.assertEqual(code, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn("Companion links", body)
        self.assertIn("Location in the source", body)
        self.assertIn("Full annotation", body)
        self.assertIn("Stored in file:", body)
        self.assertIn('target="_blank" rel="noopener noreferrer"', body)
        self.assertIn("generation=", body)
        self.assertIn("offset=1", body)
        code, _, body = self.request("/?q=missing")
        self.assertEqual(code, 200)
        self.assertIn("No matching links", body)

    def test_hostile_annotation_html_is_escaped_and_only_web_links_open(self):
        hostile = docs()[0]
        hostile["id"] = '</h2><script>alert("x")</script>'
        hostile["source"]["type"] = "generic"
        hostile["source"]["uri"] = "javascript:alert(1)"
        hostile["target"] = "data:text/html,<script>alert(1)</script>"
        hostile["location"] = {"custom": '</pre><img src=x onerror="alert(1)">'}
        unsafe_index = self.root / "unsafe"
        build_index(unsafe_index, [hostile])
        self.server.data_dir = unsafe_index
        code, _, body = self.request("/")
        self.assertEqual(code, 200)
        self.assertNotIn("<script>", body)
        self.assertNotIn("<img ", body)
        self.assertNotIn('href="javascript:', body)
        self.assertNotIn('href="data:', body)
        self.assertNotIn('href="file:', body)
        self.assertIn("&lt;script&gt;", body)
        self.assertIn("&lt;/pre&gt;", body)

    def test_generation_bound_pages_conflict_after_current_advances(self):
        generation = self.import_report["generation"]
        replacement = self.root / "replacement"
        changed = docs()
        changed[0]["id"] = "new generation"
        build_index(replacement, changed)
        next_link = self.root / "next"
        next_link.symlink_to(replacement, target_is_directory=True)
        os.replace(next_link, self.current)
        query = urlencode({"generation": generation, "limit": 1, "offset": 1})
        code, _, _ = self.request("/api/annotations?" + query)
        self.assertEqual(code, 409)
        code, _, _ = self.request("/api/report?" + query)
        self.assertEqual(code, 409)
        code, _, body = self.request("/?" + query)
        self.assertEqual(code, 409)
        self.assertIn("Refresh results", body)
        code, _, body = self.request()
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["records"][0]["annotation"]["id"], "new generation")

    def test_workspace_confinement_is_checked_after_each_pointer_change(self):
        self.server.generation_root = self.root.resolve()
        code, _, _ = self.request()
        self.assertEqual(code, 200)
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        replacement = Path(outside.name) / "outside-index"
        build_index(replacement)
        next_link = self.root / "next"
        next_link.symlink_to(replacement, target_is_directory=True)
        os.replace(next_link, self.current)
        code, _, body = self.request()
        self.assertEqual(code, 503)
        self.assertIn("outside the workspace", body)

    def test_request_pins_resolved_directory_during_pointer_change(self):
        replacement = self.root / "replacement"
        changed = docs()
        changed[0]["id"] = "new generation"
        build_index(replacement, changed)
        open_store = viewer.store.open_store
        seen = []
        def advance(**kwargs):
            seen.append(kwargs["data_dir"])
            next_link = self.root / "next"
            next_link.symlink_to(replacement, target_is_directory=True)
            os.replace(next_link, self.current)
            return open_store(**kwargs)
        with patch.object(viewer.store, "open_store", side_effect=advance):
            code, _, body = self.request()
        self.assertEqual(code, 200)
        self.assertEqual(seen, [self.index.resolve()])
        self.assertEqual(json.loads(body)["records"][0]["annotation"]["id"], "one")

    def test_stale_index_returns_unavailable_without_rebuild(self):
        with (self.index / "edges_links.jsonl").open("a") as handle:
            handle.write("\n")
        code, _, body = self.request()
        self.assertEqual(code, 503)
        self.assertIn("stale", body)

    def test_missing_or_wrong_profile_fails_closed(self):
        self.server.data_dir = self.root / "missing"
        code, _, _ = self.request()
        self.assertEqual(code, 503)
        self.server.data_dir = self.index
        with patch.object(viewer.store.SQLiteStore, "metadata", return_value={"profile": "other"}):
            code, _, body = self.request()
        self.assertEqual(code, 503)
        self.assertIn("local companion", body)

    def test_stylesheet_is_local_and_fixed(self):
        code, headers, body = self.request("/assets/viewer.css")
        self.assertEqual(code, 200)
        self.assertIn("text/css", headers["Content-Type"])
        self.assertEqual(body, viewer.CSS)
        self.assertNotIn("url(", body)


if __name__ == "__main__":
    unittest.main()
