"""Acquisition: the only step that uses the network. Writes DIR/PINS.json.

Reads five public, read-only GET routes of the live site and nothing else:

  /api/articles/current             the published build (campaign, universe, record sets)
  /api/articles/index/<shard>       the published build's page ids (enumeration only)
  /api/articles/universe/<sha>      the universe's libraries, each with its `brain_lib`
  /api/articles/article/<pageid>    the sealed view model
  /api/articles/overlay/<pageid>    that article's community overlay

Layout under DIR:
  current.json
  index/<shard>.json
  universe/<sha>.json
  articles/<pageid>.json
  overlay/<pageid>.json
  PINS.json

**One store build.** The build is pinned by the sha256 of `current.json`'s bytes.
Every article must name that build's universe sha256 and one of its record-set seals,
and `current.json` is re-read at the end and must be byte-identical: a republish
mid-run raises FetchError rather than mixing two builds.

Without pins it fetches the newest state. With pins (`--pins`, a PINS.json) it checks
every file's sha256 against the pin; any mismatch raises FetchError naming the input,
no input file is overwritten with mismatched bytes, and no PINS.json is left behind.

Politeness: one request at a time, a delay between requests, a generic User-Agent with
no personal details, and a bounded back-off on 429 and 503 that honours Retry-After.
Every network call goes through one opener (`Opener`), so tests inject a fake.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import time
import urllib.error
import urllib.request

from .canon import dumps, read_json, sha256_bytes, write_bytes

USER_AGENT = "WikiLean-articles-brain/1"
SITE = "https://wikilean.jackmccarthy.org"
PINS_SCHEMA = "wikilean.brain.articles.pins/v1"
CURRENT_SCHEMA = "wikilean.articles.current/1"
ARTICLE_SCHEMA = "wikilean.articles.article/1"
INDEX_SCHEMA = "wikilean.articles.index/1"

SHARD_RE = re.compile(r"^[0-9a-f]{1,2}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
MAX_ATTEMPTS = 5
MAX_RETRY_WAIT = 60.0


class FetchError(Exception):
    pass


class Opener:
    """The network: one HTTP GET at a time. Replace it to run offline."""

    def get(self, url: str) -> tuple[int, dict[str, str], bytes]:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as error:  # a status is an answer, not a failure
            return error.code, dict(error.headers or {}), error.read()
        except (urllib.error.URLError, OSError) as error:
            raise FetchError(f"GET {url}: {error}") from None


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _retry_after(headers: dict[str, str]) -> float | None:
    """Retry-After in seconds, when the answer gives a plain number we believe."""
    for name, value in headers.items():
        if name.lower() != "retry-after":
            continue
        try:
            seconds = float(str(value).strip())
        except ValueError:
            return None
        return min(max(seconds, 0.0), MAX_RETRY_WAIT)
    return None


class Session:
    """One polite, serial reader of the site."""

    def __init__(self, site: str, opener: Opener, delay: float = 1.0, sleep=time.sleep):
        self.site = site.rstrip("/")
        self.opener = opener
        self.delay = delay
        self.sleep = sleep
        self.requests = 0

    def get(self, path: str) -> bytes:
        url = f"{self.site}{path}"
        wait = 1.0
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if self.requests:
                self.sleep(self.delay)
            self.requests += 1
            status, headers, body = self.opener.get(url)
            if status == 200:
                return body
            if status in RETRY_STATUS and attempt < MAX_ATTEMPTS:
                after = _retry_after(headers)
                self.sleep(after if after is not None else min(wait, MAX_RETRY_WAIT))
                wait *= 2
                continue
            raise FetchError(f"GET {path}: HTTP {status}")
        raise FetchError(f"GET {path}: gave up after {MAX_ATTEMPTS} attempts")


def _json(body: bytes, what: str):
    try:
        return json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as error:
        raise FetchError(f"{what}: not JSON ({error})") from None


def read_pins_file(path: str) -> dict:
    pins = read_json(path)
    if not isinstance(pins, dict) or pins.get("schema") != PINS_SCHEMA:
        raise FetchError(f"{path}: not a {PINS_SCHEMA} pins file")
    inputs = pins.get("inputs")
    if not isinstance(inputs, dict):
        raise FetchError(f"{path}: the pins carry no inputs")
    return pins


def pageids_of(shard: dict, what: str) -> list[int]:
    if shard.get("schema") != INDEX_SCHEMA:
        raise FetchError(f"{what}: schema is not {INDEX_SCHEMA}")
    fields = shard.get("fields")
    rows = shard.get("articles")
    if not isinstance(fields, list) or not isinstance(rows, list) or "pageid" not in fields:
        raise FetchError(f"{what}: no pageid column")
    at = fields.index("pageid")
    out = []
    for row in rows:
        if not isinstance(row, list) or len(row) <= at or not isinstance(row[at], int):
            raise FetchError(f"{what}: a row carries no page id")
        out.append(row[at])
    return out


def store_identity(current: dict) -> dict:
    """What makes two answers one published build."""
    if current.get("schema") != CURRENT_SCHEMA:
        raise FetchError(f"current.json: schema is not {CURRENT_SCHEMA}")
    universe = current.get("universe") or {}
    sha = universe.get("sha256")
    if not isinstance(sha, str) or not SHA_RE.match(sha):
        raise FetchError("current.json: the universe has no sha256")
    seals = sorted(
        {
            entry.get("seal_sha256")
            for entry in (current.get("record_sets") or [])
            if isinstance(entry, dict) and isinstance(entry.get("seal_sha256"), str)
        }
        | {
            seal
            for seal in [(current.get("record_set") or {}).get("seal_sha256")]
            if isinstance(seal, str)
        }
    )
    if not seals:
        raise FetchError("current.json: the build names no record set")
    return {
        "built_at": current.get("built_at"),
        "campaign": current.get("campaign"),
        "universe_sha256": sha,
        "record_set_seals": seals,
    }


def fetch(
    inputs: str,
    pins_file: str | None = None,
    opener: Opener | None = None,
    site: str = SITE,
    delay: float = 1.0,
    sleep=time.sleep,
) -> dict:
    session = Session(site, opener or Opener(), delay, sleep)
    want = read_pins_file(pins_file)["inputs"] if pins_file else None
    out_path = os.path.join(inputs, "PINS.json")
    if os.path.exists(out_path):
        os.remove(out_path)  # a failed fetch must leave none
    for sub in ("index", "universe", "articles", "overlay"):
        os.makedirs(os.path.join(inputs, sub), exist_ok=True)
    pins: dict = {"current": {}, "index": {}, "universe": {}, "articles": {}, "overlay": {}}

    def store(group: str, name: str | None, rel: str, data: bytes, extra: dict) -> str:
        sha = sha256_bytes(data)
        if want is not None:
            pinned = want.get(group) if name is None else (want.get(group) or {}).get(name)
            if not isinstance(pinned, dict):
                raise FetchError(f"{rel}: the pins do not name this input")
            if pinned.get("sha256") != sha:
                raise FetchError(f"{rel}: sha256 {sha} does not match pinned {pinned.get('sha256')}")
        write_bytes(os.path.join(inputs, rel), data)
        entry = {"path": rel, "sha256": sha, "fetched_at": _now(), **extra}
        if name is None:
            pins[group] = entry
        else:
            pins[group][name] = entry
        return sha

    first = session.get("/api/articles/current")
    current = _json(first, "current.json")
    identity = store_identity(current)
    store("current", None, "current.json", first, {})

    index = current.get("index") or {}
    shards = index.get("shards")
    if not isinstance(shards, list) or not shards:
        raise FetchError("current.json: the build lists no index shard")
    pageids: list[int] = []
    for shard in shards:
        if not isinstance(shard, str) or not SHARD_RE.match(shard):
            raise FetchError(f"current.json: {shard!r} is not an index shard name")
        body = session.get(f"/api/articles/index/{shard}")
        pageids += pageids_of(_json(body, f"index/{shard}.json"), f"index/{shard}.json")
        store("index", shard, f"index/{shard}.json", body, {})
    pageids = sorted(set(pageids))
    entries = index.get("entries")
    if isinstance(entries, int) and entries != len(pageids):
        raise FetchError(f"the index shards hold {len(pageids)} pages, current.json says {entries}")

    universe_sha = identity["universe_sha256"]
    body = session.get(f"/api/articles/universe/{universe_sha}")
    universe = _json(body, f"universe/{universe_sha}.json")
    if universe.get("sha256") != universe_sha:
        raise FetchError(f"universe/{universe_sha}.json: the answer names universe {universe.get('sha256')}")
    store("universe", universe_sha, f"universe/{universe_sha}.json", body, {})

    for pageid in pageids:
        body = session.get(f"/api/articles/article/{pageid}")
        article = _json(body, f"articles/{pageid}.json")
        check_article(article, pageid, identity)
        store("articles", str(pageid), f"articles/{pageid}.json", body,
              {"record_set": article.get("record_set")})

        body = session.get(f"/api/articles/overlay/{pageid}")
        overlay = _json(body, f"overlay/{pageid}.json")
        check_overlay(overlay, pageid)
        rows = [r for r in (overlay.get("tags") or []) + (overlay.get("bundles") or []) if isinstance(r, dict)]
        versions = [r["version"] for r in rows if isinstance(r.get("version"), int)]
        store("overlay", str(pageid), f"overlay/{pageid}.json", body, {
            "version": max(versions) if versions else 0,
            "tags": len(overlay.get("tags") or []),
            "bundles": len(overlay.get("bundles") or []),
        })

    # One published build: a republish between the first and the last request would
    # leave the articles straddling two of them.
    again = session.get("/api/articles/current")
    if sha256_bytes(again) != pins["current"]["sha256"]:
        raise FetchError("the published build changed while fetching; nothing is pinned")

    if want is not None:
        for group in ("index", "universe", "articles", "overlay"):
            missing = sorted(set(want.get(group) or {}) - set(pins[group]))
            if missing:
                raise FetchError(f"{group}: the pins name {len(missing)} input(s) this fetch did not read: "
                                 f"{', '.join(missing[:5])}")

    document = {"schema": PINS_SCHEMA, "site": session.site, "store": identity, "inputs": pins}
    write_bytes(out_path, (dumps(document) + "\n").encode("utf-8"))
    return document


def check_article(article: dict, pageid: int, identity: dict) -> None:
    where = f"articles/{pageid}.json"
    if article.get("schema") != ARTICLE_SCHEMA:
        raise FetchError(f"{where}: schema is not {ARTICLE_SCHEMA}")
    if article.get("pageid") != pageid:
        raise FetchError(f"{where}: the answer is page {article.get('pageid')}")
    universe = (article.get("universe") or {}).get("sha256")
    if universe != identity["universe_sha256"]:
        raise FetchError(f"{where}: universe {universe} is not the build's {identity['universe_sha256']} "
                         "(two store builds must never be mixed)")
    record_set = article.get("record_set")
    if record_set not in identity["record_set_seals"]:
        raise FetchError(f"{where}: record set {record_set} is not one of the build's "
                         "(two store builds must never be mixed)")


def check_overlay(overlay: dict, pageid: int) -> None:
    if overlay.get("ok") is not True or overlay.get("pageid") != pageid:
        raise FetchError(f"overlay/{pageid}.json: not this article's overlay")
