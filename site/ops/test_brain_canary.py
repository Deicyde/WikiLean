#!/usr/bin/env python3
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("brain_canary", HERE / "brain-canary.py")
assert SPEC and SPEC.loader
brain_canary = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = brain_canary
SPEC.loader.exec_module(brain_canary)

BASE = "https://example.test"


def make_selector(release: str, previous: str | None = None) -> dict[str, object]:
    value: dict[str, object] = {
        "schema": "wikilean.release-selector/v1",
        "release_id": f"sha256:{release}",
        "release": release,
        "manifest": f"/assets/brain/releases/{release}/release.json",
        **({
            "previous_release_id": f"sha256:{previous}",
            "previous_release": previous,
            "previous_manifest": f"/assets/brain/releases/{previous}/release.json",
        } if previous is not None else {}),
        "audited_at": "2026-01-01T00:00:00Z",
    }
    return value


class FakeResponse:
    def __init__(self, body: bytes, content_type: str = "application/json", status: int = 200):
        self._body = body
        self.status = status
        self.headers = {"Content-Type": content_type}

    def read(self, size: int = -1) -> bytes:
        return self._body if size < 0 else self._body[:size]

    def getcode(self) -> int:
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class Fixture:
    def __init__(self):
        cells = {"scheme": {"min_len": 2, "max_len": 8, "pad": "_"}, "shards": {"aa": 1}, "prov": []}
        shard = {"cell:Q1": {"cell": {"id": "cell:Q1"}}}
        aliases = {"organs": {"Q1": "cell:Q1"}}
        labels = [{"id": "cell:Q1", "label": "One"}]
        supercells = {"supercells": [{"id": "supercell:one"}]}
        explorer = {"roots": ["cell:Q1"]}
        frontier_graph = {"nodes": [{"id": "cell:Q1"}], "edges": []}
        sources = [{"id": "mathlib"}]
        xref = {"Q1": ["xref:nlab:one"]}
        page = b'<html><script>fetch("/assets/brain/current.json")</script></html>'
        artifact_values = {
            "site/assets/brain/cells/manifest.json": cells,
            "site/assets/brain/cells/aa.json": shard,
            "site/assets/brain/cells/aliases.json": aliases,
            "site/assets/brain/cells/labels.json": labels,
            "site/assets/brain/cells/supercells.json": supercells,
            "site/assets/brain/cells/explorer.json": explorer,
            "site/assets/brain/cells/frontier_graph.json": frontier_graph,
            "site/assets/brain/sources.json": sources,
            "site/assets/brain/xref_index.json": xref,
        }
        artifact_bytes = {
            path: json.dumps(value, sort_keys=True).encode()
            for path, value in artifact_values.items()
        }
        artifact_bytes["site/out/brain.html"] = page
        identity_value = {
            "schema": "wikilean.release/v1",
            "profile": "brain-current-v1",
            "artifacts": [
                {"path": path, "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()}
                for path, body in artifact_bytes.items()
            ],
        }
        payload = b"wikilean\0wikilean.release.v1\0canonical-json-v1\0" + json.dumps(
            identity_value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode()
        self.release_id = "sha256:" + hashlib.sha256(payload).hexdigest()
        self.release = self.release_id.removeprefix("sha256:")
        immutable = f"/assets/brain/releases/{self.release}"
        selector = make_selector(self.release)
        manifest = {**identity_value, "release_id": self.release_id, "attestations": []}
        shard_bytes = artifact_bytes["site/assets/brain/cells/aa.json"]
        cursor_one = base64.b64encode(
            json.dumps({
                "v": 2,
                "r": self.release_id,
                "q": '{"f":0,"type":"cell","under":""}',
                "i": 1,
            }, separators=(",", ":")).encode()
        ).decode()
        api1 = {
            "ok": True,
            "release_id": self.release_id,
            "snapshot": {"generated_at": "2026-01-01", "pin": "abc"},
            "hits": [{"id": "cell:Q1"}],
            "next_cursor": cursor_one,
        }
        api2 = {
            "ok": True,
            "release_id": self.release_id,
            "snapshot": {"generated_at": "2026-01-01", "pin": "abc"},
            "hits": [{"id": "cell:Q2"}],
            "next_cursor": "cursor-two",
        }
        self.routes: dict[str, FakeResponse] = {}
        self.add_json("/assets/brain/current.json", selector)
        self.add_json(f"{immutable}/release.json", manifest)
        self.add_json(f"{immutable}/cells/manifest.json", cells)
        self.routes[f"{immutable}/cells/aa.json"] = FakeResponse(shard_bytes)
        self.add_json(f"{immutable}/cells/aliases.json", aliases)
        self.add_json(f"{immutable}/cells/labels.json", labels)
        self.add_json(f"{immutable}/cells/supercells.json", supercells)
        self.add_json(f"{immutable}/cells/explorer.json", explorer)
        self.add_json(f"{immutable}/cells/frontier_graph.json", frontier_graph)
        self.add_json(f"{immutable}/sources.json", sources)
        self.add_json(f"{immutable}/xref_index.json", xref)
        self.add_json("/assets/brain/cells/manifest.json", cells)
        self.add_json("/assets/brain/cells/aa.json", shard)
        self.add_json("/assets/brain/cells/aliases.json", aliases)
        self.add_json("/assets/brain/cells/labels.json", labels)
        self.add_json("/assets/brain/cells/supercells.json", supercells)
        self.add_json("/assets/brain/cells/explorer.json", explorer)
        self.add_json("/assets/brain/cells/frontier_graph.json", frontier_graph)
        self.add_json("/assets/brain/sources.json", sources)
        self.add_json("/assets/brain/xref_index.json", xref)
        self.routes["/brain"] = FakeResponse(page, "text/html; charset=utf-8")
        self.routes["/brain.html"] = FakeResponse(page, "text/html; charset=utf-8")
        self.add_json("/api/brain/filter?f=0&limit=1", api1)
        self.add_json(f"/api/brain/filter?f=0&limit=1&cursor={cursor_one}", api2)
        self.add_json(
            "/mcp",
            {
                "jsonrpc": "2.0",
                "id": "brain-release-canary",
                "result": {
                    "content": [{"type": "text", "text": json.dumps(api1)}],
                },
            },
        )
        self.requested: list[str] = []
        self.requested_methods: list[str] = []

    def add_json(self, path: str, value: object) -> None:
        self.routes[path] = FakeResponse(json.dumps(value, sort_keys=True).encode())

    def open(self, request, timeout: float):
        del timeout
        parts = urlsplit(request.full_url)
        query = parse_qs(parts.query)
        self.assert_cache_bust(query)
        query.pop("__brain_canary", None)
        pairs = []
        for key, values in query.items():
            for value in values:
                pairs.append((key, value))
        suffix = "&".join(f"{key}={value}" for key, value in pairs)
        path = parts.path + ("?" + suffix if suffix else "")
        self.requested.append(path)
        self.requested_methods.append(request.get_method())
        try:
            return self.routes[path]
        except KeyError as exc:
            raise AssertionError(f"unexpected request: {path}") from exc

    @staticmethod
    def assert_cache_bust(query: dict[str, list[str]]) -> None:
        if query.get("__brain_canary") != ["test-nonce"]:
            raise AssertionError("request missing deterministic cache buster")


class BrainCanaryTest(unittest.TestCase):
    def canary(self, fixture: Fixture):
        return brain_canary.BrainCanary(BASE, fixture.release_id, opener=fixture.open, nonce=lambda: "test-nonce")

    def test_complete_release_surface_passes(self):
        fixture = Fixture()
        result = self.canary(fixture).check_once()
        self.assertEqual(result["schema"], "wikilean.brain-canary-result/v1")
        self.assertTrue(result["ok"])
        self.assertEqual(result["release_id"], fixture.release_id)
        self.assertEqual(result["shard"], "cells/aa.json")
        self.assertEqual(result["pages_checked"], ["/brain", "/brain.html"])
        self.assertEqual(result["mcp_tool"], "brain_filter")
        self.assertIn("cells/aliases.json", result["aliases_checked"])
        self.assertIn("cells/supercells.json", result["aliases_checked"])
        self.assertIn("cells/explorer.json", result["aliases_checked"])
        self.assertIn("cells/frontier_graph.json", result["aliases_checked"])
        self.assertIn("sources.json", result["aliases_checked"])
        self.assertIn("xref_index.json", result["aliases_checked"])
        self.assertGreater(result["requests"], 0)
        self.assertGreater(result["response_bytes"], 0)
        self.assertGreaterEqual(result["check_duration_ms"], 0)
        self.assertGreater(result["max_rss_bytes"], 0)
        self.assertIn("POST", fixture.requested_methods)

    def test_selector_mismatch_fails(self):
        fixture = Fixture()
        fixture.add_json("/assets/brain/current.json", make_selector("b" * 64))
        with self.assertRaisesRegex(brain_canary.CanaryError, "selector release mismatch"):
            self.canary(fixture).check_once()

    def test_flat_previous_release_selector_passes(self):
        fixture = Fixture()
        fixture.add_json("/assets/brain/current.json", make_selector(fixture.release, "b" * 64))
        self.assertTrue(self.canary(fixture).check_once()["ok"])

    def test_partial_previous_release_selector_fails(self):
        fixture = Fixture()
        selector = make_selector(fixture.release)
        selector["previous_release_id"] = "sha256:" + "b" * 64
        fixture.add_json("/assets/brain/current.json", selector)
        with self.assertRaisesRegex(brain_canary.CanaryError, "supplied together"):
            self.canary(fixture).check_once()

    def test_manifest_identity_mismatch_fails(self):
        fixture = Fixture()
        fixture.add_json(
            f"/assets/brain/releases/{fixture.release}/release.json",
            {
                "schema": "wikilean.release/v1",
                "profile": "brain-current-v1",
                "release_id": "sha256:" + "b" * 64,
                "artifacts": [],
                "attestations": [],
            },
        )
        with self.assertRaisesRegex(brain_canary.CanaryError, "manifest identity"):
            self.canary(fixture).check_once()

    def test_release_manifest_self_identity_mismatch_fails(self):
        fixture = Fixture()
        manifest_path = f"/assets/brain/releases/{fixture.release}/release.json"
        manifest = json.loads(fixture.routes[manifest_path]._body)
        manifest["artifacts"][0]["sha256"] = "0" * 64
        fixture.add_json(manifest_path, manifest)
        with self.assertRaisesRegex(brain_canary.CanaryError, "self-identity mismatch"):
            self.canary(fixture).check_once()

    def test_alias_byte_mismatch_fails(self):
        fixture = Fixture()
        fixture.routes["/assets/brain/sources.json"] = FakeResponse(b"[]")
        with self.assertRaisesRegex(brain_canary.CanaryError, "sources.json"):
            self.canary(fixture).check_once()

    def test_page_must_match_frozen_release_bytes(self):
        fixture = Fixture()
        fixture.routes["/brain.html"] = FakeResponse(
            b'<html><script>fetch("/assets/brain/current.json")</script><p>stale</p></html>',
            "text/html; charset=utf-8",
        )
        with self.assertRaisesRegex(brain_canary.CanaryError, "/brain.html"):
            self.canary(fixture).check_once()

    def test_actual_brain_route_must_match_frozen_release_bytes(self):
        fixture = Fixture()
        fixture.routes["/brain"] = FakeResponse(
            b'<html><script>fetch("/assets/brain/current.json")</script><p>stale</p></html>',
            "text/html; charset=utf-8",
        )
        with self.assertRaisesRegex(brain_canary.CanaryError, "/brain"):
            self.canary(fixture).check_once()

    def test_missing_required_view_asset_fails(self):
        fixture = Fixture()
        immutable = f"/assets/brain/releases/{fixture.release}"
        fixture.routes[f"{immutable}/cells/supercells.json"] = FakeResponse(b"{}", status=404)
        with self.assertRaisesRegex(brain_canary.CanaryError, "supercells.json.*HTTP 404"):
            self.canary(fixture).check_once()

    def test_api_must_emit_cursor_for_expected_release(self):
        fixture = Fixture()
        fixture.add_json(
            "/api/brain/filter?f=0&limit=1",
            {
                "ok": True,
                "release_id": fixture.release_id,
                "snapshot": {},
                "hits": [{"id": "cell:Q1"}],
                "next_cursor": None,
            },
        )
        with self.assertRaisesRegex(brain_canary.CanaryError, "opaque cursor"):
            self.canary(fixture).check_once()

    def test_mcp_tool_must_be_bound_to_expected_release(self):
        fixture = Fixture()
        fixture.add_json(
            "/mcp",
            {
                "jsonrpc": "2.0",
                "id": "brain-release-canary",
                "result": {
                    "content": [{
                        "type": "text",
                        "text": json.dumps({
                            "ok": True,
                            "release_id": "sha256:" + "b" * 64,
                        }),
                    }],
                },
            },
        )
        with self.assertRaisesRegex(brain_canary.CanaryError, "MCP tool response"):
            self.canary(fixture).check_once()

    def test_mcp_response_must_have_expected_jsonrpc_envelope(self):
        fixture = Fixture()
        mcp = json.loads(fixture.routes["/mcp"]._body)
        mcp["id"] = "another-request"
        fixture.add_json("/mcp", mcp)
        with self.assertRaisesRegex(brain_canary.CanaryError, "JSON-RPC envelope"):
            self.canary(fixture).check_once()

    def test_mcp_tool_error_fails(self):
        fixture = Fixture()
        mcp = json.loads(fixture.routes["/mcp"]._body)
        mcp["result"]["isError"] = True
        fixture.add_json("/mcp", mcp)
        with self.assertRaisesRegex(brain_canary.CanaryError, "MCP tool reported an error"):
            self.canary(fixture).check_once()

    def test_cursor_must_be_bound_to_expected_release(self):
        fixture = Fixture()
        wrong_cursor = base64.b64encode(
            json.dumps({
                "v": 2,
                "r": "sha256:" + "b" * 64,
                "q": '{"f":0,"type":"cell","under":""}',
                "i": 1,
            }, separators=(",", ":")).encode()
        ).decode()
        fixture.add_json(
            "/api/brain/filter?f=0&limit=1",
            {
                "ok": True,
                "release_id": fixture.release_id,
                "snapshot": {},
                "hits": [{"id": "cell:Q1"}],
                "next_cursor": wrong_cursor,
            },
        )
        with self.assertRaisesRegex(brain_canary.CanaryError, "bound to the expected release and query"):
            self.canary(fixture).check_once()

    def test_invalid_expected_release_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "expected release id"):
            brain_canary.BrainCanary(BASE, "not-a-release")

    def test_response_size_is_bounded(self):
        fixture = Fixture()
        canary = brain_canary.BrainCanary(
            BASE,
            fixture.release_id,
            max_response_bytes=32,
            opener=fixture.open,
            nonce=lambda: "test-nonce",
        )
        with self.assertRaisesRegex(brain_canary.CanaryError, "response limit"):
            canary.check_once()


if __name__ == "__main__":
    unittest.main()
