#!/usr/bin/env python3
"""Publish a built formalization-atlas store to its R2 bucket (docs/ATLAS.md, section 6).

New code. An operator tool: usage, credentials and recovery are in scripts/atlas/UPLOAD.md.
It talks to R2 through R2's S3-compatible API, signing every request with AWS Signature
Version 4 (region "auto", path-style requests to https://<account-id>.r2.cloudflarestorage.com/
<bucket>/<key>), using the standard library only. For the TLS trust store it takes certifi's
CA bundle, else truststore's system store, when either is installed (both are pinned in
requirements-ci.txt; site/ops/brain_http.py prefers them in the same order), else Python's
platform default; every connection verifies the certificate and the host name.

The store directory is the one build_store.py writes (--out): it holds atlas/v1/ and
nothing else, and atlas/v1/manifest.json lists every other file with its size and sha256.
Each file is uploaded under its store path, so atlas/v1/current.json becomes the object
atlas/v1/current.json, which is where the Worker reads it (wiki/src/atlas.ts, ATLAS_PREFIX).

What a run does, in order:

  1. Verify every file of the store against manifest.json, then run the builder's --check on
     the store directory with the builder arguments given after "--", and refuse unless it
     reports this store as matching. The store is verified again after the check.
  2. List the objects under the store's prefix (atlas/v1/) in the bucket and print the plan:
     every object it will upload, in order, and every object it will delete. A dry run (the
     default) stops here, having only read the bucket.
  3. With --execute: upload every object except current.json, things referred to before what
     refers to them (universes, records and reader pages, then articles, index shards and
     manifest.json), each checked by its ETag, which for a single-part upload is the MD5 of
     the bytes (Content-MD5 and the signed payload hash make R2 check them on arrival too).
  4. Upload current.json, which publishes the build.
  5. Delete the objects under atlas/v1/ that the new manifest does not list. Nothing outside
     that prefix is ever deleted, and nothing is deleted before current.json is in place.
  6. List atlas/v1/ again and check every object against manifest.json: nothing missing,
     nothing extra, each size and ETag, and the sha256 of each object's bytes read back.

--rollback-to <dir> re-publishes a previous build the same way, check included. By each
current.json's built_at, --store refuses a build older than the live one and --rollback-to
one newer than it, so an old directory is never published by mistake.

current.json is the store's publish switch in the atlas reader's Worker (wiki/src/atlas.ts,
loadCurrent), which this script needs, as it needs that change's builder for its default --builder
(UPLOAD.md, "What you need"): without it every atlas route but the feedback export answers 503
"atlas store has no published build" and no article page shows a box, whatever else the bucket
holds; with it, every route reads its objects by key. Each Worker isolate keeps what it read of
current.json, its absence included, for a minute, so deleting current.json takes every atlas route
to 503 within about a minute, and a first publish shows nothing until current.json lands. --unpublish removes everything: it deletes
every object under atlas/v1/, current.json first, then lists the prefix again and checks that
nothing is left. It needs no store and runs no builder.

Every listing is held to the same rules, whether it plans the run or feeds its deletes: one that
returns a key outside atlas/v1/ is not acted on, and a key the script cannot read for certain
(parse_listing) or that a server could read as another key (deletable) is never deleted.

Credentials: an R2 API token's S3 access key id and secret, from the environment variables
WIKILEAN_ATLAS_R2_ACCESS_KEY_ID and WIKILEAN_ATLAS_R2_SECRET_ACCESS_KEY or, with
--credentials-file, from a file only its owner can read (mode 0600). Never from argv; they
are never printed, and the builder's check runs without them in its environment.

Exit codes: 0 done (or the dry run's plan printed); 1 the bucket did not match after writing
began (an upload's ETag, the store changing underneath, the final check, or objects left after
--unpublish); 2 refused before writing anything; 3 a request failed after retries, or an
internal error; 130 interrupted. Every failure says either that nothing was written or what
the bucket holds and what the site's readers see.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import http.client
import json
import os
import re
import shlex
import ssl
import stat
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence, TextIO
from urllib.parse import quote, unquote_plus, urlsplit

HERE = Path(__file__).resolve().parent
SCRIPT = Path(__file__).resolve()
DEFAULT_BUILDER = HERE / "build_store.py"

PREFIX = "atlas/v1/"  # build_store.STORE_PREFIX plus "/", and the Worker's ATLAS_PREFIX
MANIFEST_KEY = PREFIX + "manifest.json"
CURRENT_KEY = PREFIX + "current.json"
MANIFEST_SCHEMA = "wikilean.atlas.manifest/1"
CURRENT_SCHEMA = "wikilean.atlas.current/1"

ACCESS_KEY_VAR = "WIKILEAN_ATLAS_R2_ACCESS_KEY_ID"
SECRET_KEY_VAR = "WIKILEAN_ATLAS_R2_SECRET_ACCESS_KEY"
CREDENTIAL_VARS = (ACCESS_KEY_VAR, SECRET_KEY_VAR)

REGION = "auto"  # R2's region for SigV4 (an empty value and us-east-1 alias to it)
SERVICE = "s3"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()

EXIT_OK, EXIT_MISMATCH, EXIT_REFUSED, EXIT_FAILED, EXIT_INTERRUPTED = 0, 1, 2, 3, 130

TIMEOUT_SECONDS = 120
ATTEMPTS = 3
BACKOFF_SECONDS = (1.0, 4.0)
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_RESPONSE_BYTES = 256 * 1024 * 1024
MAX_LIST_PAGES = 10_000
MAX_CREDENTIALS_FILE_BYTES = 64 * 1024
CHECK_LINES_SHOWN = 40

# A store key: the prefix, then segments that are never empty and never start with a dot, so
# no ".", "..", hidden file or doubled slash can reach a request path.
STORE_KEY_RE = re.compile(r"^atlas/v1/(?:[A-Za-z0-9_-][A-Za-z0-9._-]*/)*[A-Za-z0-9_-][A-Za-z0-9._-]*$")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
DIGITS_RE = re.compile(r"^[0-9]+$")
ACCOUNT_ID_RE = re.compile(r"^[0-9a-f]{32}$")
BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")  # R2's bucket-name rules
CREDENTIAL_RE = re.compile(r"^[A-Za-z0-9+/=._~-]{16,256}$")
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
CHECK_LINE_RE = re.compile(r"^atlas --check: (?P<out>.+) matches \((?P<count>[0-9]+) files\)$")

# The builder flags this script supplies itself, and --help, which would exit 0 without checking.
# argparse accepts any unambiguous abbreviation, so a prefix of one of these is refused too.
RESERVED_BUILDER_FLAGS = ("--out", "--check", "--replace", "--help")
# This script's own options: after "--" they would go to the builder by mistake.
OWN_OPTIONS = ("--store", "--rollback-to", "--unpublish", "--bucket", "--account-id", "--endpoint",
               "--credentials-file", "--builder", "--execute")

# Upload order: nothing goes up before what it refers to. Universes, records and reader pages
# first, other files next, then the articles that name them, the index shards that list the
# articles, and manifest.json; current.json, which names the shards and the universe, goes last.
UPLOAD_RANK = {"universes": 0, "records": 1, "reader": 2, "articles": 4, "index": 5}
OTHER_RANK, MANIFEST_RANK, CURRENT_RANK = 3, 6, 7

CONTENT_TYPES = {".json": "application/json", ".jsonl": "application/x-ndjson",
                 ".html": "text/html; charset=utf-8"}

# While current.json is in place (the publish switch), what the site serves from the other objects,
# each read by its key: everything but current.json's own view and the dashboard's totals.
DIRECT_READERS = "reader pages, the dashboard's article list, article boxes and every JSON view but /api/atlas/current"


class Refused(Exception):
    """Refused before anything was written (exit 2)."""


class Mismatch(Exception):
    """The bucket, or the store under it, did not match after writing began (exit 1)."""


class RemoteError(Exception):
    """A request to the bucket failed after its retries (exit 3)."""


class Interrupted(Exception):
    """Ctrl-C after writing began (exit 130); the message says what state the bucket was left in."""


# ---- output ----------------------------------------------------------------------------------


class Console:
    """Every line the script prints goes through here, with the credentials redacted."""

    def __init__(self, out: TextIO, err: TextIO) -> None:
        self._out, self._err = out, err
        self._hidden: list[str] = []

    def hide(self, *values: str) -> None:
        self._hidden.extend(value for value in values if value)

    def clean(self, text: str) -> str:
        for value in self._hidden:
            text = text.replace(value, "[redacted]")
        return text

    def say(self, text: str = "") -> None:
        print(self.clean(text), file=self._out, flush=True)

    def warn(self, text: str) -> None:
        print(self.clean(text), file=self._err, flush=True)


def shown(text: str, limit: int = 300) -> str:
    """Text from the bucket or the builder, safe to print: every non-printable character (control,
    format and bidi codes) escaped, so nothing it holds can drive the terminal; length capped."""
    out = "".join(ch if ch.isprintable() else ascii(ch)[1:-1] for ch in text)
    return out if len(out) <= limit else out[:limit] + "..."


def size_of(count: int) -> str:
    return f"{count:,} bytes"


def internal_error(exc: BaseException) -> str:
    return f"internal error ({type(exc).__name__}: {exc})"


# ---- hashing and SigV4 ---------------------------------------------------------------------------


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def md5_hex(data: bytes) -> str:
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


def uri_encode(text: str, safe: str = "") -> str:
    """SigV4's encoding: every byte but A-Z a-z 0-9 - _ . ~ as %XY (uppercase); "/" too unless safe."""
    return quote(text, safe=safe)


def canonical_query(pairs: Sequence[tuple[str, str]]) -> str:
    encoded = sorted((uri_encode(name), uri_encode(value)) for name, value in pairs)
    return "&".join(f"{name}={value}" for name, value in encoded)


def signing_key(secret: str, date: str, region: str, service: str) -> bytes:
    key = hmac.new(("AWS4" + secret).encode("utf-8"), date.encode("ascii"), hashlib.sha256).digest()
    for part in (region, service, "aws4_request"):
        key = hmac.new(key, part.encode("ascii"), hashlib.sha256).digest()
    return key


def authorization(method: str, canonical_uri: str, query: str, headers: Mapping[str, str],
                  payload_sha256: str, access_key_id: str, secret: str, *,
                  region: str = REGION, service: str = SERVICE) -> str:
    """The Authorization header value for one request: AWS4-HMAC-SHA256, every given header signed.

    canonical_uri is the request path exactly as sent (already encoded; S3 never encodes it
    twice), query the canonical query string, and headers must hold x-amz-date.
    """
    values = {name.lower(): " ".join(str(value).split()) for name, value in headers.items()}
    names = sorted(values)
    canonical_headers = "".join(f"{name}:{values[name]}\n" for name in names)
    signed = ";".join(names)
    request = "\n".join([method, canonical_uri, query, canonical_headers, signed, payload_sha256])
    stamp = values["x-amz-date"]
    scope = f"{stamp[:8]}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", stamp, scope, sha256_hex(request.encode("utf-8"))])
    signature = hmac.new(signing_key(secret, stamp[:8], region, service), to_sign.encode("utf-8"),
                         hashlib.sha256).hexdigest()
    return f"AWS4-HMAC-SHA256 Credential={access_key_id}/{scope}, SignedHeaders={signed}, Signature={signature}"


def amz_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# ---- credentials, endpoint, bucket name -------------------------------------------------------------


@dataclass(frozen=True)
class Credentials:
    access_key_id: str = field(repr=False)
    secret_access_key: str = field(repr=False)
    source: str = ""


def make_credentials(values: Mapping[str, str], source: str) -> Credentials:
    for name in CREDENTIAL_VARS:
        if not values.get(name):
            raise Refused(f"{name} is not set in {source}. Set both {ACCESS_KEY_VAR} and {SECRET_KEY_VAR} "
                          "(the R2 token's S3 credentials), or pass --credentials-file with a 0600 file "
                          "holding them; never on the command line")
        if not CREDENTIAL_RE.match(values[name]):
            raise Refused(f"{name} in {source} is not an R2 credential (16 to 256 characters of "
                          "A-Z a-z 0-9 + / = . _ ~ -, no spaces or quotes)")
    return Credentials(values[ACCESS_KEY_VAR], values[SECRET_KEY_VAR], source)


def credentials_from_file(path: Path) -> Credentials:
    """NAME=value lines (blank lines, # comments and a leading `export` allowed); never echoed."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise Refused(f"cannot open the credentials file {path} ({exc.strerror}; it must be a plain "
                      "file, not a symbolic link)") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise Refused(f"the credentials file {path} is not a regular file")
        if info.st_uid != os.getuid():
            raise Refused(f"the credentials file {path} belongs to another user")
        if info.st_mode & 0o077:
            raise Refused(f"the credentials file {path} can be read by others (mode "
                          f"{stat.S_IMODE(info.st_mode):04o}); make it 0600 with chmod 600")
        data = b""
        while len(data) <= MAX_CREDENTIALS_FILE_BYTES:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            data += chunk
    finally:
        os.close(fd)
    if len(data) > MAX_CREDENTIALS_FILE_BYTES:
        raise Refused(f"the credentials file {path} is larger than {MAX_CREDENTIALS_FILE_BYTES} bytes")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise Refused(f"the credentials file {path} is not UTF-8 text") from None
    values: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), 1):
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        if entry.startswith("export "):
            entry = entry[len("export "):].lstrip()
        name, equals, value = entry.partition("=")
        name, value = name.strip(), value.strip()
        if not equals or not NAME_RE.match(name):
            raise Refused(f"line {number} of the credentials file {path} is not NAME=value")
        if name not in CREDENTIAL_VARS:
            raise Refused(f"line {number} of the credentials file {path} sets a name this script does not "
                          f"read; it reads {ACCESS_KEY_VAR} and {SECRET_KEY_VAR} only")
        if name in values:
            raise Refused(f"line {number} of the credentials file {path} sets {name} a second time")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[name] = value
    return make_credentials(values, f"the credentials file {path}")


def load_credentials(path: Path | None, environ: Mapping[str, str]) -> Credentials:
    if path is not None:
        return credentials_from_file(path)
    return make_credentials({name: environ.get(name, "").strip() for name in CREDENTIAL_VARS},
                            "the environment")


@dataclass(frozen=True)
class Endpoint:
    scheme: str
    host: str
    port: int
    host_header: str

    @property
    def origin(self) -> str:
        return f"{self.scheme}://{self.host_header}"


def r2_endpoint(account_id: str) -> Endpoint:
    if not ACCOUNT_ID_RE.match(account_id):
        raise Refused("--account-id must be a Cloudflare account id: 32 lowercase hex characters")
    host = f"{account_id}.r2.cloudflarestorage.com"
    return Endpoint("https", host, 443, host)


def loopback_endpoint(url: str) -> Endpoint:
    """A test server on this machine only, so a credential can never be sent anywhere but R2."""
    refusal = Refused("--endpoint takes only a loopback test server, http://127.0.0.1:<port>; "
                      "the R2 endpoint comes from --account-id")
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError:
        raise refusal from None
    if (parts.scheme != "http" or parts.hostname not in ("127.0.0.1", "localhost", "::1") or port is None
            or parts.path not in ("", "/") or parts.query or parts.fragment or parts.username is not None
            or parts.password is not None):
        raise refusal
    return Endpoint("http", parts.hostname, port, parts.netloc)


def tls_context() -> tuple[ssl.SSLContext, str]:
    """A verifying TLS context, and the name of the trust store behind it."""
    try:
        import certifi  # noqa: PLC0415 - optional
        context, source = ssl.create_default_context(cafile=certifi.where()), "certifi"
    except ImportError:
        try:
            import truststore  # noqa: PLC0415 - optional
            context, source = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT), "truststore (the system store)"
        except ImportError:
            context, source = ssl.create_default_context(), "Python's platform default"
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
        raise Refused("the TLS context does not verify certificates and host names")
    return context, source


# ---- the store on disk ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Entry:
    size: int
    sha256: str
    md5: str


@dataclass
class Store:
    root: Path
    manifest_sha256: str
    objects: dict[str, Entry]  # every object the bucket should hold: the manifest's files and manifest.json
    current: dict

    def read(self, key: str) -> bytes:
        """The object's bytes, read again and checked against the manifest the plan was made from.

        Called while publishing, so every failure is a Mismatch: writes may already have happened.
        """
        entry = self.objects[key]
        try:
            data = read_plain_file(self.root / key)
        except (OSError, Refused) as exc:
            raise Mismatch(f"{key} can no longer be read from the store ({exc})") from None
        if len(data) != entry.size or sha256_hex(data) != entry.sha256:
            raise Mismatch(f"{key} changed on disk after the check: it no longer matches manifest.json")
        return data

    @property
    def total_bytes(self) -> int:
        return sum(entry.size for entry in self.objects.values())


def read_plain_file(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Refused(f"{path} is not a regular file")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            return handle.read()
    finally:
        os.close(fd)


def _strict_json(data: bytes, what: str) -> object:
    def no_duplicates(pairs: list[tuple[str, object]]) -> dict:
        out: dict = {}
        for key, value in pairs:
            if key in out:
                raise ValueError(f"duplicate key {key!r}")
            out[key] = value
        return out

    def no_constants(name: str) -> object:
        raise ValueError(f"non-finite number {name}")

    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=no_duplicates, parse_constant=no_constants)
    except (UnicodeDecodeError, ValueError) as exc:
        raise Refused(f"{what} is not valid JSON ({exc})") from None


def load_store(root: Path) -> Store:
    """Read the store and verify every file against manifest.json, or refuse with every problem found."""
    try:
        return _load_store(root)
    except OSError as exc:
        raise Refused(f"cannot read the store {root}: {exc.strerror or exc} ({exc.filename or root})") from None


def _load_store(root: Path) -> Store:
    if root.is_symlink() or not root.is_dir():
        raise Refused(f"{root} is not a store directory (the --out of build_store.py)")
    on_disk: set[str] = set()
    problems: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in sorted(dirnames + filenames):
            path = Path(dirpath) / name
            relative = path.relative_to(root).as_posix()
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                problems.append(f"{relative} is a symbolic link; a store holds plain files only")
            elif name in filenames:
                if stat.S_ISREG(mode):
                    on_disk.add(relative)
                else:
                    problems.append(f"{relative} is not a regular file")
    if problems:
        raise Refused("the store directory is not a plain tree of files:\n  " + "\n  ".join(problems))
    foreign = sorted(path for path in on_disk if not path.startswith(PREFIX))
    if foreign:
        raise Refused(f"{root} holds more than an atlas store: {', '.join(foreign[:5])}"
                      f"{' ...' if len(foreign) > 5 else ''}")
    if MANIFEST_KEY not in on_disk:
        raise Refused(f"{root} has no {MANIFEST_KEY}; build the store with build_store.py first")
    manifest_bytes = read_plain_file(root / MANIFEST_KEY)
    manifest = _strict_json(manifest_bytes, MANIFEST_KEY)
    if not isinstance(manifest, dict) or manifest.get("schema") != MANIFEST_SCHEMA:
        raise Refused(f"{MANIFEST_KEY} is not a {MANIFEST_SCHEMA} manifest")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise Refused(f"{MANIFEST_KEY} has no files map")
    listed: dict[str, tuple[int, str]] = {}
    for key, value in files.items():
        if not isinstance(key, str) or not STORE_KEY_RE.match(key):
            problems.append(f"manifest key {shown(str(key))} is not a store path (atlas/v1/ then plain segments)")
        elif key == MANIFEST_KEY:
            problems.append(f"{MANIFEST_KEY} lists itself")
        elif (not isinstance(value, dict) or set(value) != {"bytes", "sha256"}
              or type(value["bytes"]) is not int or value["bytes"] < 0
              or not isinstance(value["sha256"], str) or not HEX64_RE.match(value["sha256"])):
            problems.append(f"manifest entry for {key} is not {{bytes, sha256}}")
        else:
            listed[key] = (value["bytes"], value["sha256"])
    if CURRENT_KEY not in files:
        problems.append(f"{MANIFEST_KEY} does not list {CURRENT_KEY}")
    for key in sorted(on_disk - set(files) - {MANIFEST_KEY}):
        problems.append(f"{key} is on disk but not listed in {MANIFEST_KEY}")
    objects: dict[str, Entry] = {}
    for key, (size, digest) in sorted(listed.items()):
        if key not in on_disk:
            problems.append(f"{key} is listed in {MANIFEST_KEY} but missing")
            continue
        data = read_plain_file(root / key)
        if len(data) != size:
            problems.append(f"{key} is {size_of(len(data))}; {MANIFEST_KEY} says {size_of(size)}")
        elif sha256_hex(data) != digest:
            problems.append(f"{key} has sha256 {sha256_hex(data)}; {MANIFEST_KEY} says {digest}")
        else:
            objects[key] = Entry(size, digest, md5_hex(data))
    if problems:
        raise Refused("the store does not match its manifest:\n  " + "\n  ".join(problems))
    current = _strict_json(read_plain_file(root / CURRENT_KEY), CURRENT_KEY)
    if not isinstance(current, dict) or current.get("schema") != CURRENT_SCHEMA:
        raise Refused(f"{CURRENT_KEY} is not {CURRENT_SCHEMA}")
    manifest_sha256 = sha256_hex(manifest_bytes)
    objects[MANIFEST_KEY] = Entry(len(manifest_bytes), manifest_sha256, md5_hex(manifest_bytes))
    return Store(root, manifest_sha256, objects, current)


def build_summary(current: object) -> str:
    """One line naming a build from its current.json, for the plan and the result."""
    if not isinstance(current, dict) or current.get("schema") != CURRENT_SCHEMA:
        return "not an atlas build (unreadable current.json)"
    parts = [f"campaign {shown(str(current.get('campaign')), 80)}", f"built {shown(str(current.get('built_at')), 40)}"]
    record_set = current.get("record_set")
    if isinstance(record_set, dict) and isinstance(record_set.get("seal_sha256"), str):
        parts.append(f"record set {shown(record_set['seal_sha256'], 64)}")
    index = current.get("index")
    if isinstance(index, dict) and type(index.get("entries")) is int:
        parts.append(f"{index['entries']} articles")
    return ", ".join(parts)


# ---- the builder's check -------------------------------------------------------------------------


def reserved_builder_flag(token: str) -> bool:
    if token == "-h":
        return True
    name = token.split("=", 1)[0]
    return name.startswith("--") and len(name) > 2 and any(flag.startswith(name) for flag in RESERVED_BUILDER_FLAGS)


def child_environment(environ: Mapping[str, str], credentials: Credentials) -> dict[str, str]:
    """The builder's environment: the caller's, without the credentials under any name, and with
    bytecode writes off (as -B is for the builder itself), so that the check leaves no __pycache__
    in whichever checkout holds the builder, whatever the caller's environment."""
    secrets = (credentials.access_key_id, credentials.secret_access_key)
    env = {name: value for name, value in environ.items()
           if name not in CREDENTIAL_VARS and not any(secret in value for secret in secrets)}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def run_check(builder: Path, builder_args: Sequence[str], store: Store, environ: Mapping[str, str],
              credentials: Credentials, console: Console) -> str:
    """Run the builder's --check on the store, or refuse; returns the builder's sha256."""
    command = [sys.executable, "-B", str(builder), *builder_args, "--out", str(store.root), "--check"]
    console.say(f"check     {shlex.join(command)}")
    try:
        builder_sha256 = sha256_hex(read_plain_file(builder))
    except OSError as exc:
        raise Refused(f"cannot read the builder {builder} ({exc.strerror or exc})") from None
    console.say(f"          builder sha256 {builder_sha256}")
    result = subprocess.run(command, env=child_environment(environ, credentials), capture_output=True,
                            text=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL)
    lines = [line for line in (result.stdout + result.stderr).splitlines() if line.strip()]
    for line in lines[:CHECK_LINES_SHOWN]:
        console.say(f"          | {shown(line, 1000)}")
    if len(lines) > CHECK_LINES_SHOWN:
        console.say(f"          | ... {len(lines) - CHECK_LINES_SHOWN} more lines")
    if result.returncode != 0:
        meaning = {1: "the store differs from what the builder makes of its inputs",
                   2: "the builder refused its arguments or the record set",
                   3: "the builder failed"}.get(result.returncode, "unexpected exit status")
        raise Refused(f"the builder's --check failed with exit {result.returncode} ({meaning}). Pass, after --, "
                      "the same builder arguments that built this store")
    # Exit 0 alone could be a builder that did not check (--help, a stub): require its report of this store.
    reported = [CHECK_LINE_RE.match(line) for line in result.stdout.splitlines()]
    if not any(match and match["out"] == str(store.root) and int(match["count"]) == len(store.objects)
               for match in reported):
        raise Refused(f"the builder exited 0 but did not report {store.root} as matching all "
                      f"{len(store.objects)} files")
    return builder_sha256


# ---- the bucket -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class RemoteObject:
    size: int
    etag: str  # lowercase, unquoted; "" when the listing gave none


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict[str, str]
    body: bytes


def etag_value(raw: str) -> str:
    value = raw.strip()
    if value.startswith("W/"):
        value = value[2:]
    return value.strip('"').lower()


def describe_error(status: int, body: bytes) -> str:
    """HTTP status plus the S3 error's Code and Message only: never the rest of the body."""
    code = message = ""
    try:
        root = ET.fromstring(body) if body else None
    except ET.ParseError:
        root = None
    if root is not None:
        for child in root:
            name = child.tag.rsplit("}", 1)[-1]
            if name == "Code":
                code = child.text or ""
            elif name == "Message":
                message = child.text or ""
    detail = ": ".join(part for part in (shown(code, 80), shown(message, 200)) if part)
    return f"HTTP {status}" + (f" {detail}" if detail else "")


def parse_listing(body: bytes) -> tuple[list[tuple[str, int, str]], bool, str | None, str]:
    """ListObjectsV2's answer: its objects, whether it is truncated, the next token, and the key
    encoding it declared ("url", or "" for none).

    list() always asks for encoding-type=url, as boto3 does, so that a key holding a character XML
    cannot carry still lists. Keys are decoded when the answer says EncodingType url. An answer that
    says nothing leaves it open whether its keys were encoded (Cloudflare documents encoding-type as
    implemented, not what R2 answers). A key that reads the same either way is taken as listed; one
    that reads differently decoded could name either of two keys, so the listing is refused rather
    than acted on.
    """
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise RemoteError(f"the bucket's listing is not XML ({exc})") from None
    if root.tag.rsplit("}", 1)[-1] != "ListBucketResult":
        raise RemoteError("the bucket's listing is not a ListBucketResult")
    objects: list[tuple[str, int, str]] = []
    truncated, token, encoding = False, None, ""
    for child in root:
        name = child.tag.rsplit("}", 1)[-1]
        if name == "Contents":
            fields = {element.tag.rsplit("}", 1)[-1]: element.text or "" for element in child}
            key, size = fields.get("Key"), fields.get("Size", "")
            if not key or not DIGITS_RE.match(size):
                raise RemoteError("an entry of the bucket's listing has no Key or Size")
            objects.append((key, int(size), etag_value(fields.get("ETag", ""))))
        elif name == "IsTruncated":
            truncated = (child.text or "").strip().lower() == "true"
        elif name == "NextContinuationToken":
            token = (child.text or "").strip() or None
        elif name == "EncodingType":
            encoding = (child.text or "").strip()
    if encoding == "url":
        objects = [(unquote_plus(key), size, etag) for key, size, etag in objects]
    elif encoding:
        raise RemoteError(f"the bucket's listing uses an unknown key encoding {shown(encoding, 40)}")
    else:
        unclear = [key for key, _size, _etag in objects if unquote_plus(key) != key]
        if unclear:
            raise RemoteError(f"the bucket's listing does not say whether its keys are URL-encoded (it has no "
                              f"EncodingType, though encoding-type=url was asked for), and {shown(unclear[0])}"
                              f"{' and others' if len(unclear) > 1 else ''} names a different key decoded than "
                              "as listed; refusing to act on a listing it cannot read for certain")
    return objects, truncated, token, encoding


class Bucket:
    """Path-style S3 requests to one bucket, signed with SigV4, retried on network errors and 429/5xx."""

    def __init__(self, endpoint: Endpoint, name: str, credentials: Credentials, *,
                 sleep: Callable[[float], None] = time.sleep, tls: ssl.SSLContext | None = None) -> None:
        self.endpoint, self.name = endpoint, name
        self._credentials = credentials
        self._sleep = sleep
        self._tls = tls
        self._connection: http.client.HTTPConnection | None = None
        self.listing_encoding = ""  # what the last listing declared: "url", or "" when any page said nothing

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def _connect(self) -> http.client.HTTPConnection:
        if self._connection is None:
            if self.endpoint.scheme == "https":
                self._connection = http.client.HTTPSConnection(self.endpoint.host, self.endpoint.port,
                                                               timeout=TIMEOUT_SECONDS, context=self._tls)
            else:
                self._connection = http.client.HTTPConnection(self.endpoint.host, self.endpoint.port,
                                                              timeout=TIMEOUT_SECONDS)
        return self._connection

    def path(self, key: str | None) -> str:
        bucket = "/" + uri_encode(self.name)
        return bucket if key is None else f"{bucket}/{uri_encode(key, safe='/')}"

    def _exchange(self, method: str, target: str, headers: Mapping[str, str], body: bytes) -> Response:
        connection = self._connect()
        connection.putrequest(method, target, skip_host=True, skip_accept_encoding=True)
        for name, value in headers.items():
            connection.putheader(name, value)
        if body or method == "PUT":
            connection.putheader("content-length", str(len(body)))
        connection.endheaders(body or None)
        response = connection.getresponse()
        data = response.read(MAX_RESPONSE_BYTES + 1)
        if len(data) > MAX_RESPONSE_BYTES:
            self.close()
            raise RemoteError(f"{method} answered with more than {MAX_RESPONSE_BYTES} bytes")
        reply = Response(response.status, {name.lower(): value for name, value in response.getheaders()}, data)
        if response.will_close:
            self.close()
        return reply

    def request(self, method: str, key: str | None, *, what: str, query: Sequence[tuple[str, str]] = (),
                body: bytes = b"", headers: Mapping[str, str] | None = None) -> Response:
        path = self.path(key)
        query_string = canonical_query(query)
        target = path + (f"?{query_string}" if query_string else "")
        payload = sha256_hex(body)
        failure = ""
        for attempt in range(1, ATTEMPTS + 1):
            send = {"host": self.endpoint.host_header, "x-amz-content-sha256": payload, "x-amz-date": amz_now(),
                    **(headers or {})}
            send["authorization"] = authorization(method, path, query_string, send, payload,
                                                  self._credentials.access_key_id,
                                                  self._credentials.secret_access_key)
            try:
                response = self._exchange(method, target, send, body)
            except (OSError, http.client.HTTPException) as exc:
                self.close()
                failure = f"{type(exc).__name__}: {exc}"
            else:
                if response.status not in RETRY_STATUSES:
                    return response
                failure = describe_error(response.status, response.body)
            if attempt < ATTEMPTS:
                self._sleep(BACKOFF_SECONDS[attempt - 1])
        hint = ("; this Python's TLS trust store could not verify R2's certificate: install certifi into it"
                if "CERTIFICATE_VERIFY_FAILED" in failure else "")
        raise RemoteError(f"{what} failed after {ATTEMPTS} attempts ({failure}){hint}")

    def put(self, key: str, data: bytes) -> str:
        """Upload one object in a single part; returns the ETag the bucket answered with."""
        headers = {"content-md5": base64.b64encode(hashlib.md5(data, usedforsecurity=False).digest()).decode("ascii"),
                   "content-type": CONTENT_TYPES.get(Path(key).suffix, "application/octet-stream")}
        response = self.request("PUT", key, what=f"PUT {key}", body=data, headers=headers)
        if response.status != 200:
            raise RemoteError(f"PUT {key}: {describe_error(response.status, response.body)}")
        return etag_value(response.headers.get("etag", ""))

    def get(self, key: str) -> bytes | None:
        response = self.request("GET", key, what=f"GET {shown(key)}")
        if response.status == 404:
            return None
        if response.status != 200:
            raise RemoteError(f"GET {shown(key)}: {describe_error(response.status, response.body)}")
        return response.body

    def delete(self, key: str) -> None:
        response = self.request("DELETE", key, what=f"DELETE {shown(key)}")
        if response.status not in (200, 204, 404):
            raise RemoteError(f"DELETE {shown(key)}: {describe_error(response.status, response.body)}")

    def list(self, prefix: str) -> dict[str, RemoteObject]:
        """Every object whose key starts with prefix (ListObjectsV2, every page)."""
        found: dict[str, RemoteObject] = {}
        token: str | None = None
        encodings: set[str] = set()
        for _ in range(MAX_LIST_PAGES):
            query = [("list-type", "2"), ("encoding-type", "url"), ("max-keys", "1000"), ("prefix", prefix)]
            if token:
                query.append(("continuation-token", token))
            response = self.request("GET", None, what=f"listing {prefix}", query=query)
            if response.status != 200:
                raise RemoteError(f"listing {prefix}: {describe_error(response.status, response.body)}")
            objects, truncated, next_token, encoding = parse_listing(response.body)
            encodings.add(encoding)
            for key, size, etag in objects:
                if key in found:
                    raise RemoteError(f"the bucket's listing gave {shown(key)} twice")
                found[key] = RemoteObject(size, etag)
            if not truncated:
                self.listing_encoding = "url" if encodings == {"url"} else ""
                return found
            if not next_token or next_token == token:
                raise RemoteError("the bucket's listing is truncated but gives no new continuation token")
            token = next_token
        raise RemoteError(f"the bucket's listing did not end after {MAX_LIST_PAGES} pages")


def open_bucket(endpoint: Endpoint, name: str, credentials: Credentials, sleep: Callable[[float], None],
                console: Console) -> Bucket:
    tls, trust = (tls_context() if endpoint.scheme == "https" else (None, "none (loopback test server)"))
    console.say(f"bucket    {name} at {endpoint.origin}, prefix {PREFIX} (TLS trust: {trust})")
    return Bucket(endpoint, name, credentials, sleep=sleep, tls=tls)


def list_prefix(bucket: Bucket, console: Console) -> dict[str, RemoteObject]:
    """The planning listing, and one line saying how its keys were read (the first dry run shows R2's way)."""
    remote = bucket.list(PREFIX)
    how = ("keys URL-encoded, as asked" if bucket.listing_encoding == "url"
           else "the answer has no EncodingType, so keys are read as listed; one that reads differently "
                "decoded is refused")
    console.say(f"listing   {len(remote)} objects under {PREFIX} ({how})")
    return remote


def describe_live(bucket: Bucket, remote: Mapping[str, RemoteObject], console: Console,
                  ours: str | None = None) -> object:
    """Print the build the bucket's current.json names; returns that current.json parsed, or None."""
    live = bucket.get(CURRENT_KEY) if CURRENT_KEY in remote else None
    if live is None:
        console.say(f"live      nothing published ({CURRENT_KEY} is absent)")
        return None
    try:
        live_current = json.loads(live.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        live_current = None
    same = ours is not None and sha256_hex(live) == ours
    console.say(f"live      {build_summary(live_current)}" + ("  (this build's current.json)" if same else ""))
    return live_current


# ---- the plan --------------------------------------------------------------------------------------


def upload_rank(key: str) -> tuple[int, str]:
    if key == CURRENT_KEY:
        return CURRENT_RANK, key
    if key == MANIFEST_KEY:
        return MANIFEST_RANK, key
    rest = key[len(PREFIX):]
    top = rest.split("/", 1)[0] if "/" in rest else ""
    return UPLOAD_RANK.get(top, OTHER_RANK), key


def delete_rank(key: str) -> tuple[int, str]:
    """Deletes run the other way: index shards before the articles they list, leaves last."""
    rank, _ = upload_rank(key) if key.startswith(PREFIX) else (OTHER_RANK, key)
    return -rank, key


def deletable(key: str) -> bool:
    """A key this script may delete: under the prefix, plain ASCII, and no "", "." or ".." segment
    a server could normalise into another key (a trailing "/" of a folder marker is allowed)."""
    if not key.startswith(PREFIX) or len(key) > 1024:
        return False
    if any(not 0x20 <= ord(ch) < 0x7F or ch == "\\" for ch in key):
        return False
    segments = key[len(PREFIX):].split("/")
    if segments[-1] == "":
        segments = segments[:-1]
    return all(segment not in ("", ".", "..") for segment in segments)


def outside_prefix(keys: Iterable[str]) -> list[str]:
    return sorted(key for key in keys if not key.startswith(PREFIX))


def first_of(keys: Sequence[str]) -> str:
    return f"{shown(keys[0])}{' and others' if len(keys) > 1 else ''}"


def refuse_foreign(remote: Mapping[str, RemoteObject]) -> None:
    """A listing of the prefix that returns keys outside it cannot be trusted, so it is not acted on."""
    outside = outside_prefix(remote)
    if outside:
        raise Refused(f"the bucket's listing of {PREFIX} returned keys outside it ({first_of(outside)}); "
                      "refusing to write to a bucket whose listing cannot be trusted")


def refuse_undeletable(keys: Sequence[str]) -> None:
    unsafe = [key for key in keys if not deletable(key)]
    if unsafe:
        raise Refused(f"the bucket holds objects under {PREFIX} that this script will not delete, because a "
                      "server could read their keys as other keys: " + ", ".join(shown(key) for key in unsafe[:5])
                      + ("..." if len(unsafe) > 5 else "") + ". Remove them by hand, then run this again")


@dataclass
class Plan:
    uploads: list[tuple[str, Entry, str]]  # (key, entry, "new" | "replaces" | "unchanged"); current.json last
    deletes: list[tuple[str, int]]         # (key, size in the bucket)
    live: bool = False                     # whether current.json was in the bucket: a build was published


def object_state(key: str, entry: Entry, remote: Mapping[str, RemoteObject]) -> str:
    found = remote.get(key)
    if found is None:
        return "new"
    return "unchanged" if found.size == entry.size and found.etag == entry.md5 else "replaces"


def make_plan(store: Store, remote: Mapping[str, RemoteObject]) -> Plan:
    refuse_foreign(remote)
    order = sorted(store.objects, key=upload_rank)
    uploads = [(key, store.objects[key], object_state(key, store.objects[key], remote)) for key in order]
    stale = sorted(set(remote) - set(store.objects), key=delete_rank)
    refuse_undeletable(stale)
    return Plan(uploads, [(key, remote[key].size) for key in stale], live=CURRENT_KEY in remote)


def print_plan(plan: Plan, console: Console) -> None:
    before = plan.uploads[:-1]
    counts = {state: sum(1 for _, _, s in before if s == state) for state in ("new", "replaces", "unchanged")}
    console.say(f"plan      {len(before)} uploads before current.json ({counts['new']} new, {counts['replaces']} "
                f"replacing other bytes, {counts['unchanged']} unchanged), then current.json, then "
                f"{len(plan.deletes)} deletes")
    for key, entry, state in plan.uploads:
        last = "  (last: it publishes the build)" if key == CURRENT_KEY else ""
        console.say(f"  upload  {key}  {size_of(entry.size)}  {state}{last}")
    for key, size in plan.deletes:
        console.say(f"  delete  {shown(key)}  {size_of(size)}  (not listed in the new manifest)")


# ---- publishing --------------------------------------------------------------------------------------


def verify_bucket(store: Store, bucket: Bucket) -> list[str]:
    """Every difference between the bucket's atlas/v1/ and the manifest; an empty list means a match."""
    listing = bucket.list(PREFIX)
    problems: list[str] = []
    for key in sorted(store.objects, key=upload_rank):
        entry = store.objects[key]
        found = listing.get(key)
        if found is None:
            problems.append(f"missing: {key} is not in the bucket")
            continue
        if found.size != entry.size:
            problems.append(f"size: {key} is {size_of(found.size)} in the bucket, "
                            f"{size_of(entry.size)} in the manifest")
        if found.etag and found.etag != entry.md5:
            problems.append(f"etag: {key} has ETag {shown(found.etag, 80)} in the bucket, not the MD5 of its bytes")
        data = bucket.get(key)
        if data is None:
            problems.append(f"missing: {key} is listed but cannot be read")
        elif sha256_hex(data) != entry.sha256:
            problems.append(f"sha256: {key} reads back as {sha256_hex(data)}; the manifest says {entry.sha256}")
    for key in sorted(set(listing) - set(store.objects)):
        if key.startswith(PREFIX):
            problems.append(f"unexpected: {shown(key)} ({size_of(listing[key].size)}) is not listed in the manifest")
        else:
            problems.append(f"listing fault: the bucket listed {shown(key)}, which is outside {PREFIX}; it is not a "
                            "store object and was not touched")
    return problems


def delete_unlisted(bucket: Bucket, keep: set[str], planned: set[str], console: Console) -> tuple[int, list[str]]:
    """Delete every object under the prefix that keep does not name, from a fresh listing held to the
    planning listing's rules. A key outside the prefix stops it before any delete: such a key is no store
    object, and a listing that returns one cannot be trusted. A key this script will not delete is left,
    and returned. Returns (deleted, left)."""
    listing = bucket.list(PREFIX)
    outside = outside_prefix(listing)
    if outside:
        raise Mismatch(f"the bucket's listing of {PREFIX} returned keys outside it ({first_of(outside)}); none of "
                       f"its keys was deleted. Keys outside {PREFIX} are not store objects: do not delete them")
    stale = sorted(set(listing) - keep, key=delete_rank)
    if set(stale) != planned:
        console.say(f"note      the bucket changed after the plan was printed: it now holds {len(stale)} objects "
                    f"under {PREFIX} to delete, not {len(planned)}; each one deleted is printed below")
    deleted, left = 0, []
    for key in stale:
        if not deletable(key):
            left.append(key)
            continue
        bucket.delete(key)
        deleted += 1
        console.say(f"delete    {shown(key)}  ok")
    return deleted, left


def publish_state(phase: str, plan: Plan, written: Sequence[str], failed: str | None) -> str:
    """What the bucket holds, and what the site's readers see, when a publish stops in phase.

    written holds the plan's state ("new", "replaces" or "unchanged") of each object written before
    current.json; failed is the key whose upload was under way, which may or may not have landed.
    current.json is the publish switch: without it every atlas route answers 503 whatever else is
    written; with it, every object written is already served by key.
    """
    others = len(plan.uploads) - 1
    perhaps = f" (and perhaps {failed}, the upload that failed)" if failed else ""
    if phase == "uploads" and not plan.live:
        return (f"Stopped before current.json. Nothing was published before this run and current.json is still "
                f"absent, so every atlas route answers 503 and no article box shows, although {len(written)} of "
                f"the new build's {others} other objects are written{perhaps}. Run again with --execute to finish, "
                "or --unpublish to remove them.")
    if phase == "uploads":
        replaced = sum(1 for state in written if state == "replaces")
        return (f"Stopped before current.json. It still names the previous build, so the atlas stays published and "
                f"/atlas's totals are that build's, but {len(written)} of the new build's {others} other objects are "
                f"written{perhaps}, {replaced} of them replacing objects of the previous build. "
                f"{DIRECT_READERS[0].upper()}{DIRECT_READERS[1:]} read objects by key, so they already show these. "
                "Run again with --execute to finish, or --rollback-to the previous build's directory.")
    if phase == "current" and not plan.live:
        return ("Stopped at current.json: every other object of the new build is written, and current.json may "
                "now name it, which publishes the build, or still be absent, so every atlas route answers 503. Run "
                "again with --execute, or --unpublish, before anything else.")
    if phase == "current":
        return ("Stopped at current.json: every other object of the new build is written, and current.json may "
                "name either build. Run again with --execute, or --rollback-to the previous build's directory, "
                "before anything else.")
    return (f"The new build is published (current.json in place and checked), but objects it no longer lists may "
            f"remain under {PREFIX}, and {DIRECT_READERS} still serve any of them. Run again with --execute to "
            "finish the deletes.")


def publish(store: Store, plan: Plan, bucket: Bucket, console: Console) -> tuple[int, list[str]]:
    """Steps 3 to 5. Returns (deleted, keys left undeleted); raises with the bucket's state on failure."""
    phase, written, failed = "uploads", [], None
    try:
        for key, entry, state in plan.uploads:
            data = store.read(key)
            if key == CURRENT_KEY:
                phase = "current"
            failed = key
            etag = bucket.put(key, data)
            if etag != entry.md5:
                raise Mismatch(f"PUT {key}: the bucket answered ETag {shown(etag, 80) or '(none)'}, not the MD5 "
                               f"{entry.md5} of the bytes sent")
            failed = None
            if key == CURRENT_KEY:
                console.say(f"put       {key}  ok: this build is now the published one")
            else:
                written.append(state)
                console.say(f"put       {key}  ok")
        phase = "deletes"
        return delete_unlisted(bucket, set(store.objects), {key for key, _size in plan.deletes}, console)
    except (Mismatch, RemoteError, KeyboardInterrupt) as exc:
        state = publish_state(phase, plan, written, failed)
        if isinstance(exc, KeyboardInterrupt):
            raise Interrupted(state) from None
        raise type(exc)(f"{exc}\n{state}") from None
    except Exception as exc:  # noqa: BLE001 - reported with the bucket's state, never as a traceback
        raise RemoteError(f"{internal_error(exc)}\n{publish_state(phase, plan, written, failed)}") from None


def build_time(current: object) -> datetime | None:
    """current.json's built_at as an aware time, or None when it has none this script can read."""
    if not isinstance(current, dict) or not isinstance(current.get("built_at"), str):
        return None
    try:
        value = datetime.fromisoformat(current["built_at"].replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo is not None else None


def check_direction(store: Store, live: object, rollback: bool, root: Path) -> None:
    """--store publishes a build at least as new as the live one, --rollback-to one no newer, so that
    pointing --store at an old directory by mistake cannot roll the atlas back."""
    new, old = build_time(store.current), build_time(live)
    if new is None or old is None:
        return
    shown_new, shown_old = shown(str(store.current["built_at"]), 40), shown(str(live["built_at"]), 40)
    if not rollback and old > new:
        raise Refused(f"the live build was built at {shown_old}, after this one ({shown_new}): publishing this "
                      f"store would roll the atlas back. To do that on purpose, use --rollback-to {root}")
    if rollback and new > old:
        raise Refused(f"this build ({shown_new}) is newer than the live one ({shown_old}), so this is not a "
                      "rollback: publish it with --store")


# ---- unpublishing ------------------------------------------------------------------------------------


# Once current.json is gone every atlas route answers 503 within about a minute (each Worker isolate
# keeps what it read of current.json for up to 60 s), whatever objects remain under the prefix.
OFFLINE = "every atlas route answers 503 within about a minute and the article boxes go"
UNPUBLISH_STATE = {
    "current": ("Stopped at current.json: it may still be in place, so the atlas may still be published, and no "
                "other object was deleted. Run --unpublish --execute again."),
    "rest": (f"current.json is gone, so {OFFLINE}, but objects remain under {PREFIX}. Run --unpublish --execute "
             "again to remove them."),
}


def unpublish_plan(remote: Mapping[str, RemoteObject]) -> list[tuple[str, int]]:
    """Every object under the prefix, current.json first and the rest in delete_rank order; refused, as
    make_plan refuses, when the listing strays outside the prefix or holds a key this script will not delete."""
    refuse_foreign(remote)
    keys = sorted(remote, key=lambda key: (key != CURRENT_KEY, delete_rank(key)))
    refuse_undeletable(keys)
    return [(key, remote[key].size) for key in keys]


def print_unpublish_plan(deletes: Sequence[tuple[str, int]], console: Console) -> None:
    if not deletes:
        console.say(f"plan      nothing to delete: {PREFIX} is empty")
        return
    first = "current.json first" if deletes[0][0] == CURRENT_KEY else f"{CURRENT_KEY} is already absent"
    console.say(f"plan      {len(deletes)} deletes, every object under {PREFIX} ({first})")
    for key, size in deletes:
        last = ("  (first: once it is gone every atlas route answers 503 within about a minute)"
                if key == CURRENT_KEY else "")
        console.say(f"  delete  {shown(key)}  {size_of(size)}{last}")


def unpublish(bucket: Bucket, deletes: Sequence[tuple[str, int]], console: Console) -> int:
    """Delete current.json, then every object a fresh listing shows under the prefix; returns how many."""
    phase, deleted = ("current" if deletes and deletes[0][0] == CURRENT_KEY else "rest"), 0
    try:
        if phase == "current":
            bucket.delete(CURRENT_KEY)
            deleted += 1
            console.say(f"delete    {CURRENT_KEY}  ok: {OFFLINE}")
            phase = "rest"
        # A key left here is one this script will not delete; the final check names it.
        more, _left = delete_unlisted(bucket, set(), {key for key, _size in deletes if key != CURRENT_KEY}, console)
        return deleted + more
    except (Mismatch, RemoteError, KeyboardInterrupt) as exc:
        if isinstance(exc, KeyboardInterrupt):
            raise Interrupted(UNPUBLISH_STATE[phase]) from None
        raise type(exc)(f"{exc}\n{UNPUBLISH_STATE[phase]}") from None
    except Exception as exc:  # noqa: BLE001 - reported with the bucket's state, never as a traceback
        raise RemoteError(f"{internal_error(exc)}\n{UNPUBLISH_STATE[phase]}") from None


def run_unpublish(bucket: Bucket, execute: bool, console: Console, progress: Progress) -> int:
    remote = list_prefix(bucket, console)
    live = describe_live(bucket, remote, console)
    deletes = unpublish_plan(remote)
    print_unpublish_plan(deletes, console)
    if not execute:
        console.say("dry run   nothing was deleted. Run the same command with --execute to take the atlas offline.")
        return EXIT_OK
    progress.wrote = True
    deleted = unpublish(bucket, deletes, console)
    unfinished = (f"current.json is gone, so {OFFLINE}, but the check that nothing is left under {PREFIX} "
                  "did not finish. Run --unpublish again, as a dry run, to see what remains.")
    try:
        remaining = bucket.list(PREFIX)
    except RemoteError as exc:
        raise RemoteError(f"{exc}\n{unfinished}") from None
    except KeyboardInterrupt:
        raise Interrupted(unfinished) from None
    except Exception as exc:  # noqa: BLE001 - reported with the bucket's state, never as a traceback
        raise RemoteError(f"{internal_error(exc)}\n{unfinished}") from None
    if remaining:
        lines = []
        for key in sorted(remaining):
            if not key.startswith(PREFIX):
                lines.append(f"listing fault: the bucket listed {shown(key)}, which is outside {PREFIX}; it is not "
                             "a store object and was not touched")
            elif deletable(key):
                lines.append(f"left: {shown(key)} ({size_of(remaining[key].size)})")
            else:
                lines.append(f"left: {shown(key)} ({size_of(remaining[key].size)}) could not be deleted safely; "
                             "remove it by hand")
        raise Mismatch(f"the bucket's listing of {PREFIX} is not empty after the deletes:\n  " + "\n  ".join(lines)
                       + "\n" + UNPUBLISH_STATE["rest"])
    console.say(f"verify    nothing is left under {PREFIX}")
    what = build_summary(live) if live is not None else "what was under " + PREFIX
    console.say(f"done      unpublished {what}; {deleted} deleted. The atlas is offline: {OFFLINE}, as each "
                "Worker isolate's copy of current.json expires")
    return EXIT_OK


# ---- the command ---------------------------------------------------------------------------------


@dataclass
class Progress:
    wrote: bool = False  # whether a write (an upload or a delete) may have reached the bucket


def rollback_command(args: argparse.Namespace, root: Path, builder: Path, builder_args: Sequence[str]) -> str:
    """The command that later rolls the atlas back to the build just published (a dry run until --execute)."""
    where = ["--endpoint", args.endpoint] if args.endpoint else ["--account-id", args.account_id]
    credentials = ["--credentials-file", str(args.credentials_file.absolute())] if args.credentials_file else []
    return shlex.join([sys.executable, str(SCRIPT), "--rollback-to", str(root), "--bucket", args.bucket, *where,
                       *credentials, "--builder", str(builder), "--", *builder_args])


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None,
         stdout: TextIO | None = None, stderr: TextIO | None = None,
         sleep: Callable[[float], None] = time.sleep) -> int:
    console = Console(stdout or sys.stdout, stderr or sys.stderr)
    environ = os.environ if environ is None else environ
    arguments = list(sys.argv[1:] if argv is None else argv)
    bucket: Bucket | None = None
    label = "atlas upload"
    progress = Progress()
    try:
        args, builder_args = parse_arguments(arguments)
        if args.unpublish:
            label = "atlas unpublish"
        elif args.rollback_to is not None:
            label = "atlas rollback"
        credentials = load_credentials(args.credentials_file, environ)
        console.hide(credentials.access_key_id, credentials.secret_access_key)
        if not BUCKET_RE.match(args.bucket):
            raise Refused("--bucket must be an R2 bucket name: 3 to 63 characters of a-z, 0-9 and -, "
                          "starting and ending with a letter or digit")
        endpoint = loopback_endpoint(args.endpoint) if args.endpoint else r2_endpoint(args.account_id)
        if args.unpublish:
            console.say(f"{label}, " + ("DELETING (--execute)" if args.execute
                                        else "dry run: reads the bucket, deletes nothing (--execute deletes)"))
            bucket = open_bucket(endpoint, args.bucket, credentials, sleep, console)
            return run_unpublish(bucket, args.execute, console, progress)

        builder = args.builder.resolve()
        if not builder.is_file():
            raise Refused(f"--builder {builder} is not a file")
        rollback = args.rollback_to is not None
        root = (args.rollback_to if rollback else args.store).absolute()
        console.say(f"{label}, " + ("PUBLISHING (--execute)" if args.execute
                                    else "dry run: reads the bucket, writes nothing (--execute publishes)"))

        store = load_store(root)
        console.say(f"store     {root}  ({len(store.objects)} objects, {size_of(store.total_bytes)})")
        console.say(f"build     {build_summary(store.current)}")
        console.say(f"          manifest.json sha256 {store.manifest_sha256}")
        console.say(f"          current.json  sha256 {store.objects[CURRENT_KEY].sha256}")
        builder_sha256 = run_check(builder, builder_args, store, environ, credentials, console)
        if load_store(root).objects != store.objects:
            raise Refused("the store changed while the builder was checking it; nothing was uploaded")

        bucket = open_bucket(endpoint, args.bucket, credentials, sleep, console)
        remote = list_prefix(bucket, console)
        live_current = describe_live(bucket, remote, console, store.objects[CURRENT_KEY].sha256)
        if rollback:
            console.say(f"rollback  the build in {root} replaces the live one")
        check_direction(store, live_current, rollback, root)
        plan = make_plan(store, remote)
        print_plan(plan, console)
        if not args.execute:
            console.say("dry run   nothing was written. Run the same command with --execute to publish.")
            return EXIT_OK

        progress.wrote = True
        deleted, left = publish(store, plan, bucket, console)
        final_check = ("The new build is published (current.json in place and checked), but the final check of "
                       "every object did not finish. Run again with --execute.")
        try:
            problems = verify_bucket(store, bucket)
        except RemoteError as exc:
            raise RemoteError(f"{exc}\n{final_check}") from None
        except KeyboardInterrupt:
            raise Interrupted(final_check) from None
        except Exception as exc:  # noqa: BLE001 - reported with the bucket's state, never as a traceback
            raise RemoteError(f"{internal_error(exc)}\n{final_check}") from None
        problems += [f"left: {shown(key)} could not be deleted safely; remove it by hand" for key in left]
        if problems:
            undo = ("roll back with --rollback-to the previous build's directory" if plan.live
                    else "remove it with --unpublish")
            raise Mismatch("the bucket does not match manifest.json after publishing:\n  " + "\n  ".join(problems)
                           + f"\nThe new current.json is in place. Run again with --execute, or {undo}.")
        console.say(f"verify    {len(store.objects)} objects under {PREFIX}: none missing, none extra, every size, "
                    "ETag and sha256 matches manifest.json")
        console.say(f"done      {'rolled back to' if rollback else 'published'} {build_summary(store.current)}; "
                    f"{len(plan.uploads)} uploaded, {deleted} deleted")
        console.say(f"          manifest.json sha256 {store.manifest_sha256}")
        console.say(f"          current.json  sha256 {store.objects[CURRENT_KEY].sha256}")
        console.say("keep      for a rollback to this build: this store directory, every record set and side file "
                    f"the builder arguments name, unchanged, and this builder (sha256 {builder_sha256}). The "
                    "rollback, a dry run until you add --execute:")
        console.say(f"          {rollback_command(args, root, builder, builder_args)}")
        return EXIT_OK
    except Refused as exc:
        console.warn(f"{label}: REFUSED, nothing was written: {exc}")
        return EXIT_REFUSED
    except Mismatch as exc:
        console.warn(f"{label}: MISMATCH: {exc}")
        return EXIT_MISMATCH
    except RemoteError as exc:
        console.warn(f"{label}: FAILED: {exc}" + ("" if progress.wrote else "\nNothing was written."))
        return EXIT_FAILED
    except Interrupted as exc:
        console.warn(f"{label}: INTERRUPTED: {exc}")
        return EXIT_INTERRUPTED
    except KeyboardInterrupt:
        console.warn(f"{label}: INTERRUPTED " + ("after writing began; see what the bucket holds with a dry run"
                                                 if progress.wrote else "before anything was written"))
        return EXIT_INTERRUPTED
    except Exception as exc:  # noqa: BLE001 - never a traceback: it could carry request details
        console.warn(f"{label}: {internal_error(exc)}; " + ("see what the bucket holds with a dry run before "
                                                             "anything else" if progress.wrote
                                                             else "nothing was written"))
        return EXIT_FAILED
    finally:
        if bucket is not None:
            bucket.close()


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        raise Refused(f"{message}\n{self.format_usage().strip()}")


def parse_arguments(arguments: list[str]) -> tuple[argparse.Namespace, list[str]]:
    """This script's own options, and the builder's arguments after the first "--"."""
    if "--" in arguments:
        split = arguments.index("--")
        own, builder_args = arguments[:split], arguments[split + 1:]
    else:
        own, builder_args = arguments, []
    parser = _Parser(prog="upload_store.py", description=__doc__.split("\n\n")[0],
                     usage="%(prog)s (--store DIR | --rollback-to DIR) --bucket NAME (--account-id ID | "
                           "--endpoint URL)\n       [--credentials-file FILE] [--builder PATH] [--execute] "
                           "-- BUILDER-ARGS...\n   or: %(prog)s --unpublish --bucket NAME (--account-id ID | "
                           "--endpoint URL)\n       [--credentials-file FILE] [--execute]",
                     epilog=f"Credentials: {ACCESS_KEY_VAR} and {SECRET_KEY_VAR} in the environment, or "
                            "--credentials-file. Everything after -- goes to build_store.py --check; "
                            "--unpublish takes nothing after --. See scripts/atlas/UPLOAD.md.")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--store", type=Path, metavar="DIR",
                        help="the store directory to publish (build_store.py --out)")
    target.add_argument("--rollback-to", type=Path, metavar="DIR",
                        help="a previous build's store directory, re-published the same way")
    target.add_argument("--unpublish", action="store_true",
                        help="take the atlas offline: delete every object under atlas/v1/, current.json first")
    parser.add_argument("--bucket", required=True, help="the R2 bucket's name")
    where = parser.add_mutually_exclusive_group(required=True)
    where.add_argument("--account-id",
                       help="the Cloudflare account id (endpoint https://<id>.r2.cloudflarestorage.com)")
    where.add_argument("--endpoint", help="tests only: a loopback S3 server, http://127.0.0.1:<port>")
    parser.add_argument("--credentials-file", type=Path, metavar="FILE",
                        help=f"a 0600 file of {ACCESS_KEY_VAR}=... and {SECRET_KEY_VAR}=... lines "
                             "(default: those environment variables)")
    parser.add_argument("--builder", type=Path, metavar="PATH",
                        help="the build_store.py that checks the store (default: the one beside this script; for "
                             "a rollback to a build an older builder made, that commit's build_store.py)")
    parser.add_argument("--execute", action="store_true",
                        help="publish (or, with --unpublish, delete); without it, a dry run that writes nothing")
    args = parser.parse_args(own)
    if args.unpublish:
        if builder_args:
            raise Refused("--unpublish runs no builder check: give nothing after --")
        if args.builder is not None:
            raise Refused("--unpublish runs no builder: leave out --builder")
        return args, builder_args
    if not builder_args:
        raise Refused("give the builder's arguments after --, exactly as they built the store but without "
                      "--out: for example -- --record-set <set-dir>")
    misplaced = [token for token in builder_args if token.split("=", 1)[0] in OWN_OPTIONS]
    if misplaced:
        raise Refused(f"{', '.join(misplaced)} after -- would go to the builder; put this script's own "
                      "options before --")
    reserved = [token for token in builder_args if reserved_builder_flag(token)]
    if reserved:
        raise Refused(f"after --, leave out {', '.join(reserved)}: this script passes --out <store> --check itself")
    if args.builder is None:
        args.builder = DEFAULT_BUILDER
    return args, builder_args


if __name__ == "__main__":
    sys.exit(main())
