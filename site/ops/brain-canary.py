#!/usr/bin/env python3
"""Verify that a deployed Brain release has converged across public surfaces."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import resource
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable

RELEASE_ID_RE = re.compile(r"^sha256:([0-9a-f]{64})$")
SELECTOR_SCHEMA = "wikilean.release-selector/v1"
RELEASE_SCHEMA = "wikilean.release/v1"
SELECTOR_KEYS = {
    "schema",
    "release_id",
    "release",
    "manifest",
    "previous_release_id",
    "previous_release",
    "previous_manifest",
    "audited_at",
}
DEFAULT_MAX_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_CURSOR_BYTES = 4096


class CanaryError(RuntimeError):
    """A retryable release-convergence failure."""


@dataclass(frozen=True)
class HttpResult:
    body: bytes
    content_type: str
    status: int


class BrainCanary:
    def __init__(
        self,
        base_url: str,
        expected_release_id: str,
        *,
        request_timeout: float = 20.0,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        opener: Callable[..., object] = urllib.request.urlopen,
        nonce: Callable[[], str] | None = None,
    ) -> None:
        match = RELEASE_ID_RE.fullmatch(expected_release_id)
        if not match:
            raise ValueError("expected release id must be sha256:<64 lowercase hex>")
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base URL must be an absolute http(s) URL")
        if request_timeout <= 0:
            raise ValueError("request timeout must be positive")
        if isinstance(max_response_bytes, bool) or max_response_bytes <= 0:
            raise ValueError("maximum response bytes must be positive")
        self.base_url = base_url.rstrip("/")
        self.expected_release_id = expected_release_id
        self.release = match.group(1)
        self.request_timeout = request_timeout
        self.max_response_bytes = max_response_bytes
        self.opener = opener
        self.nonce = nonce or (lambda: str(time.time_ns()))
        self.request_count = 0
        self.response_bytes = 0

    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            raise CanaryError(f"refusing non-absolute public path: {path!r}")
        parts = urllib.parse.urlsplit(self.base_url + path)
        query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        query.append(("__brain_canary", self.nonce()))
        return urllib.parse.urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(query), parts.fragment)
        )

    def fetch(self, path: str, *, body: bytes | None = None) -> HttpResult:
        method = "POST" if body is not None else "GET"
        headers = {
            "Accept": "application/json,text/html;q=0.9,*/*;q=0.1",
            "Cache-Control": "no-cache",
            "User-Agent": "WikiLean-Brain-Canary/1",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self._url(path),
            data=body,
            headers=headers,
            method=method,
        )
        try:
            response = self.opener(request, timeout=self.request_timeout)
            with response:
                status = int(getattr(response, "status", response.getcode()))
                body = response.read(self.max_response_bytes + 1)
                content_type = response.headers.get("Content-Type", "")
        except (OSError, urllib.error.URLError) as exc:
            raise CanaryError(f"{method} {path} failed: {exc}") from exc
        if status != 200:
            raise CanaryError(f"{method} {path} returned HTTP {status}")
        if len(body) > self.max_response_bytes:
            raise CanaryError(
                f"{method} {path} exceeded the {self.max_response_bytes} byte response limit"
            )
        if not body:
            raise CanaryError(f"{method} {path} returned an empty body")
        self.request_count += 1
        self.response_bytes += len(body)
        return HttpResult(body=body, content_type=content_type, status=status)

    def fetch_json(self, path: str) -> tuple[object, bytes]:
        result = self.fetch(path)
        try:
            return json.loads(result.body), result.body
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CanaryError(f"GET {path} did not return valid JSON: {exc}") from exc

    def post_json(self, path: str, value: object) -> tuple[object, bytes]:
        body = self._canonical_json(value)
        result = self.fetch(path, body=body)
        try:
            return json.loads(result.body), result.body
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CanaryError(f"POST {path} did not return valid JSON: {exc}") from exc

    @staticmethod
    def _object(value: object, label: str) -> dict[str, object]:
        if not isinstance(value, dict):
            raise CanaryError(f"{label} must be a JSON object")
        return value

    @staticmethod
    def _canonical_json(value: object) -> bytes:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")

    def _verify_filter_cursor(self, cursor: str) -> None:
        if len(cursor.encode("utf-8")) > MAX_CURSOR_BYTES:
            raise CanaryError("Brain API emitted an oversized opaque cursor")
        try:
            padded = cursor + "=" * (-len(cursor) % 4)
            raw = base64.b64decode(padded, validate=True)
            value = json.loads(raw)
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CanaryError("Brain API emitted a malformed opaque cursor") from exc
        if (
            not isinstance(value, dict)
            or value.get("v") != 2
            or value.get("r") != self.expected_release_id
            or value.get("q") != '{"f":0,"type":"cell","under":""}'
            or not isinstance(value.get("i"), int)
            or value["i"] < 0
        ):
            raise CanaryError("Brain API cursor is not versioned and bound to the expected release and query")

    @staticmethod
    def _verify_artifact(
        artifacts: dict[str, tuple[int, str]],
        release_path: str,
        public_path: str,
        body: bytes,
    ) -> None:
        declared = artifacts.get(release_path)
        if declared is None:
            raise CanaryError(f"release manifest does not declare {release_path}")
        declared_size, declared_digest = declared
        if len(body) != declared_size or hashlib.sha256(body).hexdigest() != declared_digest:
            raise CanaryError(f"immutable asset does not match release manifest: {public_path}")

    def check_once(self) -> dict[str, object]:
        started = time.monotonic()
        self.request_count = 0
        self.response_bytes = 0
        selector_raw, _ = self.fetch_json("/assets/brain/current.json")
        selector = self._object(selector_raw, "selector")
        expected_manifest = f"/assets/brain/releases/{self.release}/release.json"
        unknown_selector_keys = sorted(set(selector) - SELECTOR_KEYS)
        if unknown_selector_keys:
            raise CanaryError(f"selector has unknown fields: {unknown_selector_keys}")
        if selector.get("schema") != SELECTOR_SCHEMA:
            raise CanaryError("selector schema mismatch")
        if selector.get("release_id") != self.expected_release_id:
            raise CanaryError(
                f"selector release mismatch: expected {self.expected_release_id}, got {selector.get('release_id')!r}"
            )
        if selector.get("release") != self.release:
            raise CanaryError("selector release hex does not match release_id")
        if selector.get("manifest") != expected_manifest:
            raise CanaryError("selector manifest path does not match the immutable release")
        previous_keys = ("previous_release_id", "previous_release", "previous_manifest")
        present_previous = [key for key in previous_keys if key in selector]
        has_previous = bool(present_previous)
        if has_previous:
            if len(present_previous) != len(previous_keys):
                raise CanaryError("selector previous release fields must be supplied together")
            previous_match = RELEASE_ID_RE.fullmatch(str(selector.get("previous_release_id", "")))
            if (
                previous_match is None
                or previous_match.group(1) != selector.get("previous_release")
                or selector.get("previous_manifest")
                != f"/assets/brain/releases/{selector.get('previous_release')}/release.json"
                or selector.get("previous_release_id") == self.expected_release_id
            ):
                raise CanaryError("selector previous release is inconsistent")
        audited_at = selector.get("audited_at")
        if audited_at is not None and (not isinstance(audited_at, str) or not audited_at):
            raise CanaryError("selector audited_at must be a non-empty string")

        release_raw, _ = self.fetch_json(expected_manifest)
        release_manifest = self._object(release_raw, "release manifest")
        if release_manifest.get("schema") != RELEASE_SCHEMA:
            raise CanaryError("release manifest schema mismatch")
        if release_manifest.get("profile") != "brain-current-v1":
            raise CanaryError("release manifest profile mismatch")
        if release_manifest.get("release_id") != self.expected_release_id:
            raise CanaryError("release manifest identity does not match the selector")
        identity_value = dict(release_manifest)
        for key in ("release_id", "attestations", "created_at"):
            identity_value.pop(key, None)
        payload = b"wikilean\0wikilean.release.v1\0canonical-json-v1\0" + self._canonical_json(identity_value)
        if release_manifest.get("release_id") != "sha256:" + hashlib.sha256(payload).hexdigest():
            raise CanaryError("release manifest self-identity mismatch")
        artifacts_raw = release_manifest.get("artifacts")
        if not isinstance(artifacts_raw, list):
            raise CanaryError("release manifest artifacts must be an array")
        artifacts: dict[str, tuple[int, str]] = {}
        for index, value in enumerate(artifacts_raw):
            if not isinstance(value, dict):
                raise CanaryError(f"release manifest artifact {index} must be an object")
            path, size, digest = value.get("path"), value.get("bytes"), value.get("sha256")
            if (
                not isinstance(path, str)
                or not isinstance(size, int)
                or isinstance(size, bool)
                or size < 0
                or not isinstance(digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                or path in artifacts
            ):
                raise CanaryError(f"release manifest artifact {index} is malformed or duplicated")
            artifacts[path] = (size, digest)

        immutable_base = f"/assets/brain/releases/{self.release}"
        cells_raw, cells_bytes = self.fetch_json(f"{immutable_base}/cells/manifest.json")
        self._verify_artifact(
            artifacts,
            "site/assets/brain/cells/manifest.json",
            "cells/manifest.json",
            cells_bytes,
        )
        cells = self._object(cells_raw, "cell manifest")
        shards = cells.get("shards")
        if (
            not isinstance(shards, dict)
            or not shards
            or not all(
                isinstance(key, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", key)
                for key in shards
            )
        ):
            raise CanaryError("cell manifest has no valid shard inventory")
        shard_key = sorted(shards)[0]
        shard_path = f"cells/{shard_key}.json"
        shard_raw, shard_bytes = self.fetch_json(f"{immutable_base}/{shard_path}")
        shard = self._object(shard_raw, "cell shard")
        if not shard:
            raise CanaryError(f"deterministic cell shard {shard_key!r} is empty")
        self._verify_artifact(
            artifacts,
            f"site/assets/brain/{shard_path}",
            shard_path,
            shard_bytes,
        )

        page_bodies: dict[str, bytes] = {}
        for page_path in ("/brain", "/brain.html"):
            page = self.fetch(page_path)
            if "text/html" not in page.content_type.lower() and b"<html" not in page.body[:1024].lower():
                raise CanaryError(f"{page_path} is not HTML")
            if b"/assets/brain/current.json" not in page.body:
                raise CanaryError(f"{page_path} does not bootstrap the Brain release selector")
            self._verify_artifact(
                artifacts,
                "site/out/brain.html",
                page_path,
                page.body,
            )
            page_bodies[page_path] = page.body
        if page_bodies["/brain"] != page_bodies["/brain.html"]:
            raise CanaryError("/brain and /brain.html do not serve the same frozen release page")

        api_raw, _ = self.fetch_json("/api/brain/filter?f=0&limit=1")
        api = self._object(api_raw, "Brain API response")
        if api.get("ok") is not True:
            raise CanaryError("representative Brain API response is not ok")
        if api.get("release_id") != self.expected_release_id:
            raise CanaryError("Brain API release_id does not match the selector")
        if not isinstance(api.get("snapshot"), dict):
            raise CanaryError("Brain API response is missing snapshot metadata")
        hits = api.get("hits")
        if not isinstance(hits, list) or len(hits) != 1:
            raise CanaryError("Brain API filter did not return one representative row")
        cursor = api.get("next_cursor")
        if not isinstance(cursor, str) or not cursor:
            raise CanaryError("Brain API did not emit a new opaque cursor")
        self._verify_filter_cursor(cursor)

        cursor_raw, _ = self.fetch_json(
            "/api/brain/filter?f=0&limit=1&cursor=" + urllib.parse.quote(cursor, safe="")
        )
        cursor_page = self._object(cursor_raw, "Brain API cursor response")
        if cursor_page.get("ok") is not True or cursor_page.get("release_id") != self.expected_release_id:
            raise CanaryError("Brain API cursor did not remain bound to the expected release")
        next_hits = cursor_page.get("hits")
        if not isinstance(next_hits, list) or len(next_hits) != 1 or next_hits == hits:
            raise CanaryError("Brain API cursor did not advance to a distinct row")

        mcp_raw, _ = self.post_json(
            "/mcp",
            {
                "jsonrpc": "2.0",
                "id": "brain-release-canary",
                "method": "tools/call",
                "params": {
                    "name": "brain_filter",
                    "arguments": {"f": 0, "limit": 1},
                },
            },
        )
        mcp = self._object(mcp_raw, "MCP response")
        if mcp.get("jsonrpc") != "2.0" or mcp.get("id") != "brain-release-canary":
            raise CanaryError("MCP response has the wrong JSON-RPC envelope")
        result = self._object(mcp.get("result"), "MCP result")
        if result.get("isError") is True:
            raise CanaryError("MCP tool reported an error")
        content = result.get("content")
        if not isinstance(content, list) or len(content) != 1:
            raise CanaryError("MCP result does not contain one text response")
        content_item = self._object(content[0], "MCP content item")
        if content_item.get("type") != "text" or not isinstance(content_item.get("text"), str):
            raise CanaryError("MCP result is not a text response")
        try:
            mcp_payload = self._object(json.loads(content_item["text"]), "MCP tool payload")
        except json.JSONDecodeError as exc:
            raise CanaryError("MCP tool text is not valid JSON") from exc
        if mcp_payload.get("ok") is not True or mcp_payload.get("release_id") != self.expected_release_id:
            raise CanaryError("MCP tool response is not bound to the expected release")

        alias_pairs = [
            ("cells/manifest.json", cells_bytes),
            (shard_path, shard_bytes),
        ]
        for relative in (
            "cells/aliases.json",
            "cells/labels.json",
            "cells/supercells.json",
            "cells/explorer.json",
            "cells/frontier_graph.json",
            "sources.json",
            "xref_index.json",
        ):
            immutable = self.fetch(f"{immutable_base}/{relative}").body
            self._verify_artifact(
                artifacts,
                f"site/assets/brain/{relative}",
                relative,
                immutable,
            )
            alias_pairs.append((relative, immutable))
        for relative, immutable in alias_pairs:
            mutable = self.fetch(f"/assets/brain/{relative}").body
            if mutable != immutable:
                raise CanaryError(f"mutable compatibility alias differs from immutable {relative}")

        return {
            "schema": "wikilean.brain-canary-result/v1",
            "ok": True,
            "release_id": self.expected_release_id,
            "release": self.release,
            "manifest": expected_manifest,
            "shard": shard_path,
            "cursor": cursor,
            "mcp_tool": "brain_filter",
            "pages_checked": sorted(page_bodies),
            "aliases_checked": [relative for relative, _ in alias_pairs],
            "requests": self.request_count,
            "response_bytes": self.response_bytes,
            "check_duration_ms": round((time.monotonic() - started) * 1000, 3),
            "max_rss_bytes": _max_rss_bytes(),
        }


def poll(canary: BrainCanary, timeout: float, interval: float) -> dict[str, object]:
    if timeout < 0 or interval < 0:
        raise ValueError("timeout and interval must be non-negative")
    started = time.monotonic()
    attempts = 0
    last_error = "canary did not run"
    while True:
        attempts += 1
        try:
            result = canary.check_once()
            result["attempts"] = attempts
            result["convergence_seconds"] = round(time.monotonic() - started, 3)
            return result
        except CanaryError as exc:
            last_error = str(exc)
        elapsed = time.monotonic() - started
        if elapsed >= timeout:
            raise CanaryError(
                f"release did not converge within {timeout:g}s after {attempts} attempt(s): {last_error}"
            )
        time.sleep(min(interval, max(timeout - elapsed, 0)))


def _max_rss_bytes() -> int:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value if sys.platform == "darwin" else value * 1024)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="https://wikilean.jackmccarthy.org")
    parser.add_argument("--expected-release-id", required=True)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--request-timeout", type=float, default=20.0)
    parser.add_argument("--max-response-bytes", type=int, default=DEFAULT_MAX_RESPONSE_BYTES)
    args = parser.parse_args(argv)
    try:
        result = poll(
            BrainCanary(
                args.base_url,
                args.expected_release_id,
                request_timeout=args.request_timeout,
                max_response_bytes=args.max_response_bytes,
            ),
            args.timeout,
            args.interval,
        )
    except (CanaryError, ValueError) as exc:
        print(
            json.dumps({
                "schema": "wikilean.brain-canary-result/v1",
                "ok": False,
                "error": str(exc),
            }, sort_keys=True),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
