#!/usr/bin/env python3
"""Download a declaration-index bundle from WikiLean, and talk to its claims API.

The Worker streams the index bundles to an owned agent token (wiki/src/bundles.ts) and
hands out work packages (wiki/src/claims.ts). This module is the transport for both.
It is standard library only and sits beside submission_transport.py, whose origin rule
and refusal of redirects it reuses. Every request names a path on a fixed allowlist and
carries the token in a header: never on a command line, never in an error message.

A download resumes a cut transfer with Range and If-Range, hashes as it writes, and
moves a file into place only once its size and sha256 match. The move matters: staging
hard-links these files into every task directory (scripts/pipeline/run/workdir.py), so a
file truncated in place would corrupt all of them.

    python3 scripts/articles/contrib_transport.py bundles
    python3 scripts/articles/contrib_transport.py fetch-bundle <manifest sha256> \\
        [--cache ~/.cache/wikilean/index] [--skip names/] [--execute]

WIKILEAN_AGENT_TOKEN is read from the environment. Without --execute, fetch-bundle reads
the bundle's MANIFEST.json and says what it would download, and downloads nothing else.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

from submission_transport import BASE_URL, NoRedirect, TransportError, intake, origin

TOKEN_ENV = "WIKILEAN_AGENT_TOKEN"
MAX_JSON_BYTES = 1024 * 1024
RANGE_BYTES = 64 * 1024 * 1024
READ_BYTES = 1024 * 1024
RETRIES = 3
MAX_WAIT_SECONDS = 300
BUNDLE_SCHEMA = "wikilean.pipeline.index-bundle/2"
MANIFEST = "MANIFEST.json"
DEFAULT_CACHE = Path("~/.cache/wikilean/index")
SHA = re.compile(r"[0-9a-f]{64}")
_SEGMENT = r"[A-Za-z0-9_][A-Za-z0-9._-]*"
_RUN = r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}"
# A file a bundle may list: nothing that needs URL-encoding, nothing that leaves the bundle.
BUNDLE_PATH = re.compile(rf"{_SEGMENT}(?:/{_SEGMENT})*")
# Every path this client requests, and nothing else; no query string anywhere.
PATHS = re.compile(
    r"/api/packages"
    rf"|/api/packages/{_RUN}/(?:claims|state)"
    r"|/api/claims"
    r"|/api/claims/clm-[0-9a-f]{32}/(?:package|renew|release|submissions)"
    r"|/api/index/bundles"
    rf"|/api/index/bundles/[0-9a-f]{{64}}(?:/files/{_SEGMENT}(?:/{_SEGMENT})*)?"
    r"|/api/submissions/sub-[0-9a-f]{64}/body"
)
_BROKEN = (OSError, http.client.HTTPException)


class Refusal(TransportError):
    """The service refused: its HTTP status, its code, and how long it asked us to wait."""

    def __init__(self, status: int, code: str, error: str, retry_after: float | None = None):
        super().__init__(f"HTTP {status} ({code}): {error[:1000]}")
        self.status, self.code, self.retry_after = status, code, retry_after


def _open(base: str, path: str, token: str, *, method: str = "GET", body: bytes | None = None,
          headers: dict[str, str] | None = None, timeout: float = 60):
    if not PATHS.fullmatch(path):
        raise TransportError(f"not a path this client requests: {path[:200]}")
    if not token or any(ch.isspace() for ch in token):
        raise TransportError(f"missing or malformed WikiLean agent token; set {TOKEN_ENV}")
    sent = {"Authorization": "Bearer " + token, "User-Agent": "WikiLean-contributor/1", **(headers or {})}
    if body is not None:
        sent["Content-Type"] = "application/json"
    request = urllib.request.Request(origin(base) + path, data=body, method=method, headers=sent)
    try:
        return urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        return exc
    except (urllib.error.URLError, *_BROKEN) as exc:
        # Never the request's repr: it carries the token.
        raise TransportError(f"could not reach {origin(base)} ({type(exc).__name__}); run again") from None


def _retry_after(response) -> float | None:
    value = (response.headers.get("Retry-After") or "").strip()
    return float(value) if value.isdigit() else None


def _read_capped(response, limit: int) -> bytes:
    try:
        data = response.read(limit + 1)
    except _BROKEN:
        raise TransportError("the answer was cut off; run again") from None
    if len(data) > limit:
        raise TransportError(f"the answer exceeds {limit} bytes")
    return data


def _json_object(data: bytes) -> dict | None:
    try:
        value = intake.build_store.strict_json(data.decode("utf-8"))
    except (UnicodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _refusal(status: int, value: dict | None, retry_after: float | None) -> Refusal:
    value = value or {}
    # The write brake answers {ok, error, write_mode} with no code (site-settings.ts).
    code = value.get("code") or ("brake" if "write_mode" in value else "http_error")
    return Refusal(status, str(code), str(value.get("error") or "request refused"), retry_after)


def _exchange(base: str, path: str, token: str, *, method: str = "GET", body: bytes | None = None,
              sleep: Callable[[float], None] = time.sleep) -> tuple[int, bytes, float | None]:
    """One request and its whole answer, waiting out a short 429 at most RETRIES times."""
    for attempt in range(RETRIES + 1):
        response = _open(base, path, token, method=method, body=body)
        with response:
            status, wait = response.code, _retry_after(response)
            data = _read_capped(response, MAX_JSON_BYTES)
        if status != 429 or wait is None or wait > MAX_WAIT_SECONDS or attempt == RETRIES:
            return status, data, wait
        sleep(wait)
    raise AssertionError("unreachable")


def call(base: str, path: str, token: str, payload: dict | None = None, *,
         sleep: Callable[[float], None] = time.sleep) -> dict:
    """One JSON request: a GET without a payload, a POST with one. A refusal raises Refusal."""
    body = None if payload is None else json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    status, data, wait = _exchange(base, path, token, method="GET" if body is None else "POST", body=body, sleep=sleep)
    value = _json_object(data)
    if not 200 <= status < 300:
        raise _refusal(status, value, wait)
    if value is None or value.get("ok") is not True:
        raise TransportError(f"HTTP {status} without a JSON answer")
    return value


def fetch_bytes(base: str, path: str, token: str, *, sleep: Callable[[float], None] = time.sleep) -> bytes:
    """A small file (at most MAX_JSON_BYTES) read whole: a bundle's MANIFEST.json."""
    status, data, wait = _exchange(base, path, token, sleep=sleep)
    if status != 200:
        raise _refusal(status, _json_object(data), wait)
    return data


def _hash_file(path: Path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(READ_BYTES), b""):
            digest.update(block)
    return digest


def _present(path: Path, size: int, sha256: str) -> bool:
    return path.is_file() and path.stat().st_size == size and _hash_file(path).hexdigest() == sha256


def download(base: str, path: str, token: str, dest: Path, *, size: int, sha256: str,
             sleep: Callable[[float], None] = time.sleep) -> Path:
    """One file of known size and sha256 at dest, resuming a cut transfer from dest.part."""
    dest = Path(dest)
    if _present(dest, size, sha256):
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    if not part.is_file() or part.stat().st_size > size:
        part.write_bytes(b"")
    digest = _hash_file(part)
    have = part.stat().st_size
    waits = 0
    while have < size:
        end = min(have + RANGE_BYTES, size) - 1
        response = _open(base, path, token, timeout=300,
                         headers={"Range": f"bytes={have}-{end}", "If-Range": f'"{sha256}"'})
        with response:
            status = response.code
            wait = _retry_after(response)
            if status == 429 and wait is not None and wait <= MAX_WAIT_SECONDS and waits < RETRIES:
                waits += 1
                sleep(wait)
                continue
            if status == 206:
                if not (response.headers.get("Content-Range") or "").startswith(f"bytes {have}-"):
                    raise TransportError(f"{dest.name}: the service answered another range than the one asked for")
                mode, written = "ab", have
            elif status == 200:
                # The whole file (the service ignored the range): start again from its first byte.
                mode, written, digest = "wb", 0, hashlib.sha256()
            else:
                raise _refusal(status, _json_object(_read_capped(response, MAX_JSON_BYTES)), wait)
            start = written
            with part.open(mode) as out:
                while True:
                    try:
                        block = response.read(READ_BYTES)
                    except _BROKEN:
                        block = b""
                    if not block:
                        break
                    if written + len(block) > size:
                        out.close()
                        part.unlink()
                        raise TransportError(f"{dest.name}: the service sent more than the manifest's {size} bytes")
                    out.write(block)
                    digest.update(block)
                    written += len(block)
        if written == start:
            raise TransportError(f"{dest.name}: the service sent no bytes at offset {start}; run again to resume")
        have = written
    if digest.hexdigest() != sha256:
        part.unlink()
        raise TransportError(f"{dest.name}: the bytes do not hash to the manifest's sha256; run again")
    os.replace(part, dest)
    return dest


def read_manifest(raw: bytes, sha256: str) -> dict:
    """The MANIFEST.json named by sha256, checked as far as this client relies on it."""
    if hashlib.sha256(raw).hexdigest() != sha256:
        raise TransportError("MANIFEST.json does not hash to the sha256 it was asked for")
    value = _json_object(raw)
    files = value.get("files") if value else None
    if not value or value.get("schema") != BUNDLE_SCHEMA or not isinstance(files, dict) or not files:
        raise TransportError(f"MANIFEST.json is not a {BUNDLE_SCHEMA} manifest")
    for rel, entry in files.items():
        if (rel == MANIFEST or len(rel) > 300 or not BUNDLE_PATH.fullmatch(rel) or not isinstance(entry, dict)
                or not isinstance(entry.get("sha256"), str) or not SHA.fullmatch(entry["sha256"])
                or type(entry.get("bytes")) is not int or entry["bytes"] < 0):
            raise TransportError(f"MANIFEST.json lists a file without a plain path, sha256 and size: {rel[:120]}")
    return value


def fetch_bundle(base: str, token: str, sha256: str, cache: Path, *, skip: tuple[str, ...] = (),
                 execute: bool = False, sleep: Callable[[float], None] = time.sleep,
                 log: Callable[[str], None] = print) -> dict:
    """A bundle's files under <cache>/<sha256>/, each checked against its MANIFEST.json.

    Without execute, only the manifest is read. MANIFEST.json is written last, so its
    presence in the cache means every file it lists (less the skipped ones) is there.
    """
    if not SHA.fullmatch(sha256):
        raise TransportError("a bundle is named by the 64-hex sha256 of its MANIFEST.json")
    root = Path(cache).expanduser() / sha256
    raw = fetch_bytes(base, f"/api/index/bundles/{sha256}", token, sleep=sleep)
    manifest = read_manifest(raw, sha256)
    wanted = {rel: entry for rel, entry in sorted(manifest["files"].items()) if not rel.startswith(tuple(skip))}
    missing = {rel: entry for rel, entry in wanted.items() if not _present(root / rel, entry["bytes"], entry["sha256"])}
    licences = manifest.get("licences")
    restricted = licences.get("use_restricted") if isinstance(licences, dict) else None
    summary = {
        "bundle": manifest.get("bundle"), "manifest_sha256": sha256, "root": str(root),
        "files": len(wanted), "bytes": sum(entry["bytes"] for entry in wanted.values()),
        "missing": len(missing), "missing_bytes": sum(entry["bytes"] for entry in missing.values()),
        "skipped": len(manifest["files"]) - len(wanted),
        "use_restricted": restricted if isinstance(restricted, list) else [],
        "downloaded": 0, "dry_run": not execute,
    }
    if not execute:
        return summary
    for number, (rel, entry) in enumerate(missing.items(), 1):
        log(f"  [{number}/{len(missing)}] {rel} ({entry['bytes']} bytes)")
        download(base, f"/api/index/bundles/{sha256}/files/{rel}", token, root / rel,
                 size=entry["bytes"], sha256=entry["sha256"], sleep=sleep)
        summary["downloaded"] += 1
    staged = root / (MANIFEST + ".part")
    staged.write_bytes(raw)
    os.replace(staged, root / MANIFEST)
    summary["missing"] = summary["missing_bytes"] = 0
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default=BASE_URL)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("bundles", help="list the live index bundles")
    fetch = commands.add_parser("fetch-bundle", help="download one bundle into the cache, resumably")
    fetch.add_argument("manifest_sha256")
    fetch.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    fetch.add_argument("--skip", action="append", default=[], metavar="PREFIX",
                       help="leave out the files under this path prefix (repeatable)")
    fetch.add_argument("--execute", action="store_true", help="download; without it only the manifest is read")
    args = parser.parse_args(argv)
    token = os.environ.get(TOKEN_ENV, "")
    try:
        if args.command == "bundles":
            result = call(args.base_url, "/api/index/bundles", token)
        else:
            result = fetch_bundle(args.base_url, token, args.manifest_sha256, args.cache,
                                  skip=tuple(args.skip), execute=args.execute,
                                  log=lambda line: print(line, file=sys.stderr))
    except Refusal as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (TransportError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted; run again to resume", file=sys.stderr)
        return 130
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
