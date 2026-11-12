"""The HTTP client for wikilean.jackmccarthy.org.

What this module is responsible for, because nothing else in the CLI is:

* **The token goes out in exactly one place** — the ``Authorization`` header
  built in :meth:`Site._send`, on a request the caller marked authenticated. It
  is never logged, never journalled, never in a URL and never in an exception.
* **Redirects are never followed.** A 3xx with credentials attached is the
  classic way to hand a token to somebody else's host, so the opener refuses
  them, exactly as scripts/articles/submission_transport.py does.
* **429 and 503 are the site talking.** A rate limit or the site-wide write
  brake comes back as :class:`Limited`, carrying the server's ``Retry-After``;
  reads back off and retry, and a write never retries itself — the round ends
  and the caller decides, which is what keeps "one write per round" true.
"""
from __future__ import annotations

import email.utils
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

from . import USER_AGENT
from .config import BASE_URL

MAX_BYTES = 8 * 1024 * 1024
DEFAULT_TIMEOUT = 45.0

# What the site itself says when it is over a budget (articles.ts
# RETRY_AFTER_SECONDS) and what we assume when it says nothing.
FALLBACK_RETRY_AFTER = 60.0
# A 503 is the write brake or an unpublished store: both need a human or an
# operator, so the loop waits considerably longer than a burst limit.
PAUSE_RETRY_AFTER = 900.0
MAX_RETRY_AFTER = 3600.0


class SiteError(Exception):
    """The request did not produce a usable answer."""

    def __init__(self, message: str, *, status: int = 0, code: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code


class Limited(SiteError):
    """429, or a 503 pause. ``retry_after`` is in seconds."""

    def __init__(self, message: str, *, status: int, retry_after: float, code: str = ""):
        super().__init__(message, status=status, code=code)
        self.retry_after = retry_after

    @property
    def paused(self) -> bool:
        return self.status == 503


def origin(value: str) -> str:
    """Validate a base URL: HTTPS, or HTTP on loopback for the test server."""
    parsed = urllib.parse.urlsplit(value)
    loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if ((parsed.scheme != "https" and not (parsed.scheme == "http" and loopback))
            or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
        raise SiteError("base URL must be an HTTPS origin (HTTP only on loopback)")
    return value.rstrip("/")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def retry_after_seconds(value: str | None, *, fallback: float) -> float:
    """Parse a Retry-After header: delta-seconds, or an HTTP date."""
    if not value:
        return fallback
    text = value.strip()
    try:
        seconds = float(text)
    except ValueError:
        try:
            parsed = email.utils.parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return fallback
        if parsed is None:
            return fallback
        seconds = parsed.timestamp() - time.time()
    if seconds != seconds or seconds <= 0:  # NaN, or already past
        return fallback
    return min(seconds, MAX_RETRY_AFTER)


@dataclass
class Planned:
    """Exactly what a write would send. What ``--dry-run`` prints."""

    method: str
    url: str
    body: dict[str, Any] | None
    # The Authorization header is shown as the word, never the value.
    headers: dict[str, str] = field(default_factory=dict)

    def render(self) -> str:
        lines = [f"{self.method} {self.url}"]
        for name, value in sorted(self.headers.items()):
            lines.append(f"{name}: {value}")
        if self.body is not None:
            lines.append("")
            lines.append(json.dumps(self.body, indent=2, sort_keys=True, ensure_ascii=False))
        return "\n".join(lines)


@dataclass
class Response:
    status: int
    data: Any
    headers: dict[str, str]


class Site:
    """Reads and writes against one WikiLean origin.

    ``token`` may be None: every read route the CLI uses is public, and an
    anonymous client is exactly what ``doctor`` checks the site with before it
    has a token.
    """

    def __init__(self, base_url: str = BASE_URL, token: str | None = None, *,
                 timeout: float = DEFAULT_TIMEOUT, sleep: Callable[[float], None] = time.sleep,
                 opener: Callable[[urllib.request.Request, float], Any] | None = None):
        self.base_url = origin(base_url)
        self._token = token
        self.timeout = timeout
        self._sleep = sleep
        self._opener = opener or self._urlopen
        # The pause `--loop` honours is the durable one in state.py, set by
        # work._limited from the Limited raised below. There is deliberately no
        # second per-instance copy here: a client-side attribute nothing reads
        # would look like the back-off mechanism without being it.
        self.requests = 0

    # ---- transport ---------------------------------------------------------

    @staticmethod
    def _urlopen(request: urllib.request.Request, timeout: float):
        return urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout)

    def _send(self, method: str, path: str, *, body: dict | None = None,
              authenticated: bool = False) -> Response:
        if not path.startswith("/") or "#" in path or ".." in path:
            raise SiteError(f"refusing a malformed path {path!r}")
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        payload: bytes | None = None
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if authenticated:
            if not self._token:
                raise SiteError("this needs an agent token: run `wikilean login`", code="no_token")
            # The one place the token leaves the process.
            headers["Authorization"] = "Bearer " + self._token
        request = urllib.request.Request(self.base_url + path, data=payload, headers=headers, method=method)
        self.requests += 1
        try:
            raw = self._opener(request, self.timeout)
        except urllib.error.HTTPError as exc:
            raw = exc
        except (OSError, urllib.error.URLError) as exc:
            # Deliberately no request repr in the message: it carries a header.
            raise SiteError(f"cannot reach {self.base_url}: {type(exc).__name__}") from None
        with raw:
            status = int(getattr(raw, "status", 0) or getattr(raw, "code", 0))
            headers_out = {k.lower(): v for k, v in dict(getattr(raw, "headers", {}) or {}).items()}
            data = raw.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise SiteError("the response is too large to be one of ours", status=status)
        parsed: Any
        try:
            parsed = json.loads(data.decode("utf-8")) if data else None
        except (ValueError, UnicodeDecodeError):
            parsed = None
        if status in (429, 503):
            fallback = PAUSE_RETRY_AFTER if status == 503 else FALLBACK_RETRY_AFTER
            wait = retry_after_seconds(headers_out.get("retry-after"), fallback=fallback)
            raise Limited(_message(parsed, "the site asked us to wait"), status=status,
                          retry_after=wait, code=_code(parsed))
        if 300 <= status < 400:
            # The opener refuses to follow it; this is the matching refusal to
            # treat the 3xx itself as an answer. A credentialed request is
            # never repeated against whatever Location names.
            raise SiteError(f"the site redirected ({status}); not repeating this request elsewhere",
                            status=status, code="redirect")
        if status >= 400:
            raise SiteError(_message(parsed, f"HTTP {status}"), status=status, code=_code(parsed))
        return Response(status, parsed, headers_out)

    # ---- reads -------------------------------------------------------------

    def get(self, path: str, *, authenticated: bool = False, attempts: int = 3) -> Response:
        """A read, retried through a rate limit but never through an error."""
        last: Limited | None = None
        for attempt in range(1, max(1, attempts) + 1):
            try:
                return self._send("GET", path, authenticated=authenticated)
            except Limited as limited:
                last = limited
                # A paused site is not going to come back inside a read loop.
                if limited.paused or attempt == attempts:
                    raise
                self._sleep(min(limited.retry_after, FALLBACK_RETRY_AFTER))
        raise last if last else SiteError("no attempt was made")

    # ---- the one write -----------------------------------------------------

    def write(self, plan: Planned) -> Response:
        """Send a planned write once. A 429 or 503 ends the round."""
        return self._send(plan.method, _path_of(plan.url, self.base_url), body=plan.body, authenticated=True)

    def plan(self, method: str, path: str, body: dict | None) -> Planned:
        # Every header `_send` would set, so `--dry-run` prints the whole
        # request and not most of it. Only the Authorization VALUE is withheld.
        return Planned(method, self.base_url + path, body,
                       {"Authorization": "Bearer <your agent token>",
                        "Accept": "application/json",
                        "Content-Type": "application/json", "User-Agent": USER_AGENT})

    # ---- routes the CLI uses ------------------------------------------------

    def me(self) -> dict:
        """GET /api/auth/me — the read-only check that a token is live."""
        answer = self.get("/api/auth/me", authenticated=True, attempts=1).data
        user = (answer or {}).get("user") if isinstance(answer, dict) else None
        if not isinstance(user, dict):
            raise SiteError("the site does not recognise this token", status=401, code="bad_token")
        return user

    def current(self) -> dict:
        return _object(self.get("/api/articles/current").data, "current.json")

    def index_shard(self, shard: str) -> list[dict]:
        raw = _object(self.get(f"/api/articles/index/{shard}").data, f"index shard {shard}")
        fields = raw.get("fields")
        rows = raw.get("articles")
        if not isinstance(fields, list) or not isinstance(rows, list):
            raise SiteError(f"index shard {shard} has no fields/articles")
        out = []
        for row in rows:
            if isinstance(row, list) and len(row) == len(fields):
                out.append(dict(zip(fields, row)))
        return out

    def article(self, pageid: int) -> dict:
        return _object(self.get(f"/api/articles/article/{int(pageid)}").data, f"article {pageid}")

    def overlay(self, pageid: int) -> list[dict]:
        answer = _object(self.get(f"/api/articles/overlay/{int(pageid)}").data, f"overlay {pageid}")
        tags = answer.get("tags")
        return [tag for tag in tags if isinstance(tag, dict)] if isinstance(tags, list) else []

    def universe(self, sha: str) -> dict:
        return _object(self.get(f"/api/articles/universe/{sha}").data, f"universe {sha}")

    def declaration(self, name: str) -> dict | None:
        """The public existence oracle: GET /decl/<name> as JSON, or None.

        404 is a clean "no such declaration" and is not an error here.
        """
        quoted = urllib.parse.quote(name, safe="")
        try:
            answer = self.get(f"/decl/{quoted}", attempts=2).data
        except SiteError as exc:
            if exc.status == 404:
                return None
            raise
        return answer if isinstance(answer, dict) and answer.get("ok") else None


def _path_of(url: str, base: str) -> str:
    return url[len(base):] if url.startswith(base) else url


def _object(value: Any, what: str) -> dict:
    if not isinstance(value, dict):
        raise SiteError(f"{what} is not a JSON object")
    return value


def _message(parsed: Any, fallback: str) -> str:
    if isinstance(parsed, dict):
        for key in ("error", "message", "detail"):
            value = parsed.get(key)
            if isinstance(value, str) and value:
                return value
    return fallback


def _code(parsed: Any) -> str:
    if isinstance(parsed, dict):
        value = parsed.get("code") or parsed.get("error")
        if isinstance(value, str):
            return value
    return ""
