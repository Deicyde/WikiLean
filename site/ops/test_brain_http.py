#!/usr/bin/env python3
from __future__ import annotations

import io
import urllib.error
import urllib.request
import unittest
from email.message import Message
from urllib.parse import parse_qs, urlsplit

import brain_http


class FakeResponse:
    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        content_type: str = "application/json",
        final_url: str | None = None,
    ) -> None:
        self.body = body
        self.status = status
        self.headers = {"Content-Type": content_type}
        self.final_url = final_url

    def read(self, size: int = -1) -> bytes:
        return self.body if size < 0 else self.body[:size]

    def getcode(self) -> int:
        return self.status

    def geturl(self) -> str:
        assert self.final_url is not None
        return self.final_url

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> bool:
        return False


class RecordingOpener:
    trust_source = "fixture-ca:1"

    def __init__(self, response: FakeResponse | None = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.requests = []
        self.timeouts = []

    def __call__(self, request, timeout: float):
        self.requests.append(request)
        self.timeouts.append(timeout)
        if self.error is not None:
            raise self.error
        assert self.response is not None
        if self.response.final_url is None:
            self.response.final_url = request.full_url
        return self.response


class BrainHttpTest(unittest.TestCase):
    def test_trusted_opener_uses_an_explicit_maintained_ca_source(self) -> None:
        opener = brain_http.trusted_urlopen()
        self.assertTrue(callable(opener))
        self.assertRegex(
            opener.trust_source,
            r"^(?:certifi:[^:]+:sha256|truststore:[^:]+:environment-sha256):[0-9a-f]{64}$",
        )

    def test_https_base_url_is_strict_and_normalized(self) -> None:
        self.assertEqual(
            brain_http.require_https_base_url("https://example.test/root/"),
            "https://example.test/root",
        )
        for value in (
            "http://example.test",
            "//example.test",
            "https://user@example.test",
            "https://example.test?query=1",
            "https://example.test/#fragment",
        ):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "absolute HTTPS"):
                brain_http.require_https_base_url(value)

    def test_selector_probe_surfaces_200_with_digest_and_trust_source(self) -> None:
        body = b'{"schema":"wikilean.release-selector/v1"}\n'
        opener = RecordingOpener(FakeResponse(body))
        probe = brain_http.probe_selector(
            "https://example.test",
            opener=opener,
            nonce=lambda: "fixed nonce",
            request_timeout=7,
        )

        self.assertEqual(probe.status, 200)
        self.assertFalse(probe.missing)
        self.assertEqual(probe.body, body)
        self.assertEqual(
            probe.body_sha256,
            "776e2a83d6135806a09fe1265f3e95f11b06720a2d6b2227a9809b1c1ceefae9",
        )
        self.assertEqual(probe.sha256, probe.body_sha256)
        self.assertEqual(probe.trust_source, "fixture-ca:1")
        self.assertEqual(opener.timeouts, [7])
        request = opener.requests[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(
            parse_qs(urlsplit(request.full_url).query),
            {"__brain_preflight": ["fixed nonce"]},
        )
        self.assertEqual(request.get_header("Cache-control"), "no-cache")

    def test_selector_probe_surfaces_http_error_404(self) -> None:
        url = "https://example.test/assets/brain/current.json?__brain_preflight=missing"
        headers = Message()
        headers["Content-Type"] = "text/plain"
        error = urllib.error.HTTPError(url, 404, "Not Found", headers, io.BytesIO(b"missing"))
        probe = brain_http.probe_selector(
            "https://example.test",
            opener=RecordingOpener(error=error),
            nonce="missing",
        )

        self.assertEqual(probe.status, 404)
        self.assertTrue(probe.missing)
        self.assertEqual(probe.body, b"missing")

    def test_selector_probe_rejects_other_statuses_transport_errors_and_empty_200(self) -> None:
        cases = (
            (RecordingOpener(FakeResponse(b"forbidden", status=403)), "HTTP 403"),
            (RecordingOpener(error=urllib.error.URLError("dns failed")), "dns failed"),
            (RecordingOpener(FakeResponse(b"", status=200)), "empty HTTP 200"),
        )
        for opener, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(brain_http.TransportError, message):
                brain_http.probe_selector(
                    "https://example.test",
                    opener=opener,
                    nonce=lambda: "test",
                )

    def test_fetch_rejects_redirects_and_oversized_bodies(self) -> None:
        request_url = "https://example.test/value"
        request = urllib.request.Request(request_url)
        redirected = RecordingOpener(
            FakeResponse(b"ok", final_url="https://other.test/value")
        )
        with self.assertRaisesRegex(brain_http.TransportError, "redirects are forbidden"):
            brain_http.fetch_bounded(
                request,
                opener=redirected,
                timeout=1,
                max_response_bytes=16,
            )

        oversized = RecordingOpener(FakeResponse(b"12345"))
        with self.assertRaisesRegex(brain_http.TransportError, "response limit"):
            brain_http.fetch_bounded(
                request,
                opener=oversized,
                timeout=1,
                max_response_bytes=4,
            )

    def test_explicitly_allowed_redirect_is_returned_without_following(self) -> None:
        request_url = "https://example.test/brain.html?nonce=one"
        headers = Message()
        headers["Content-Type"] = "text/html"
        headers["Location"] = "/brain?nonce=one"
        error = urllib.error.HTTPError(
            request_url,
            307,
            "Temporary Redirect",
            headers,
            io.BytesIO(b""),
        )
        result = brain_http.fetch_bounded(
            urllib.request.Request(request_url),
            opener=RecordingOpener(error=error),
            timeout=1,
            max_response_bytes=16,
            allowed_statuses=(307,),
        )
        self.assertEqual(result.status, 307)
        self.assertEqual(result.url, request_url)
        self.assertEqual(result.location, "/brain?nonce=one")

    def test_fetch_rejects_nonfinite_timeout_and_noninteger_limit(self) -> None:
        request = urllib.request.Request("https://example.test/value")
        opener = RecordingOpener(FakeResponse(b"ok"))
        for timeout in (float("nan"), float("inf")):
            with self.subTest(timeout=timeout), self.assertRaisesRegex(
                ValueError, "finite"
            ):
                brain_http.fetch_bounded(
                    request,
                    opener=opener,
                    timeout=timeout,
                    max_response_bytes=16,
                )
        with self.assertRaisesRegex(ValueError, "maximum response"):
            brain_http.fetch_bounded(
                request,
                opener=opener,
                timeout=1,
                max_response_bytes=1.5,
            )


if __name__ == "__main__":
    unittest.main()
