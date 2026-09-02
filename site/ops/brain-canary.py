#!/usr/bin/env python3
"""Verify that a deployed Brain release has converged across public surfaces."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import re
import resource
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

from brain_http import (
    HttpResult,
    TransportError,
    fetch_bounded,
    require_https_base_url,
    trusted_urlopen,
)
from brain_public_baseline import (
    BaselineValidationError,
    CRITICAL_PATHS,
    INDEX_FAMILIES,
    PublicAssetFile,
    PublicAssetBaseline,
    verify_public_baseline,
)

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

    def __init__(self, message: str, *, details: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


class BrainCanary:
    def __init__(
        self,
        base_url: str,
        expected_release_id: str,
        *,
        request_timeout: float = 20.0,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        public_baseline: PublicAssetBaseline | None = None,
        opener: Callable[..., object] | None = None,
        nonce: Callable[[], str] | None = None,
    ) -> None:
        match = RELEASE_ID_RE.fullmatch(expected_release_id)
        if not match:
            raise ValueError("expected release id must be sha256:<64 lowercase hex>")
        if not math.isfinite(request_timeout) or request_timeout <= 0:
            raise ValueError("request timeout must be finite and positive")
        if (
            isinstance(max_response_bytes, bool)
            or not isinstance(max_response_bytes, int)
            or max_response_bytes <= 0
        ):
            raise ValueError("maximum response bytes must be positive")
        self.base_url = require_https_base_url(base_url)
        self.expected_release_id = expected_release_id
        self.release = match.group(1)
        self.request_timeout = request_timeout
        self.max_response_bytes = max_response_bytes
        self.public_baseline = public_baseline
        try:
            transport = trusted_urlopen() if opener is None else None
        except TransportError as exc:
            raise CanaryError(f"cannot initialize trusted HTTPS transport: {exc}") from exc
        self.opener = transport if transport is not None else opener
        self.trust_source = (
            transport.trust_source
            if transport is not None
            else str(getattr(opener, "trust_source", "injected"))
        )
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

    def fetch(
        self,
        path: str,
        *,
        body: bytes | None = None,
        allowed_statuses: tuple[int, ...] = (200,),
        require_body: bool = True,
    ) -> HttpResult:
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
            result = fetch_bounded(
                request,
                opener=self.opener,
                timeout=self.request_timeout,
                max_response_bytes=self.max_response_bytes,
                allowed_statuses=allowed_statuses,
            )
        except TransportError as exc:
            raise CanaryError(f"{method} {path} failed: {exc}") from exc
        if require_body and not result.body:
            raise CanaryError(f"{method} {path} returned an empty body")
        self.request_count += 1
        self.response_bytes += len(result.body)
        return result

    @staticmethod
    def _require_exact_same_origin_redirect(
        result: HttpResult,
        canonical_path: str,
    ) -> None:
        if result.status != 307 or not result.location:
            raise CanaryError(
                f"expected an HTTP 307 redirect to {canonical_path}, got {result.status}"
            )
        source = urllib.parse.urlsplit(result.url)
        target = urllib.parse.urlsplit(
            urllib.parse.urljoin(result.url, result.location)
        )
        if (
            target.scheme != source.scheme
            or target.netloc != source.netloc
            or target.path != canonical_path
            or target.query != source.query
            or target.fragment
        ):
            raise CanaryError(
                f"redirect target is not the exact same-origin {canonical_path} alias"
            )

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

    def _verify_public_baseline(self) -> list[str]:
        if self.public_baseline is None:
            return []
        by_path = {item.path: item for item in self.public_baseline.files}
        paths = set(CRITICAL_PATHS)
        for prefix in INDEX_FAMILIES:
            payload = sorted(
                path
                for path in by_path
                if path.startswith(prefix) and path != prefix + "manifest.json"
            )
            if not payload:
                raise CanaryError(f"public baseline has no payload in {prefix.rstrip('/')}")
            paths.add(payload[0])
        checked: list[str] = []
        for path in sorted(paths):
            expected = by_path.get(path)
            if expected is None:
                raise CanaryError(f"public baseline omitted required canary asset {path}")
            if path == "concepts.html":
                body = self.fetch("/concepts").body
            elif path == "404.html":
                body = self.fetch(
                    "/map",
                    allowed_statuses=(404,),
                ).body
            else:
                body = self.fetch("/" + path).body
            if len(body) != expected.bytes or hashlib.sha256(body).hexdigest() != expected.sha256:
                raise CanaryError(f"public asset differs from frozen baseline: /{path}")
            checked.append(path)
        return checked

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

        page = self.fetch("/brain")
        if "text/html" not in page.content_type.lower() and b"<html" not in page.body[:1024].lower():
            raise CanaryError("/brain is not HTML")
        if b"/assets/brain/current.json" not in page.body:
            raise CanaryError("/brain does not bootstrap the Brain release selector")
        self._verify_artifact(
            artifacts,
            "site/out/brain.html",
            "/brain",
            page.body,
        )
        html_alias = self.fetch(
            "/brain.html",
            allowed_statuses=(200, 307),
            require_body=False,
        )
        if html_alias.status == 307:
            self._require_exact_same_origin_redirect(html_alias, "/brain")
            brain_html_delivery = "same-origin-307"
        else:
            if not html_alias.body:
                raise CanaryError("GET /brain.html returned an empty body")
            self._verify_artifact(
                artifacts,
                "site/out/brain.html",
                "/brain.html",
                html_alias.body,
            )
            if html_alias.body != page.body:
                raise CanaryError(
                    "/brain and /brain.html do not serve the same frozen release page"
                )
            brain_html_delivery = "byte-identical-200"

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

        public_assets_checked = self._verify_public_baseline()

        return {
            "schema": "wikilean.brain-canary-result/v1",
            "ok": True,
            "release_id": self.expected_release_id,
            "release": self.release,
            "manifest": expected_manifest,
            "shard": shard_path,
            "cursor": cursor,
            "mcp_tool": "brain_filter",
            "pages_checked": ["/brain", "/brain.html"],
            "brain_html_delivery": brain_html_delivery,
            "aliases_checked": [relative for relative, _ in alias_pairs],
            "public_baseline_id": (
                self.public_baseline.baseline_id if self.public_baseline is not None else None
            ),
            "public_assets_checked": public_assets_checked,
            "requests": self.request_count,
            "response_bytes": self.response_bytes,
            "trust_source": self.trust_source,
            "check_duration_ms": round((time.monotonic() - started) * 1000, 3),
            "max_rss_bytes": _max_rss_bytes(),
        }


def poll(canary: BrainCanary, timeout: float, interval: float) -> dict[str, object]:
    if (
        not math.isfinite(timeout)
        or not math.isfinite(interval)
        or timeout < 0
        or interval < 0
    ):
        raise ValueError("timeout and interval must be finite and non-negative")
    started = time.monotonic()
    attempts = 0
    failed_requests = 0
    failed_response_bytes = 0
    last_error = "canary did not run"
    while True:
        attempts += 1
        try:
            result = canary.check_once()
            result["attempts"] = attempts
            result["convergence_seconds"] = round(time.monotonic() - started, 3)
            result["requests"] = int(result["requests"]) + failed_requests
            result["response_bytes"] = int(result["response_bytes"]) + failed_response_bytes
            return result
        except CanaryError as exc:
            last_error = str(exc)
            failed_requests += canary.request_count
            failed_response_bytes += canary.response_bytes
        elapsed = time.monotonic() - started
        if elapsed >= timeout:
            raise CanaryError(
                f"release did not converge within {timeout:g}s after {attempts} attempt(s): {last_error}",
                details={
                    "release_id": canary.expected_release_id,
                    "attempts": attempts,
                    "convergence_seconds": round(elapsed, 3),
                    "requests": failed_requests,
                    "response_bytes": failed_response_bytes,
                    "max_rss_bytes": _max_rss_bytes(),
                    "trust_source": canary.trust_source,
                },
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
    parser.add_argument("--public-baseline-id")
    parser.add_argument("--public-baseline-root", type=Path)
    args = parser.parse_args(argv)
    try:
        if (args.public_baseline_id is None) != (args.public_baseline_root is None):
            raise ValueError(
                "--public-baseline-id and --public-baseline-root must be supplied together"
            )
        public_baseline = (
            verify_public_baseline(
                args.public_baseline_root,
                Path(__file__).resolve().parents[2],
                expected_baseline_id=args.public_baseline_id,
            )
            if args.public_baseline_root is not None
            else None
        )
        result = poll(
            BrainCanary(
                args.base_url,
                args.expected_release_id,
                request_timeout=args.request_timeout,
                max_response_bytes=args.max_response_bytes,
                public_baseline=public_baseline,
            ),
            args.timeout,
            args.interval,
        )
    except (BaselineValidationError, CanaryError, ValueError) as exc:
        details = exc.details if isinstance(exc, CanaryError) else {}
        print(
            json.dumps({
                "schema": "wikilean.brain-canary-result/v1",
                "ok": False,
                "error": str(exc),
                "release_id": args.expected_release_id,
                "attempts": 0,
                "convergence_seconds": 0.0,
                "requests": 0,
                "response_bytes": 0,
                "max_rss_bytes": _max_rss_bytes(),
                **details,
            }, sort_keys=True),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
