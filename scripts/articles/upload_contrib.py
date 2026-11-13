#!/usr/bin/env python3
"""Put an index bundle or a work package in the contributor bucket, never replaced.

New code, an operator tool. docs/ROADMAP.md, outside contributors (Jack, 2026-11-11): the
Worker is to stream each declaration-index bundle from a private R2 bucket,
``wikilean-contrib``, to the holders of agent tokens. This uploads a bundle that
scripts/pipeline/package_index.py wrote, under keys derived from its content:

    index/v1/<manifest sha256>/MANIFEST.json
    index/v1/<manifest sha256>/<every path MANIFEST.json lists>

The sha256 of the bundle's MANIFEST.json is its identity, and a key never changes once
written: an object already there with the same size and MD5 is skipped, and one that
differs is refused, never replaced. MANIFEST.json goes up last, so a prefix that holds it
holds the whole bundle. This script deletes nothing; retiring a bundle is a state change
in the Worker's table, not a delete.

What a run does, in order:

  1. Read MANIFEST.json strictly (schema index-bundle/2) and hash every file it lists.
     A file that differs or is missing, or one in the directory the manifest does not
     list, is refused.
  2. Refuse a bundle that is not fit to hand out: a library with no licence recorded, a
     library without its licence texts (package_index.py --licence-texts), or use
     restrictions other than exactly the ones named with --acknowledge-restricted. A copy
     handed to a contributor cannot be recalled, so this is decided before the upload.
  3. List the bundle's prefix in the bucket and print the plan. A dry run (the default)
     stops here, having only read the bucket.
  4. With --execute: upload every missing object, MANIFEST.json last, each checked by the
     ETag R2 answers (for a single-part upload, the MD5 of the bytes; Content-MD5 and the
     signed payload hash make R2 check them on arrival too). Then list the prefix again
     and check every object's size and ETag.

Credentials: the contributor bucket's own R2 token, from WIKILEAN_CONTRIB_R2_ACCESS_KEY_ID
and WIKILEAN_CONTRIB_R2_SECRET_ACCESS_KEY or a 0600 --credentials-file; never from argv,
and never printed. It is not the articles bucket's token: an R2 token is scoped to
buckets, and the articles upload must not be able to overwrite a bundle.

    python3 scripts/articles/upload_contrib.py bundle <dir> --bucket wikilean-contrib \\
        --account-id <32 hex> [--credentials-file <file>] \\
        [--acknowledge-restricted <library> ...] [--execute]

``package`` uploads one work package for the claims API (Jack, 2026-11-11, "Build a
claims API"): the transport envelope publish_package.py --api writes, one JSON object
``{"schema", "run_id", "files": {<path>: <base64>}}`` in canonical form, to

    packages/v1/<envelope sha256>.json

after checking that it is canonical and that every file is the one its package.json
names, so the sha256 the Worker publishes it under is a function of the package. The
coordinator then publishes it with POST /api/packages, which refuses an envelope the
bucket does not hold at its size.

    python3 scripts/articles/upload_contrib.py package <envelope.json> --bucket wikilean-contrib \\
        --account-id <32 hex> [--credentials-file <file>] [--execute]

Exit codes: 0 done, or the dry run's plan printed; 1 the bucket did not match after
writing began; 2 refused before writing anything; 3 a request failed after retries, or an
internal error; 130 interrupted.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import re
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence, TextIO

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from upload_store import (  # noqa: E402
    BUCKET_RE, Bucket, Console, Refused, RemoteError, RemoteObject, _strict_json,
    internal_error, load_credentials, loopback_endpoint, r2_endpoint, shown, size_of,
    tls_context)

BUNDLE_SCHEMA = "wikilean.pipeline.index-bundle/2"
MANIFEST_NAME = "MANIFEST.json"
INDEX_PREFIX = "index/v1/"
ACCESS_KEY_VAR = "WIKILEAN_CONTRIB_R2_ACCESS_KEY_ID"
SECRET_KEY_VAR = "WIKILEAN_CONTRIB_R2_SECRET_ACCESS_KEY"
CREDENTIAL_VARS = (ACCESS_KEY_VAR, SECRET_KEY_VAR)
#: The Worker reads a bundle's MANIFEST.json whole, so it is kept small.
MAX_MANIFEST_BYTES = 1 << 20
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
#: One segment of a path a manifest may list. Nothing that would need URL-encoding, so a
#: path is the same string in the manifest, the bucket key and the Worker's download URL.
SEGMENT_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*$")
PACKAGE_PREFIX = "packages/v1/"
ENVELOPE_SCHEMA = "wikilean.contribute.package-transport/1"
PACKAGE_SCHEMA = "wikilean.contribute.package/1"  # scripts/pipeline/contribute/package.py
PACKAGE_JSON = "package.json"
#: The Worker's limit on a package (wiki/src/claims.ts PACKAGE_MAX_BYTES).
MAX_ENVELOPE_BYTES = 64 << 20
#: scripts/pipeline/vocab.py RUN_ID.
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
EXIT_OK, EXIT_MISMATCH, EXIT_REFUSED, EXIT_FAILED, EXIT_INTERRUPTED = 0, 1, 2, 3, 130


class Mismatch(Exception):
    """The bucket does not hold what was uploaded (exit 1)."""


@dataclass(frozen=True)
class Local:
    size: int
    sha256: str
    md5: str


@dataclass(frozen=True)
class Bundle:
    root: Path
    manifest: dict
    manifest_sha256: str
    files: dict[str, Local]  # every object to upload, MANIFEST.json included

    @property
    def prefix(self) -> str:
        return f"{INDEX_PREFIX}{self.manifest_sha256}/"

    @property
    def total_bytes(self) -> int:
        return sum(entry.size for entry in self.files.values())


def read_regular(path: Path) -> bytes:
    """A file's bytes, refusing a symbolic link or anything that is not a regular file."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise Refused(f"cannot read {path} ({exc.strerror}; it must be a regular file, not a "
                      "symbolic link)") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Refused(f"{path} is not a regular file")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            return handle.read()
    finally:
        os.close(fd)


def digest_file(path: Path) -> Local:
    """Size, sha256 and MD5 of a regular file, read in pieces (an index is hundreds of MB)."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise Refused(f"cannot read {path} ({exc.strerror}; it must be a regular file, not a "
                      "symbolic link)") from None
    sha, md5, size = hashlib.sha256(), hashlib.md5(usedforsecurity=False), 0
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Refused(f"{path} is not a regular file")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                sha.update(chunk)
                md5.update(chunk)
                size += len(chunk)
    finally:
        os.close(fd)
    return Local(size, sha.hexdigest(), md5.hexdigest())


def listed_path(rel: object) -> str:
    """A path the manifest lists, refused unless it is plain and stays inside the bundle."""
    if not isinstance(rel, str) or len(rel) > 300 or rel == MANIFEST_NAME:
        raise Refused(f"MANIFEST.json lists {shown(repr(rel))}, which is not a file path a bundle "
                      "may hold")
    if not all(SEGMENT_RE.match(segment) for segment in rel.split("/")):
        raise Refused(f"MANIFEST.json lists {shown(rel)}: every segment of a path must be letters, "
                      "digits, '.', '_' or '-', not starting with '.' or '-', so it reads the same "
                      "as a bucket key and a download URL")
    return rel


def load_bundle(root: Path, acknowledged: Sequence[str]) -> Bundle:
    """Read and check a bundle directory; refuse anything not fit to hand out."""
    root = Path(root)
    if not root.is_dir():
        raise Refused(f"{root} is not a directory")
    raw = read_regular(root / MANIFEST_NAME)
    if len(raw) > MAX_MANIFEST_BYTES:
        raise Refused(f"{MANIFEST_NAME} is {size_of(len(raw))}; the Worker reads it whole and takes "
                      f"at most {size_of(MAX_MANIFEST_BYTES)}")
    manifest = _strict_json(raw, MANIFEST_NAME)
    if not isinstance(manifest, dict) or manifest.get("schema") != BUNDLE_SCHEMA:
        found = manifest.get("schema") if isinstance(manifest, dict) else None
        raise Refused(f"{MANIFEST_NAME} is schema {shown(repr(found))}, not {BUNDLE_SCHEMA}: package "
                      "the bundle again with scripts/pipeline/package_index.py --licence-texts")
    listed = manifest.get("files")
    if not isinstance(listed, dict) or not listed:
        raise Refused(f"{MANIFEST_NAME} lists no files")
    expected: dict[str, tuple[int, str]] = {}
    for rel, entry in listed.items():
        rel = listed_path(rel)
        size = entry.get("bytes") if isinstance(entry, dict) else None
        sha = entry.get("sha256") if isinstance(entry, dict) else None
        if (not isinstance(size, int) or isinstance(size, bool) or size < 0
                or not isinstance(sha, str) or not SHA256_RE.match(sha)):
            raise Refused(f"{MANIFEST_NAME} gives {shown(rel)} no sha256 and byte count")
        expected[rel] = (size, sha)
    if manifest.get("bytes") != sum(size for size, _ in expected.values()):
        raise Refused(f"{MANIFEST_NAME} states {manifest.get('bytes')!r} bytes in all and its files "
                      f"add up to {sum(size for size, _ in expected.values())}")

    index = manifest.get("index")
    universe = manifest.get("universe")
    if (not isinstance(index, dict) or not isinstance(index.get("path"), str)
            or index["path"] not in expected or expected[index["path"]][1] != index.get("sha256")):
        raise Refused(f"{MANIFEST_NAME}'s index is not one of its files with that file's sha256")
    if not isinstance(universe, dict) or not SHA256_RE.match(str(universe.get("universe_sha256") or "")):
        raise Refused(f"{MANIFEST_NAME} names no universe_sha256")

    # Every file on disk is a listed one, so nothing rides along unvouched for.
    on_disk = set()
    for folder, dirs, names in os.walk(root):
        for name in dirs:
            if (Path(folder) / name).is_symlink():
                raise Refused(f"{Path(folder) / name} is a symbolic link; a bundle holds plain files")
        for name in names:
            on_disk.add((Path(folder) / name).relative_to(root).as_posix())
    unlisted = sorted(on_disk - set(expected) - {MANIFEST_NAME})
    missing = sorted(set(expected) - on_disk)
    if unlisted:
        raise Refused(f"{root} holds {', '.join(shown(rel) for rel in unlisted[:5])}, which "
                      f"{MANIFEST_NAME} does not list")
    if missing:
        raise Refused(f"{root} lacks {', '.join(shown(rel) for rel in missing[:5])}, which "
                      f"{MANIFEST_NAME} lists")
    files: dict[str, Local] = {}
    for rel in sorted(expected):
        local = digest_file(root / rel)
        if (local.size, local.sha256) != expected[rel]:
            raise Refused(f"{shown(rel)} does not match {MANIFEST_NAME} (it is {size_of(local.size)} "
                          f"with sha256 {local.sha256[:12]}); package the bundle again")
        files[rel] = local

    check_licences(manifest, root, set(expected), acknowledged)
    manifest_local = Local(len(raw), hashlib.sha256(raw).hexdigest(),
                           hashlib.md5(raw, usedforsecurity=False).hexdigest())
    files[MANIFEST_NAME] = manifest_local
    return Bundle(root, manifest, manifest_local.sha256, files)


def check_licences(manifest: dict, root: Path, listed: set[str], acknowledged: Sequence[str]) -> None:
    """Every library has a licence on record and its own licence texts; restrictions are acknowledged."""
    licences = manifest.get("licences")
    if not isinstance(licences, dict):
        raise Refused(f"{MANIFEST_NAME} has no licences block")
    unknown = licences.get("no_licence_recorded")
    if unknown != []:
        raise Refused(f"the universe records no licence for {shown(', '.join(map(str, unknown or ['?'])))}; "
                      "no licence recorded is not a permission, so the bundle is not handed out until "
                      "each has its SPDX id in the universe")
    document = licences.get("document")
    if not isinstance(document, str) or document not in listed:
        raise Refused(f"{MANIFEST_NAME}'s licence document is not one of its files")
    spdx = _strict_json(read_regular(root / document), str(document))
    packages = spdx.get("packages") if isinstance(spdx, dict) else None
    if not isinstance(packages, list):
        raise Refused(f"{document} lists no libraries")
    libraries = sorted(str(package.get("name")) for package in packages if isinstance(package, dict))
    texts = licences.get("texts")
    if not isinstance(texts, dict):
        texts = {}
    without = [name for name in libraries
               if not isinstance(texts.get(name), list) or not texts[name]]
    if without:
        raise Refused(f"{len(without)} librar(ies) have no licence texts in the bundle "
                      f"({', '.join(shown(name) for name in without[:6])}"
                      f"{' ...' if len(without) > 6 else ''}); package it again with "
                      "package_index.py --licence-texts, so the licences travel with every copy")
    stray = [rel for paths in texts.values() for rel in paths if rel not in listed]
    if stray or set(texts) - set(libraries):
        raise Refused(f"{MANIFEST_NAME}'s licence texts name files or libraries the bundle does not hold")
    restricted = licences.get("use_restricted")
    if not isinstance(restricted, list) or sorted(map(str, restricted)) != sorted(set(acknowledged)):
        raise Refused(
            "the libraries whose use is restricted are "
            f"{', '.join(map(str, restricted or [])) or 'none'}, and --acknowledge-restricted names "
            f"{', '.join(sorted(set(acknowledged))) or 'none'}. Name exactly the restricted ones, after "
            "reading their terms in LICENCES.md: each copy handed out carries those terms")


@dataclass(frozen=True)
class Envelope:
    path: Path
    run_id: str
    data: bytes
    local: Local
    files: int

    @property
    def key(self) -> str:
        return f"{PACKAGE_PREFIX}{self.local.sha256}.json"


def load_envelope(path: Path) -> Envelope:
    """A work package's transport envelope, refused unless canonical and true to its package.json."""
    data = read_regular(path)
    if not data or len(data) > MAX_ENVELOPE_BYTES:
        raise Refused(f"{path} is {size_of(len(data))}; a package's envelope holds between 1 byte and "
                      f"{size_of(MAX_ENVELOPE_BYTES)}")
    envelope = _strict_json(data, path.name)
    if (not isinstance(envelope, dict) or set(envelope) != {"schema", "run_id", "files"}
            or envelope["schema"] != ENVELOPE_SCHEMA):
        raise Refused(f"{path} is not a {ENVELOPE_SCHEMA} envelope: one object of schema, run_id and files")
    run_id, files = envelope["run_id"], envelope["files"]
    if not isinstance(run_id, str) or not RUN_ID_RE.match(run_id):
        raise Refused(f"{path} names {shown(repr(run_id))}, which is not a pipeline run id")
    if not isinstance(files, dict) or PACKAGE_JSON not in files:
        raise Refused(f"{path} carries no {PACKAGE_JSON}")
    canonical = json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    if canonical != data:
        raise Refused(f"{path} is not in canonical form (sorted keys, no spaces, ASCII), so its sha256 is not "
                      "a function of the package; write it again with publish_package.py --api")
    contents: dict[str, bytes] = {}
    for rel, text in files.items():
        if not isinstance(text, str) or rel.startswith("/") or any(part in ("", ".", "..") for part in rel.split("/")):
            raise Refused(f"{path} carries {shown(repr(rel))}, which is not a plain path inside a package")
        try:
            contents[rel] = base64.b64decode(text, validate=True)
        except (binascii.Error, ValueError):
            raise Refused(f"{path}: {shown(rel)} is not base64") from None
    manifest = _strict_json(contents[PACKAGE_JSON], PACKAGE_JSON)
    named = manifest.get("files") if isinstance(manifest, dict) else None
    if (not isinstance(manifest, dict) or manifest.get("schema") != PACKAGE_SCHEMA
            or manifest.get("run_id") != run_id or not isinstance(named, dict)):
        raise Refused(f"{path}'s {PACKAGE_JSON} is not a {PACKAGE_SCHEMA} manifest of run {run_id}")
    carried = set(contents) - {PACKAGE_JSON}
    if carried != set(named):
        extra, missing = sorted(carried - set(named)), sorted(set(named) - carried)
        raise Refused(f"{path} and its {PACKAGE_JSON} disagree: "
                      + "; ".join(part for part in (
                          f"carried but not named: {', '.join(map(shown, extra[:3]))}" if extra else "",
                          f"named but not carried: {', '.join(map(shown, missing[:3]))}" if missing else "")
                          if part))
    for rel in sorted(carried):
        if hashlib.sha256(contents[rel]).hexdigest() != named[rel]:
            raise Refused(f"{path}: {shown(rel)} is not the file its {PACKAGE_JSON} names")
    local = Local(len(data), hashlib.sha256(data).hexdigest(), hashlib.md5(data, usedforsecurity=False).hexdigest())
    return Envelope(path, run_id, data, local, len(files))


def envelope_needed(envelope: Envelope, remote: Mapping[str, RemoteObject]) -> bool:
    """Whether the envelope must be uploaded; refused if its key holds other bytes."""
    stray = sorted(key for key in remote if key != envelope.key)
    if stray:
        raise Refused(f"the bucket's listing of {envelope.key} returned {shown(stray[0])}; refusing to write "
                      "to a bucket whose listing cannot be trusted")
    found = remote.get(envelope.key)
    if found is None:
        return True
    if (found.size, found.etag) == (envelope.local.size, envelope.local.md5):
        return False
    raise Refused(f"the bucket already holds {envelope.key} with other bytes; a key here is never replaced. "
                  "Nothing was uploaded")


# ---- the bucket ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Plan:
    present: list[str]
    uploads: list[str]  # in upload order, MANIFEST.json last


def make_plan(bundle: Bundle, remote: Mapping[str, RemoteObject]) -> Plan:
    prefix = bundle.prefix
    outside = sorted(key for key in remote if not key.startswith(prefix))
    if outside:
        raise Refused(f"the bucket's listing of {prefix} returned keys outside it "
                      f"({shown(outside[0])}); refusing to write to a bucket whose listing cannot be "
                      "trusted")
    foreign = sorted(key for key in remote if key[len(prefix):] not in bundle.files)
    if foreign:
        raise Refused(f"the bucket holds {shown(foreign[0])}{' and others' if len(foreign) > 1 else ''} "
                      f"under {prefix}, which this bundle does not list. A prefix is one bundle's "
                      "content; find out who wrote it before uploading anything there")
    present, uploads, conflicts = [], [], []
    for rel, local in bundle.files.items():
        found = remote.get(prefix + rel)
        if found is None:
            uploads.append(rel)
        elif found.size == local.size and found.etag == local.md5:
            present.append(rel)
        else:
            conflicts.append(rel)
    if conflicts:
        raise Refused(f"the bucket already holds {shown(prefix + conflicts[0])} with other bytes; a key "
                      "here is never replaced. Nothing was uploaded")
    uploads.sort(key=lambda rel: (rel == MANIFEST_NAME, rel))
    return Plan(sorted(present), uploads)


def upload(bundle: Bundle, plan: Plan, bucket: Bucket, console: Console) -> None:
    for rel in plan.uploads:
        key = bundle.prefix + rel
        data = read_regular(bundle.root / rel)
        local = bundle.files[rel]
        if len(data) != local.size or hashlib.sha256(data).hexdigest() != local.sha256:
            raise Mismatch(f"{rel} changed after it was checked; stopping before {key}. "
                           f"{MANIFEST_NAME}, which goes last, is not uploaded, so the bundle there is "
                           "incomplete; package it again and rerun")
        etag = bucket.put(key, data)
        if etag != local.md5:
            raise Mismatch(f"PUT {key} answered ETag {shown(etag, 40)}, not the MD5 of the bytes "
                           f"({local.md5}); stopping. Run again with --execute to see what the bucket holds")
        console.say(f"  put     {rel}  ({size_of(local.size)})")


def verify(bundle: Bundle, bucket: Bucket) -> list[str]:
    remote = bucket.list(bundle.prefix)
    problems = []
    for rel, local in bundle.files.items():
        found = remote.get(bundle.prefix + rel)
        if found is None:
            problems.append(f"missing: {rel}")
        elif (found.size, found.etag) != (local.size, local.md5):
            problems.append(f"differs: {rel} ({size_of(found.size)}, ETag {shown(found.etag, 40)})")
    problems += [f"extra: {shown(key)}" for key in sorted(remote)
                 if key[len(bundle.prefix):] not in bundle.files]
    return problems


# ---- the command -----------------------------------------------------------------------------------


def parse_arguments(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    bundle = sub.add_parser("bundle", help="upload an index bundle package_index.py wrote")
    bundle.add_argument("dir", type=Path, help="the bundle directory")
    package = sub.add_parser("package", help="upload a work package's envelope publish_package.py --api wrote")
    package.add_argument("envelope", type=Path, help="the envelope file")
    for command in (bundle, package):
        command.add_argument("--bucket", required=True, help="the contributor bucket's name")
        where = command.add_mutually_exclusive_group(required=True)
        where.add_argument("--account-id", help="the Cloudflare account id (32 hex characters)")
        where.add_argument("--endpoint", help="tests only: a loopback S3 server, http://127.0.0.1:<port>")
        command.add_argument("--credentials-file", type=Path, metavar="FILE",
                             help=f"a 0600 file setting {ACCESS_KEY_VAR} and {SECRET_KEY_VAR}")
        command.add_argument("--execute", action="store_true", help="upload (default: a dry run)")
    bundle.add_argument("--acknowledge-restricted", action="append", default=[], metavar="LIBRARY",
                        help="a library whose use is restricted, named to hand it out anyway "
                             "(repeatable; must be exactly the bundle's restricted libraries)")
    return parser.parse_args(list(argv))


def upload_package(envelope: Envelope, bucket: Bucket, execute: bool, console: Console) -> int:
    """The package command once the envelope is checked: plan, then upload with --execute."""
    if not envelope_needed(envelope, bucket.list(envelope.key)):
        console.say(f"done      the bucket already holds {envelope.key} with the same bytes; nothing to upload")
        return EXIT_OK
    console.say(f"plan      1 object to upload ({size_of(envelope.local.size)})")
    if not execute:
        console.say("dry run   nothing was written. Run the same command with --execute to upload.")
        return EXIT_OK
    etag = bucket.put(envelope.key, envelope.data)
    if etag != envelope.local.md5:
        raise Mismatch(f"PUT {envelope.key} answered ETag {shown(etag, 40)}, not the MD5 of the bytes "
                       f"({envelope.local.md5}). Run again with --execute to see what the bucket holds")
    found = bucket.list(envelope.key).get(envelope.key)
    if found is None or (found.size, found.etag) != (envelope.local.size, envelope.local.md5):
        raise Mismatch(f"the bucket does not hold {envelope.key} as uploaded")
    console.say(f"verify    {envelope.key}: its size and ETag match")
    console.say(f"done      uploaded. Publish it with POST /api/packages: run_id {envelope.run_id}, "
                f"envelope_sha256 {envelope.local.sha256}, bytes {envelope.local.size}")
    return EXIT_OK


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None,
         stdout: TextIO | None = None, stderr: TextIO | None = None,
         sleep: Callable[[float], None] = time.sleep) -> int:
    console = Console(stdout or sys.stdout, stderr or sys.stderr)
    environ = os.environ if environ is None else environ
    args = parse_arguments(sys.argv[1:] if argv is None else argv)
    label, wrote, bucket = "contrib upload", False, None
    try:
        credentials = load_credentials(args.credentials_file, environ, CREDENTIAL_VARS)
        console.hide(credentials.access_key_id, credentials.secret_access_key)
        if not BUCKET_RE.match(args.bucket):
            raise Refused("--bucket must be an R2 bucket name: 3 to 63 characters of a-z, 0-9 and -, "
                          "starting and ending with a letter or digit")
        endpoint = loopback_endpoint(args.endpoint) if args.endpoint else r2_endpoint(args.account_id)
        console.say(f"{label}, " + ("UPLOADING (--execute)" if args.execute
                                    else "dry run: reads the bucket, writes nothing (--execute uploads)"))
        if args.command == "package":
            envelope = load_envelope(args.envelope.absolute())
            console.say(f"package   {envelope.path}  (run {envelope.run_id}, {envelope.files} files, "
                        f"{size_of(envelope.local.size)})")
            console.say(f"          envelope sha256 {envelope.local.sha256}")
            tls, trust = tls_context() if endpoint.scheme == "https" else (None, "none (loopback test server)")
            console.say(f"bucket    {args.bucket} at {endpoint.origin}, key {envelope.key} (TLS trust: {trust})")
            bucket = Bucket(endpoint, args.bucket, credentials, sleep=sleep, tls=tls)
            wrote = args.execute
            return upload_package(envelope, bucket, args.execute, console)
        bundle = load_bundle(args.dir.absolute(), args.acknowledge_restricted)
        manifest = bundle.manifest
        console.say(f"bundle    {bundle.root}  ({len(bundle.files)} objects, {size_of(bundle.total_bytes)})")
        console.say(f"          {shown(str(manifest.get('bundle')))}, universe "
                    f"{manifest['universe']['universe_sha256'][:12]}, manifest sha256 {bundle.manifest_sha256}")
        restricted = manifest["licences"]["use_restricted"]
        console.say(f"licences  every library with its licence texts; use restricted: "
                    + (", ".join(f"{name} (acknowledged)" for name in restricted) or "none"))
        tls, trust = tls_context() if endpoint.scheme == "https" else (None, "none (loopback test server)")
        console.say(f"bucket    {args.bucket} at {endpoint.origin}, prefix {bundle.prefix} (TLS trust: {trust})")
        bucket = Bucket(endpoint, args.bucket, credentials, sleep=sleep, tls=tls)
        remote = bucket.list(bundle.prefix)
        plan = make_plan(bundle, remote)
        console.say(f"plan      {len(plan.present)} present with the same bytes, {len(plan.uploads)} to upload "
                    f"({size_of(sum(bundle.files[rel].size for rel in plan.uploads))})")
        for rel in plan.uploads:
            console.say(f"  upload  {rel}  ({size_of(bundle.files[rel].size)})")
        if not args.execute:
            console.say("dry run   nothing was written. Run the same command with --execute to upload.")
            return EXIT_OK
        wrote = True
        upload(bundle, plan, bucket, console)
        problems = verify(bundle, bucket)
        if problems:
            raise Mismatch("the bucket does not match the bundle after uploading:\n  " + "\n  ".join(problems))
        console.say(f"verify    {len(bundle.files)} objects under {bundle.prefix}: none missing, none extra, "
                    "every size and ETag matches")
        console.say(f"done      {len(plan.uploads)} uploaded, {len(plan.present)} already there. The bundle's "
                    f"id is {bundle.manifest_sha256}; the Worker serves it once it is registered there.")
        return EXIT_OK
    except Refused as exc:
        console.warn(f"{label}: REFUSED, nothing was written: {exc}")
        return EXIT_REFUSED
    except Mismatch as exc:
        console.warn(f"{label}: MISMATCH: {exc}")
        return EXIT_MISMATCH
    except RemoteError as exc:
        state = ("objects uploaded so far stay (their keys are content-addressed); run again with --execute "
                 "to finish" if wrote else "nothing was written")
        console.warn(f"{label}: FAILED: {exc}\n{state}")
        return EXIT_FAILED
    except KeyboardInterrupt:
        console.warn(f"{label}: interrupted; " + ("objects uploaded so far stay; run again with --execute"
                                                  if wrote else "nothing was written"))
        return EXIT_INTERRUPTED
    except Exception as exc:  # noqa: BLE001 - reported, never a traceback that could carry a credential
        console.warn(f"{label}: FAILED: {internal_error(exc)}")
        return EXIT_FAILED
    finally:
        if bucket is not None:
            bucket.close()


if __name__ == "__main__":
    raise SystemExit(main())
