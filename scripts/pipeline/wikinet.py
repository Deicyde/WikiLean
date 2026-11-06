#!/usr/bin/env python3
"""Stage 9a: the one module of this pipeline that may talk to Wikipedia.

docs/PIPELINE.md section 3 (stage 9a). Ported from formal-companion's
``scripts/sources/wikinet.py``, narrowed to what WikiLean's sourceset needs.

**Nothing here decides whether the network is used.** Every request goes through an
injectable :class:`Transport`, and the only caller — :mod:`fetch_sources` — builds a
:class:`CurlTransport` under an explicit ``--execute`` and a :class:`FixtureTransport`
otherwise. That is the shape ``brain/acquire-*.sh`` takes in this repository, and the
reason is the same: an offline default that cannot become a live call by accident.

* :class:`CurlTransport` — ``curl`` in a subprocess, the raw response bytes kept exactly
  as received, https only (on redirects too), ``--fail``, a bounded size and time, a
  User-Agent naming WikiLean and nothing about the operator, a ``maxlag`` on every
  request, a polite minimum interval, and retries with backoff on a lagged replica, a
  429 or a 5xx. An HTTP 403 is never retried: it stops.
* :class:`FixtureTransport` — recorded responses in a directory, keyed by the sha256 of
  the request's canonical query. A request with no recording is refused by name, so a
  test that grew a request says which one.

Three requests are made, each with a fixed parameter set:

* :func:`fetch_parse` — one revision's ``action=parse`` output with the parameters of
  :data:`PARSE_PARAMS`; the bytes are the source's ``source.raw.html``, and the sha256
  of the canonical JSON of that parameter set is what every fetched entry records under
  ``fetch.params_sha256``, so two fetches of one revision with different parameters can
  never pass for one source;
* :func:`project_pages` — the PageAssessments listing of one WikiProject, followed
  through its continuation: page id, title, class and importance, articles only;
* :func:`page_infos` — each page's latest revision id, its Wikidata item and whether it
  is a redirect or a disambiguation page, fifty pages a request.

**TODO (stage 9a).** The biography flag (``P31 = Q5`` over the Wikidata Query Service)
is not ported: it is a second endpoint for one optional flag, and a page with an
undetermined flag is planned, not skipped (:func:`sourceset.is_concept_article` reads
``is True``). Until it lands, ``flags.biography`` stays null on an entry this pipeline
writes.

No other module of this pipeline may fetch anything; :mod:`test_wikinet` enforces it.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.parse
from pathlib import Path
from typing import Callable, Iterable, Iterator, Mapping

#: names WikiLean and nothing about whoever runs it (Wikimedia's User-Agent policy)
USER_AGENT = "wikilean-locate/1 (+https://wikilean.jackmccarthy.org)"
#: seconds of replica lag past which the API refuses us, as it asks bots to
MAXLAG = "5"
#: never more than one request a second from one transport
MIN_INTERVAL = 1.0
MAX_ATTEMPTS = 4
BACKOFF = (2.0, 4.0, 8.0, 16.0)
MAX_FILESIZE = "64M"
TIMEOUT = 120
MAXLAG_WAIT = 5.0
MAX_RETRY_WAIT = 120.0
#: page ids per page-info request (the API's limit for an ordinary client)
INFO_BATCH = 50
#: the MediaWiki API's path on a family's site
API_PATH = "/w/api.php"
#: parameters the transport adds, which the recording key leaves out
TRANSPORT_PARAMS = frozenset({"maxlag"})

#: the one parameter set a source is ever fetched with
PARSE_PARAMS: dict[str, str] = {
    "action": "parse",
    "prop": "text",
    "formatversion": "2",
    "disablelimitreport": "1",
    "disableeditsection": "1",
    "format": "json",
}
FETCH_API = "action=parse"

PAGE_INFO_PARAMS = {"action": "query", "prop": "info|pageprops", "ppprop":
                    "wikibase_item|disambiguation", "formatversion": "2", "format": "json"}
PROJECT_PARAMS = {"action": "query", "list": "projectpages", "wppassessments": "1",
                  "wpplimit": "500", "formatversion": "2", "format": "json"}


class WikiError(ValueError):
    """A request this module will not make, or an answer it will not read."""


class WikiUnavailable(WikiError):
    """The API is lagged, rate-limiting or refusing: try later, nothing was written."""


# --------------------------------------------------------------------------
# the fixed parameter set
# --------------------------------------------------------------------------

def params_sha256() -> str:
    """sha256 of the canonical JSON of :data:`PARSE_PARAMS` (sorted keys, no spaces)."""
    blob = json.dumps(PARSE_PARAMS, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def parse_params(revision: int) -> dict[str, str]:
    """The full request parameters for one revision."""
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise WikiError(f"a revision is a positive integer, not {revision!r}")
    return {**PARSE_PARAMS, "oldid": str(revision)}


def fetch_block() -> dict:
    """The ``fetch`` value of a sourceset entry fetched with the fixed parameter set."""
    return {"api": FETCH_API, "params_sha256": params_sha256()}


# --------------------------------------------------------------------------
# the transports
# --------------------------------------------------------------------------

def canonical_query(params: Mapping[str, str]) -> str:
    """The request's parameters sorted and URL-encoded, without transport parameters."""
    pairs = sorted((str(k), str(v)) for k, v in params.items() if k not in TRANSPORT_PARAMS)
    return urllib.parse.urlencode(pairs)


def request_key(params: Mapping[str, str]) -> str:
    """sha256 of :func:`canonical_query`: what a recording is filed under."""
    return hashlib.sha256(canonical_query(params).encode("utf-8")).hexdigest()


def request_url(site: str, params: Mapping[str, str], path: str = API_PATH) -> str:
    """The full https URL of an API request on ``site`` (``https://en.wikipedia.org``)."""
    if not str(site).startswith("https://"):
        raise WikiError(f"the site {site!r} is not an https URL")
    if path != API_PATH:
        raise WikiError(f"no request is made to {path!r}: only the MediaWiki API is")
    query = urllib.parse.urlencode(sorted((str(k), str(v)) for k, v in params.items()))
    return site.rstrip("/") + path + "?" + query


class CurlTransport:
    """``curl``, bounded, polite and retried; the bytes are kept as received."""

    def __init__(self, *, user_agent: str = USER_AGENT, min_interval: float = MIN_INTERVAL,
                 timeout: int = TIMEOUT, attempts: int = MAX_ATTEMPTS,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic,
                 runner: Callable = subprocess.run, exe: str | None = None):
        self.user_agent = user_agent
        self.min_interval = min_interval
        self.timeout = timeout
        self.attempts = attempts
        self.sleep = sleep
        self.clock = clock
        self.runner = runner
        self.exe = exe
        self._last: float | None = None
        self.requests = 0

    def argv(self, url: str, out: str, headers: str | None = None) -> list[str]:
        """The exact command line, so a test can pin the bounds."""
        exe = self.exe or shutil.which("curl") or "curl"
        argv = [exe, "-sS", "--fail", "--proto", "=https", "--proto-redir", "=https",
                "--max-redirs", "3", "--max-filesize", MAX_FILESIZE,
                "--max-time", str(self.timeout), "-A", self.user_agent,
                "-o", out, "-w", "%{http_code}"]
        if headers:
            argv += ["-D", headers]
        return argv + [url]

    def _wait(self) -> None:
        if self._last is not None:
            gap = self.clock() - self._last
            if gap < self.min_interval:
                self.sleep(self.min_interval - gap)
        self._last = self.clock()

    def get(self, site: str, params: Mapping[str, str], *, path: str = API_PATH) -> bytes:
        url = request_url(site, {**params, "maxlag": MAXLAG}, path)
        problem = "no attempt was made"
        delay = 0.0
        for attempt in range(max(1, self.attempts)):
            if attempt:
                self.sleep(delay)
            self._wait()
            fd, tmp = tempfile.mkstemp(prefix="wl-wiki-", suffix=".json")
            os.close(fd)
            fd, hdr = tempfile.mkstemp(prefix="wl-wiki-", suffix=".headers")
            os.close(fd)
            try:
                self.requests += 1
                try:
                    proc = self.runner(self.argv(url, tmp, hdr), capture_output=True,
                                       text=True)
                except OSError as exc:
                    raise WikiError(f"curl could not be run ({exc}); install it or put it "
                                    f"on PATH — nothing was written for this request"
                                    ) from None
                status = (proc.stdout or "").strip()[-3:]
                body = Path(tmp).read_bytes() if os.path.exists(tmp) else b""
                headers = Path(hdr).read_bytes() if os.path.exists(hdr) else b""
            finally:
                for scratch in (tmp, hdr):
                    try:
                        os.unlink(scratch)
                    except OSError:
                        pass
            backoff = BACKOFF[min(attempt, len(BACKOFF) - 1)]
            wanted = retry_after(headers)
            if proc.returncode == 0 and status == "200":
                if not _lagged(body):
                    return body
                problem = "the replicas are lagged (maxlag)"
                delay = max(backoff, wanted or 0.0, MAXLAG_WAIT)
            elif status == "403":
                raise WikiUnavailable(f"GET {url} was refused (HTTP 403): the API is "
                                      f"blocking this client; stop, and re-run much later")
            elif status in ("429", "500", "502", "503", "504") or proc.returncode in (
                    6, 7, 28, 35, 52, 55, 56):
                problem = (f"curl exit {proc.returncode}, HTTP {status or '?'}: "
                           f"{(proc.stderr or '').strip()[:200]}")
                delay = max(backoff, wanted or 0.0)
            else:
                raise WikiError(f"GET {url} failed: curl exit {proc.returncode}, HTTP "
                                f"{status or '?'}: {(proc.stderr or '').strip()[:200]}; "
                                f"nothing was written for this request")
            delay = min(delay, MAX_RETRY_WAIT)
        raise WikiUnavailable(f"GET {url} failed {max(1, self.attempts)} time(s): "
                              f"{problem}; re-run later, nothing was written")


def retry_after(headers: bytes) -> float | None:
    """The seconds a ``Retry-After`` header of the LAST response asks for, or None.

    ``curl -D`` writes every response of a redirect chain; the last block is the answer.
    Only the delta-seconds form is read (an HTTP-date is ignored).
    """
    text = headers.decode("latin-1", errors="replace")
    blocks = [b for b in text.replace("\r\n", "\n").split("\n\n") if b.strip()]
    for line in (blocks[-1].split("\n") if blocks else []):
        name, _, value = line.partition(":")
        if name.strip().lower() == "retry-after":
            value = value.strip()
            if value.isdigit():
                return float(value)
    return None


def _lagged(body: bytes) -> bool:
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return False
    return isinstance(data, dict) and (data.get("error") or {}).get("code") == "maxlag"


class FixtureTransport:
    """Recorded responses: ``<dir>/*.json`` files ``{"query": {...}, "body" | "json"}``.

    ``body`` is the response text exactly; ``json`` instead holds a JSON value, whose
    bytes are its canonical encoding. Files are matched by :func:`request_key` of their
    ``query``.
    """

    def __init__(self, directory: str | Path):
        self.directory = Path(directory)
        if not self.directory.is_dir():
            raise WikiError(f"no fixture directory at {self.directory}")
        self.recorded: dict[str, bytes] = {}
        for path in sorted(self.directory.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("query"), dict):
                raise WikiError(f"{path}: a recording is {{query, body | json}}")
            if "body" in data:
                body = str(data["body"]).encode("utf-8")
            else:
                body = json.dumps(data.get("json"), ensure_ascii=False, sort_keys=True,
                                  separators=(",", ":")).encode("utf-8")
            self.recorded[request_key(data["query"])] = body
        self.requests: list[str] = []

    def get(self, site: str, params: Mapping[str, str], *, path: str = API_PATH) -> bytes:
        query = canonical_query(params)
        self.requests.append(query)
        try:
            return self.recorded[request_key(params)]
        except KeyError:
            raise WikiError(f"no recorded response for {query}; record it into "
                            f"{self.directory} as {{\"query\": {{…}}, \"body\": …}}"
                            ) from None


# --------------------------------------------------------------------------
# the three requests
# --------------------------------------------------------------------------

def _json(body: bytes, what: str) -> dict:
    try:
        text = body.decode("utf-8")
        data = json.loads(text[1:] if text.startswith("﻿") else text)
    except (UnicodeDecodeError, ValueError) as exc:
        raise WikiError(f"{what}: the response is not JSON ({exc})") from None
    if not isinstance(data, dict):
        raise WikiError(f"{what}: the response is not a JSON object")
    if isinstance(data.get("error"), dict):
        error = data["error"]
        raise WikiError(f"{what}: the API refused it ({error.get('code')}: "
                        f"{error.get('info', '')})")
    return data


def fetch_parse(transport, site: str, revision: int) -> bytes:
    """One revision's ``action=parse`` response, as raw bytes; checked, never altered."""
    body = transport.get(site, parse_params(revision))
    check_parse(body, revision)
    return body


def check_parse(body: bytes, revision: int) -> dict:
    """``parse`` of an ``action=parse`` response of ``revision``; else :class:`WikiError`.

    The checks every recorded fetch passes, whether the bytes just arrived or were found
    on disk from an earlier run: JSON, a ``parse.text``, and the revision asked for (a
    missing ``revid`` is accepted, as the API omits it for none).
    """
    data = _json(body, f"parse of revision {revision}")
    parse = data.get("parse")
    if not isinstance(parse, dict) or not isinstance(parse.get("text"), (str, dict)):
        raise WikiError(f"parse of revision {revision}: no parse.text in the response")
    if parse.get("revid") not in (None, revision):
        raise WikiError(f"parse of revision {revision} answered for revision "
                        f"{parse.get('revid')}")
    return parse


def _continued(transport, site: str, params: dict, what: str) -> Iterator[dict]:
    """Each answer of a continued query, the continuation followed to its end."""
    seen: set[str] = set()
    current = dict(params)
    while True:
        data = _json(transport.get(site, current), what)
        yield data
        cont = data.get("continue")
        if not isinstance(cont, dict) or not cont:
            return
        key = json.dumps(cont, sort_keys=True)
        if key in seen:
            raise WikiError(f"{what}: the continuation repeats itself ({key[:80]})")
        seen.add(key)
        current = {**params, **{k: str(v) for k, v in cont.items()}}


def project_pages(transport, site: str, project: str) -> dict[int, dict]:
    """``{pageid: {title, class, importance}}`` of one WikiProject's article namespace."""
    out: dict[int, dict] = {}
    params = {**PROJECT_PARAMS, "wppprojects": project}
    for data in _continued(transport, site, params, f"the pages of {project}"):
        for row in (data.get("query") or {}).get("projectpages") or []:
            for page in row.get("pages") or []:
                if page.get("ns") != 0 or not isinstance(page.get("pageid"), int):
                    continue
                assessment = (page.get("assessment") or {})
                out[page["pageid"]] = {
                    "title": str(page.get("title") or ""),
                    "class": str(assessment.get("class") or "") or None,
                    "importance": str(assessment.get("importance") or "") or None,
                }
    return out


def page_infos(transport, site: str, pageids: Iterable[int]) -> dict[int, dict]:
    """``{pageid: {revision, wikidata, redirect, disambiguation}}``, fifty a request."""
    wanted = sorted({int(p) for p in pageids})
    out: dict[int, dict] = {}
    for at in range(0, len(wanted), INFO_BATCH):
        batch = wanted[at:at + INFO_BATCH]
        params = {**PAGE_INFO_PARAMS, "pageids": "|".join(str(p) for p in batch)}
        for data in _continued(transport, site, params,
                               f"the info of {len(batch)} page(s)"):
            for page in (data.get("query") or {}).get("pages") or []:
                pageid = page.get("pageid")
                if not isinstance(pageid, int):
                    continue
                props = page.get("pageprops") or {}
                row = out.setdefault(pageid, {"revision": None, "wikidata": None,
                                              "redirect": None, "disambiguation": None})
                if isinstance(page.get("lastrevid"), int):
                    row["revision"] = page["lastrevid"]
                if props.get("wikibase_item"):
                    row["wikidata"] = str(props["wikibase_item"])
                row["redirect"] = bool(page.get("redirect"))
                row["disambiguation"] = "disambiguation" in props
    return out
