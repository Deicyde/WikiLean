#!/usr/bin/env python3
"""Bounded, certificate-verified HTTP transport for Brain release operations."""
from __future__ import annotations

import hashlib
import importlib.metadata
import math
import platform
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, Collection

DEFAULT_SELECTOR_MAX_BYTES = 64 * 1024


class TransportError(RuntimeError):
    """A transport response cannot be trusted or does not meet its contract."""


@dataclass(frozen=True)
class HttpResult:
    body: bytes
    content_type: str
    status: int
    url: str
    location: str | None = None


@dataclass(frozen=True)
class SelectorProbe:
    body: bytes
    body_sha256: str
    content_type: str
    status: int
    trust_source: str
    url: str

    @property
    def missing(self) -> bool:
        return self.status == 404

    @property
    def sha256(self) -> str:
        """Compatibility name used by the promotion state machine."""
        return self.body_sha256


@dataclass(frozen=True)
class TrustedUrlOpener:
    """Callable urllib opener together with the CA source used to build it."""

    open: Callable[..., object]
    trust_source: str

    def __call__(self, *args: object, **kwargs: object) -> object:
        return self.open(*args, **kwargs)


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        return None


def _package_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _trusted_context() -> tuple[ssl.SSLContext, str]:
    """Build an explicit verified context, never Python's ambient CA default."""
    try:
        import certifi

        with open(certifi.where(), "rb") as ca_file:
            ca_bytes = ca_file.read()
        context = ssl.create_default_context(
            purpose=ssl.Purpose.SERVER_AUTH,
            cadata=ca_bytes.decode("ascii"),
        )
        source = (
            f"certifi:{_package_version('certifi')}:"
            f"sha256:{hashlib.sha256(ca_bytes).hexdigest()}"
        )
    except (ImportError, OSError, UnicodeError) as certifi_error:
        try:
            import truststore

            context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            environment = "\0".join(
                (sys.platform, platform.platform(), ssl.OPENSSL_VERSION)
            ).encode("utf-8")
            source = (
                f"truststore:{_package_version('truststore')}:"
                f"environment-sha256:{hashlib.sha256(environment).hexdigest()}"
            )
        except (ImportError, OSError) as truststore_error:
            raise TransportError(
                "cannot construct a trusted TLS context from certifi or truststore "
                f"(certifi: {certifi_error}; truststore: {truststore_error})"
            ) from truststore_error
    if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
        raise TransportError("trusted TLS context must verify certificates and hostnames")
    return context, source


def trusted_urlopen() -> TrustedUrlOpener:
    """Return a redirect-rejecting urllib opener with an explicit CA source."""
    context, source = _trusted_context()
    opener = urllib.request.build_opener(
        _RejectRedirects(),
        urllib.request.HTTPSHandler(context=context),
    )
    return TrustedUrlOpener(opener.open, source)


def require_https_base_url(base_url: str) -> str:
    """Validate and normalize a production origin used by the promoter."""
    parsed = urllib.parse.urlsplit(base_url)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("base URL must be an absolute HTTPS URL without credentials, query, or fragment")
    path = parsed.path.rstrip("/")
    return urllib.parse.urlunsplit(("https", parsed.netloc, path, "", ""))


def fetch_bounded(
    request: urllib.request.Request,
    *,
    opener: Callable[..., object],
    timeout: float,
    max_response_bytes: int,
    allowed_statuses: Collection[int] = (200,),
) -> HttpResult:
    """Open one request without redirects and read at most the configured bytes."""
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("request timeout must be finite and positive")
    if (
        isinstance(max_response_bytes, bool)
        or not isinstance(max_response_bytes, int)
        or max_response_bytes <= 0
    ):
        raise ValueError("maximum response bytes must be positive")
    allowed = frozenset(allowed_statuses)
    if not allowed or any(isinstance(value, bool) or not isinstance(value, int) for value in allowed):
        raise ValueError("allowed statuses must be non-empty integers")
    method = request.get_method()

    response: object
    try:
        try:
            response = opener(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            if exc.code not in allowed:
                raise TransportError(f"{method} {request.full_url} returned HTTP {exc.code}") from exc
            response = exc
        with response:  # type: ignore[attr-defined]
            status_value = getattr(response, "status", None)
            if status_value is None:
                status_value = response.getcode()  # type: ignore[attr-defined]
            status = int(status_value)
            if status not in allowed:
                raise TransportError(f"{method} {request.full_url} returned HTTP {status}")
            final_url_getter = getattr(response, "geturl", None)
            final_url = final_url_getter() if callable(final_url_getter) else request.full_url
            if final_url != request.full_url:
                raise TransportError(
                    f"{method} {request.full_url} redirected to {final_url}; redirects are forbidden"
                )
            body = response.read(max_response_bytes + 1)  # type: ignore[attr-defined]
            headers = getattr(response, "headers", {})
            content_type = headers.get("Content-Type", "")
            location = headers.get("Location")
    except TransportError:
        raise
    except (OSError, urllib.error.URLError) as exc:
        raise TransportError(f"{method} {request.full_url} failed: {exc}") from exc

    if len(body) > max_response_bytes:
        raise TransportError(
            f"{method} {request.full_url} exceeded the {max_response_bytes} byte response limit"
        )
    return HttpResult(
        body=body,
        content_type=content_type,
        status=status,
        url=final_url,
        location=location,
    )


def probe_selector(
    base_url: str,
    *,
    request_timeout: float = 20.0,
    max_response_bytes: int = DEFAULT_SELECTOR_MAX_BYTES,
    opener: Callable[..., object] | None = None,
    nonce: str | Callable[[], str] | None = None,
) -> SelectorProbe:
    """Probe the live release selector, distinguishing a trusted 200 from 404."""
    base = require_https_base_url(base_url)
    transport = trusted_urlopen() if opener is None else None
    open_request = transport if transport is not None else opener
    assert open_request is not None
    trust_source = transport.trust_source if transport is not None else getattr(opener, "trust_source", "injected")
    if nonce is None:
        token = str(time.time_ns())
    elif callable(nonce):
        token = nonce()
    elif isinstance(nonce, str) and nonce:
        token = nonce
    else:
        raise ValueError("selector probe nonce must be a non-empty string or callable")
    query = urllib.parse.urlencode({"__brain_preflight": token})
    url = f"{base}/assets/brain/current.json?{query}"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Cache-Control": "no-cache",
            "User-Agent": "WikiLean-Brain-Canary/1",
        },
        method="GET",
    )
    result = fetch_bounded(
        request,
        opener=open_request,
        timeout=request_timeout,
        max_response_bytes=max_response_bytes,
        allowed_statuses=(200, 404),
    )
    if result.status == 200 and not result.body:
        raise TransportError("GET release selector returned an empty HTTP 200 body")
    return SelectorProbe(
        body=result.body,
        body_sha256=hashlib.sha256(result.body).hexdigest(),
        content_type=result.content_type,
        status=result.status,
        trust_source=str(trust_source),
        url=result.url,
    )
