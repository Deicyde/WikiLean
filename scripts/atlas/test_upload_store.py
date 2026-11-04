#!/usr/bin/env python3
"""Tests for scripts/atlas/upload_store.py, the atlas store uploader (scripts/atlas/UPLOAD.md).

New code. Offline: every bucket here is FakeS3, an S3 endpoint served in-process by
http.server on 127.0.0.1 that keeps objects in memory and refuses, as R2 would, any request
whose SigV4 signature (recomputed here from the request as received, by code independent of
the uploader's), payload hash or Content-MD5 does not match. Faults are injected per test:
failing statuses, a wrong ETag, bytes that change after upload, a lost object, a delete that
does nothing, keys outside the prefix in a listing, a listing that does not say how its keys
are encoded, and error bodies that echo the credentials. Most runs use a stand-in builder that
reports the store as matching (or not); RealBuilderTests run the real build_store.py --check on
the golden fixture store. After every test: no credential appeared in any output, no request
was refused for its signature, and no write landed outside atlas/v1/.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import os
import re
import shlex
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qsl, quote, quote_plus, unquote
from xml.sax.saxutils import escape

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import upload_store  # noqa: E402

UPLOAD = HERE / "upload_store.py"
RECORDSET = HERE / "fixtures" / "recordset"
GOLDEN_STORE = HERE / "fixtures" / "store"

BUCKET = "atlas-test"
# Synthetic credentials, shaped like an R2 token's S3 pair; they open nothing.
ACCESS_KEY_ID = "TESTKEYID-not-a-real-credential-01"
SECRET_ACCESS_KEY = "TESTSECRET-not-a-real-credential-0123456789"
ACCESS_VAR, SECRET_VAR = upload_store.ACCESS_KEY_VAR, upload_store.SECRET_KEY_VAR
PREFIX = "atlas/v1/"
CURRENT = PREFIX + "current.json"
MANIFEST = PREFIX + "manifest.json"


def canonical(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_store(root: Path, campaign: str, files: dict[str, bytes], built_at: str = "2026-11-01T00:00:00Z") -> Path:
    """A store laid out as build_store.py writes one: files under atlas/v1/, then current.json, then
    manifest.json listing every other file with its bytes and sha256."""
    tree = {PREFIX + name: data for name, data in files.items()}
    tree[CURRENT] = canonical({
        "schema": upload_store.CURRENT_SCHEMA, "campaign": campaign, "built_at": built_at,
        "record_set": {"seal_sha256": sha256(campaign.encode())},
        "index": {"entries": sum(1 for name in files if name.startswith("articles/"))}})
    tree[MANIFEST] = canonical({
        "schema": upload_store.MANIFEST_SCHEMA, "record_set": sha256(campaign.encode()),
        "files": {key: {"bytes": len(data), "sha256": sha256(data)} for key, data in sorted(tree.items())}})
    for key, data in tree.items():
        path = root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return root


def store_tree(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def reseal_manifest(root: Path, key: str) -> None:
    """Point manifest.json's entry for key at the file's current bytes (so only the builder can notice)."""
    path = root / MANIFEST
    manifest = json.loads(path.read_text(encoding="utf-8"))
    data = (root / key).read_bytes()
    manifest["files"][key] = {"bytes": len(data), "sha256": sha256(data)}
    path.write_bytes(canonical(manifest))


BUILD_A = {
    "universes/u0.json": b'{"universe":"a"}\n',
    "records/1.jsonl": b'{"record":1}\n',
    "records/2.jsonl": b'{"record":2}\n',
    "reader/1.html": b"<p>one</p>\n",
    "articles/1.json": b'{"article":1,"build":"a"}\n',
    "articles/2.json": b'{"article":2,"build":"a"}\n',
    "index/a.json": b'{"shard":"a","build":"a"}\n',
    "index/b.json": b'{"shard":"b","build":"a"}\n',
}
BUILD_B = {
    "universes/u0.json": b'{"universe":"b"}\n',
    "records/1.jsonl": b'{"record":1}\n',
    "records/3.jsonl": b'{"record":3}\n',
    "articles/1.json": b'{"article":1,"build":"b"}\n',
    "articles/3.json": b'{"article":3,"build":"b"}\n',
    "index/a.json": b'{"shard":"a","build":"b"}\n',
    "index/c.json": b'{"shard":"c","build":"b"}\n',
}
# What publishing B over A must delete.
STALE_AFTER_B = {PREFIX + name for name in ("records/2.jsonl", "reader/1.html", "articles/2.json", "index/b.json")}

# Stands in for build_store.py --check. FAKE_BUILDER_MODE: match (exit 0 with the builder's report), differs
# (exit 1), silent (exit 0, no report), rebuild (rewrites one article and its manifest entry, then reports).
FAKE_BUILDER = r'''
import hashlib, json, os, sys
args = sys.argv[1:]
log = os.environ.get("FAKE_BUILDER_LOG")
if log:
    with open(log, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"argv": args, "env": dict(os.environ),
                                 "dont_write_bytecode": sys.flags.dont_write_bytecode}) + "\n")
out = args[args.index("--out") + 1]
mode = os.environ.get("FAKE_BUILDER_MODE", "match")
if mode == "differs":
    print("atlas --check: differs: atlas/v1/current.json", file=sys.stderr)
    sys.exit(1)
if mode == "silent":
    sys.exit(0)
if mode == "rebuild":
    data = b'{"rebuilt":true}\n'
    with open(os.path.join(out, "atlas", "v1", "articles", "1.json"), "wb") as handle:
        handle.write(data)
    path = os.path.join(out, "atlas", "v1", "manifest.json")
    with open(path, encoding="utf-8") as handle:
        manifest = json.load(handle)
    manifest["files"]["atlas/v1/articles/1.json"] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n")
count = sum(len(files) for _, _, files in os.walk(out))
print(f"atlas --check: {out} matches ({count} files)")
'''


def error_xml(code: str, message: str, extra: str = "") -> bytes:
    return (f"<?xml version=\"1.0\" encoding=\"UTF-8\"?><Error><Code>{escape(code)}</Code>"
            f"<Message>{escape(message)}</Message>{extra}</Error>").encode("utf-8")


class FakeS3:
    """One bucket's S3 path-style API (ListObjectsV2, PUT, GET, DELETE) in memory, on 127.0.0.1."""

    def __init__(self, bucket: str = BUCKET, page_size: int = 1000) -> None:
        self.bucket, self.page_size = bucket, page_size
        self.objects: dict[str, bytes] = {}
        self.etags: dict[str, str] = {}  # what the bucket computed on upload, kept as R2 keeps it
        self.log: list[tuple[str, str]] = []  # (method, key or "?list"), in arrival order
        self.refusals: list[str] = []  # requests refused for their signature, payload hash or digest
        self.writes_outside_prefix: list[str] = []
        self.lock = threading.Lock()
        # Faults a test sets:
        self.fail: dict[tuple[str, str], list[int]] = {}  # (method, key or "?list") -> statuses to answer first
        self.wrong_etag: set[str] = set()  # PUT answers an ETag that is not the MD5
        self.corrupt: set[str] = set()  # stored bytes change after the upload; the recorded ETag does not
        self.drop: set[str] = set()  # PUT answers 200 but keeps nothing
        self.keep_on_delete: set[str] = set()  # DELETE answers 204 but keeps the object
        self.extra_listing: list[str] = []  # keys a listing returns whatever its prefix (a broken server)
        self.echo_credentials: set[tuple[str, str]] = set()  # answer 403 with a body naming the credentials
        self.after_put: dict[str, dict[str, bytes]] = {}  # once key is PUT, these objects appear (another writer)
        self.on_put: dict[str, object] = {}  # once key is PUT, call this (it changes something elsewhere)
        self.on_delete: dict[str, object] = {}  # once key is deleted, call this
        # How a listing answers encoding-type=url: "echo" encodes the keys and says so (EncodingType url), as
        # S3 does; "silent" encodes them without saying so; "ignore" sends them as they are.
        self.encoding_reply = "echo"
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: object) -> None:
                pass

            def do_GET(self) -> None:
                fake.handle(self)

            do_PUT = do_DELETE = do_HEAD = do_POST = do_GET

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def put_object(self, key: str, data: bytes) -> None:
        self.objects[key] = data
        self.etags[key] = hashlib.md5(data).hexdigest()

    def under_prefix(self) -> dict[str, bytes]:
        return {key: data for key, data in self.objects.items() if key.startswith(PREFIX)}

    def methods(self) -> list[str]:
        return [method for method, _ in self.log]

    # -- the request path ------------------------------------------------------------------------

    def handle(self, request: BaseHTTPRequestHandler) -> None:
        method = request.command
        raw_path, _, raw_query = request.path.partition("?")
        length = int(request.headers.get("Content-Length") or 0)
        body = request.rfile.read(length) if length else b""
        problem = self.check_signature(request, method, raw_path, raw_query, body)
        if problem:
            with self.lock:
                self.refusals.append(f"{method} {raw_path}: {problem}")
            return self.reply(request, 403, error_xml("SignatureDoesNotMatch", problem))
        base = "/" + self.bucket
        if raw_path == base:
            key = None
        elif raw_path.startswith(base + "/"):
            key = unquote(raw_path[len(base) + 1:])
        else:
            return self.reply(request, 404, error_xml("NoSuchBucket", "The specified bucket does not exist."))
        name = "?list" if key is None else key
        with self.lock:
            self.log.append((method, name))
            planned = self.fail.get((method, name))
            status = planned.pop(0) if planned else None
            echo = (method, name) in self.echo_credentials
        if echo:
            return self.reply(request, 403, error_xml(
                "AccessDenied", f"Access denied for {ACCESS_KEY_ID} with {SECRET_ACCESS_KEY}",
                f"<AWSAccessKeyId>{ACCESS_KEY_ID}</AWSAccessKeyId><StringToSign>{SECRET_ACCESS_KEY}</StringToSign>"))
        if status is not None:
            return self.reply(request, status, error_xml("InternalError", "injected failure"))
        if key is None and method == "GET":
            return self.listing(request, dict(parse_qsl(raw_query, keep_blank_values=True)))
        if key is None:
            return self.reply(request, 405, error_xml("MethodNotAllowed", "not a bucket operation here"))
        if method == "PUT":
            return self.put(request, key, body)
        if method == "GET":
            with self.lock:
                data, etag = self.objects.get(key), self.etags.get(key, "")
            if data is None:
                return self.reply(request, 404, error_xml("NoSuchKey", "The specified key does not exist."))
            return self.reply(request, 200, data, {"ETag": f'"{etag}"'})
        if method == "DELETE":
            with self.lock:
                if not key.startswith(PREFIX):
                    self.writes_outside_prefix.append(f"DELETE {key}")
                if key not in self.keep_on_delete:
                    self.objects.pop(key, None)
                    self.etags.pop(key, None)
                callback = self.on_delete.pop(key, None)
            if callback is not None:
                callback()
            return self.reply(request, 204)
        return self.reply(request, 405, error_xml("MethodNotAllowed", method))

    def put(self, request: BaseHTTPRequestHandler, key: str, body: bytes) -> None:
        digest = base64.b64encode(hashlib.md5(body).digest()).decode("ascii")
        if request.headers.get("Content-MD5") != digest:
            with self.lock:
                self.refusals.append(f"PUT {key}: BadDigest")
            return self.reply(request, 400, error_xml("BadDigest", "The Content-MD5 you specified did not match."))
        etag = hashlib.md5(body).hexdigest()
        with self.lock:
            if not key.startswith(PREFIX):
                self.writes_outside_prefix.append(f"PUT {key}")
            if key not in self.drop:
                stored = bytes([body[0] ^ 0x01]) + body[1:] if key in self.corrupt and body else body
                self.objects[key], self.etags[key] = stored, etag
            for other, data in self.after_put.pop(key, {}).items():
                self.objects[other], self.etags[other] = data, hashlib.md5(data).hexdigest()
            callback = self.on_put.pop(key, None)
        if callback is not None:
            callback()
        answered = "0" * 32 if key in self.wrong_etag else etag
        return self.reply(request, 200, b"", {"ETag": f'"{answered}"'})

    def listing(self, request: BaseHTTPRequestHandler, query: dict[str, str]) -> None:
        if query.get("list-type") != "2":
            return self.reply(request, 400, error_xml("InvalidArgument", "list-type=2 only"))
        prefix = query.get("prefix", "")
        token = query.get("continuation-token")
        with self.lock:
            keys = sorted(key for key in self.objects if key.startswith(prefix))
            if token:
                after = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
                keys = [key for key in keys if key > after]
            page, rest = keys[:self.page_size], keys[self.page_size:]
            entries = [(key, len(self.objects[key]), self.etags[key]) for key in page]
            if not token:
                entries += [(key, 1, "0" * 32) for key in self.extra_listing]
        # encoding-type=url: keys URL-encoded as S3 does it (a space as "+"), so any key survives the XML.
        asked = query.get("encoding-type") == "url"
        encode = (lambda key: quote_plus(key, safe="/")) if asked and self.encoding_reply != "ignore" else escape
        parts = [f"<Name>{self.bucket}</Name><Prefix>{encode(prefix)}</Prefix><KeyCount>{len(entries)}</KeyCount>"
                 f"<MaxKeys>{self.page_size}</MaxKeys><IsTruncated>{'true' if rest else 'false'}</IsTruncated>"]
        if asked and self.encoding_reply == "echo":
            parts.append("<EncodingType>url</EncodingType>")
        if rest:
            next_token = base64.urlsafe_b64encode(page[-1].encode("utf-8")).decode("ascii")
            parts.append(f"<NextContinuationToken>{next_token}</NextContinuationToken>")
        for key, size, etag in entries:
            parts.append(f"<Contents><Key>{encode(key)}</Key><LastModified>2026-11-01T00:00:00.000Z</LastModified>"
                         f"<ETag>&quot;{etag}&quot;</ETag><Size>{size}</Size><StorageClass>STANDARD</StorageClass>"
                         "</Contents>")
        body = ('<?xml version="1.0" encoding="UTF-8"?><ListBucketResult '
                'xmlns="http://s3.amazonaws.com/doc/2006-03-01/">' + "".join(parts) + "</ListBucketResult>")
        return self.reply(request, 200, body.encode("utf-8"), {"Content-Type": "application/xml"})

    @staticmethod
    def reply(request: BaseHTTPRequestHandler, status: int, body: bytes = b"",
              headers: dict[str, str] | None = None) -> None:
        request.send_response(status)
        for name, value in (headers or {}).items():
            request.send_header(name, value)
        request.send_header("Content-Length", str(len(body)))
        request.end_headers()
        if request.command != "HEAD":
            request.wfile.write(body)

    # -- SigV4, recomputed from the request as it arrived ----------------------------------------------

    @staticmethod
    def check_signature(request: BaseHTTPRequestHandler, method: str, raw_path: str, raw_query: str,
                        body: bytes) -> str | None:
        header = request.headers.get("Authorization", "")
        match = re.fullmatch(r"AWS4-HMAC-SHA256 Credential=([^/,]+)/([0-9]{8})/([a-z0-9-]+)/s3/aws4_request, "
                             r"SignedHeaders=([a-z0-9;-]+), Signature=([0-9a-f]{64})", header)
        if not match:
            return "malformed Authorization header"
        key_id, date, region, signed, signature = match.groups()
        if key_id != ACCESS_KEY_ID or region != "auto":
            return "wrong credential or region"
        stamp = request.headers.get("x-amz-date", "")
        if not re.fullmatch(r"[0-9]{8}T[0-9]{6}Z", stamp) or stamp[:8] != date:
            return "x-amz-date does not match the credential scope"
        names = signed.split(";")
        if names != sorted(set(names)) or not {"host", "x-amz-content-sha256", "x-amz-date"} <= set(names):
            return "SignedHeaders must be sorted and cover host, x-amz-content-sha256 and x-amz-date"
        if method == "PUT" and not {"content-md5", "content-type"} <= set(names):
            return "a PUT must sign its Content-MD5 and Content-Type"
        payload = request.headers.get("x-amz-content-sha256", "")
        if payload != sha256(body):
            return "XAmzContentSHA256Mismatch"
        pairs = sorted((quote(name, safe="-_.~"), quote(value, safe="-_.~"))
                       for name, value in parse_qsl(raw_query, keep_blank_values=True))
        query = "&".join(f"{name}={value}" for name, value in pairs)
        headers = "".join(f"{name}:{' '.join((request.headers.get(name) or '').split())}\n" for name in names)
        canonical_request = "\n".join([method, raw_path, query, headers, signed, payload])
        to_sign = "\n".join(["AWS4-HMAC-SHA256", stamp, f"{date}/auto/s3/aws4_request",
                             sha256(canonical_request.encode("utf-8"))])
        key = ("AWS4" + SECRET_ACCESS_KEY).encode("utf-8")
        for part in (date, "auto", "s3", "aws4_request"):
            key = hmac.new(key, part.encode("utf-8"), hashlib.sha256).digest()
        expected = hmac.new(key, to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
        return None if hmac.compare_digest(expected, signature) else "SignatureDoesNotMatch"


class UploadTestCase(unittest.TestCase):
    """A fake bucket and builder per test; every run's output is kept and checked for credentials."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="atlas-upload-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.s3 = FakeS3()
        self.addCleanup(self.s3.close)
        self.builder = self.tmp / "fake_build_store.py"
        self.builder.write_text(FAKE_BUILDER, encoding="utf-8")
        self.builder_log = self.tmp / "builder.jsonl"
        self.env = {ACCESS_VAR: ACCESS_KEY_ID, SECRET_VAR: SECRET_ACCESS_KEY,
                    "FAKE_BUILDER_LOG": str(self.builder_log)}
        self.outputs: list[str] = []

    def tearDown(self) -> None:
        for text in self.outputs:
            self.assertNotIn(ACCESS_KEY_ID, text)
            self.assertNotIn(SECRET_ACCESS_KEY, text)
        self.assertEqual(self.s3.refusals, [], "the fake bucket refused a request's signature or digest")
        self.assertEqual(self.s3.writes_outside_prefix, [])

    def store(self, name: str, files: dict[str, bytes], built_at: str = "2026-11-01T00:00:00Z") -> Path:
        return write_store(self.tmp / name, name, files, built_at)

    def run_upload(self, store: Path, *options: str, builder: Path | None = None,
                   builder_args: tuple[str, ...] = ("--record-set", "the-set"),
                   env: dict[str, str] | None = None, rollback: bool = False) -> tuple[int, str, str]:
        argv = ["--rollback-to" if rollback else "--store", str(store), "--bucket", BUCKET,
                "--endpoint", self.s3.endpoint, "--builder", str(builder or self.builder), *options,
                "--", *builder_args]
        out, err = io.StringIO(), io.StringIO()
        code = upload_store.main(argv, environ=self.env if env is None else env, stdout=out, stderr=err,
                                 sleep=lambda _seconds: None)
        self.outputs.append(out.getvalue() + err.getvalue())
        return code, out.getvalue(), err.getvalue()

    def run_unpublish(self, *options: str) -> tuple[int, str, str]:
        argv = ["--unpublish", "--bucket", BUCKET, "--endpoint", self.s3.endpoint, *options]
        out, err = io.StringIO(), io.StringIO()
        code = upload_store.main(argv, environ=self.env, stdout=out, stderr=err, sleep=lambda _seconds: None)
        self.outputs.append(out.getvalue() + err.getvalue())
        return code, out.getvalue(), err.getvalue()

    def publish(self, store: Path, **kwargs: object) -> None:
        code, out, err = self.run_upload(store, "--execute", **kwargs)
        self.assertEqual(code, 0, out + err)

    def seed(self, store: Path) -> None:
        """Put a build in the bucket directly, as if published before."""
        for key, data in store_tree(store).items():
            self.s3.put_object(key, data)

    def clear_faults(self) -> None:
        s3 = self.s3
        for faults in (s3.fail, s3.after_put, s3.on_put, s3.on_delete):
            faults.clear()
        for keys in (s3.wrong_etag, s3.corrupt, s3.drop, s3.keep_on_delete, s3.echo_credentials):
            keys.clear()
        s3.extra_listing = []
        s3.encoding_reply = "echo"

    def reset_bucket(self) -> None:
        """An empty bucket with no faults and an empty log, for the next case of a loop."""
        self.clear_faults()
        self.s3.objects.clear()
        self.s3.etags.clear()
        self.s3.log.clear()

    def writes(self) -> list[tuple[str, str]]:
        return [(method, key) for method, key in self.s3.log if method in ("PUT", "DELETE", "POST")]


class SigV4Tests(unittest.TestCase):
    """The signer against the worked examples of AWS's SigV4 documentation for S3
    (examplebucket, 2013-05-24, us-east-1, the documented example credentials)."""

    KEY_ID, SECRET = "AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    STAMP = "20130524T000000Z"
    HOST = "examplebucket.s3.amazonaws.com"

    def sign(self, method: str, path: str, query: list[tuple[str, str]], headers: dict[str, str],
             payload: str) -> str:
        value = upload_store.authorization(method, upload_store.uri_encode(path, safe="/"),
                                           upload_store.canonical_query(query), headers, payload,
                                           self.KEY_ID, self.SECRET, region="us-east-1")
        return value.rsplit("Signature=", 1)[1]

    def test_aws_documented_signatures(self) -> None:
        empty = upload_store.EMPTY_SHA256
        common = {"host": self.HOST, "x-amz-content-sha256": empty, "x-amz-date": self.STAMP}
        self.assertEqual(self.sign("GET", "/test.txt", [], {**common, "range": "bytes=0-9"}, empty),
                         "f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41")
        body = sha256(b"Welcome to Amazon S3.")
        self.assertEqual(self.sign("PUT", "/test$file.text", [],
                                   {"date": "Fri, 24 May 2013 00:00:00 GMT", "host": self.HOST,
                                    "x-amz-date": self.STAMP, "x-amz-storage-class": "REDUCED_REDUNDANCY",
                                    "x-amz-content-sha256": body}, body),
                         "98ad721746da40c64f1a55b78f14c238d841ea1380cd77a1b5971af0ece108bd")
        self.assertEqual(self.sign("GET", "/", [("lifecycle", "")], common, empty),
                         "fea454ca298b7da1c68078a5d1bdbfbbe0d65c699e0f91ac7a200a0136783543")
        self.assertEqual(self.sign("GET", "/", [("max-keys", "2"), ("prefix", "J")], common, empty),
                         "34b48302e7b5fa45bde8084f4b7868a86f0a534bc59db6670ed5711ef69dc6f7")

    def test_keys_and_queries_are_encoded_once_as_sigv4_wants(self) -> None:
        bucket = upload_store.Bucket(upload_store.loopback_endpoint("http://127.0.0.1:9"), "b-1",
                                     upload_store.Credentials("k" * 16, "s" * 16))
        self.assertEqual(bucket.path("atlas/v1/a b~c+d$.json"), "/b-1/atlas/v1/a%20b~c%2Bd%24.json")
        self.assertEqual(bucket.path(None), "/b-1")
        self.assertEqual(upload_store.canonical_query([("prefix", "atlas/v1/"), ("list-type", "2"),
                                                       ("continuation-token", "a+b=/")]),
                         "continuation-token=a%2Bb%3D%2F&list-type=2&prefix=atlas%2Fv1%2F")


class StoreTests(unittest.TestCase):
    """The store on disk is verified against its manifest before anything else happens."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="atlas-upload-store-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_a_built_store_loads_with_every_object_and_its_digests(self) -> None:
        root = write_store(self.tmp / "s", "a", BUILD_A)
        store = upload_store.load_store(root)
        tree = store_tree(root)
        self.assertEqual(set(store.objects), set(tree))
        for key, data in tree.items():
            entry = store.objects[key]
            self.assertEqual((entry.size, entry.sha256, entry.md5),
                             (len(data), sha256(data), hashlib.md5(data).hexdigest()))
        self.assertEqual(store.manifest_sha256, sha256(tree[MANIFEST]))

    def test_the_golden_fixture_store_loads(self) -> None:
        store = upload_store.load_store(GOLDEN_STORE)
        self.assertEqual(set(store.objects), set(store_tree(GOLDEN_STORE)))
        self.assertIn(CURRENT, store.objects)

    def refused(self, mutate) -> str:
        root = write_store(self.tmp / f"s{len(list(self.tmp.iterdir()))}", "a", BUILD_A)
        mutate(root)
        with self.assertRaises(upload_store.Refused) as caught:
            upload_store.load_store(root)
        return str(caught.exception)

    def test_every_disagreement_with_the_manifest_is_refused(self) -> None:
        def manifest_edit(edit):
            def mutate(root: Path) -> None:
                manifest = json.loads((root / MANIFEST).read_text(encoding="utf-8"))
                edit(manifest)
                (root / MANIFEST).write_bytes(canonical(manifest))
            return mutate

        cases = {
            "atlas/v1/.DS_Store is on disk but not listed": lambda r: (r / PREFIX / ".DS_Store").write_bytes(b"x"),
            "atlas/v1/index/b.json is listed in atlas/v1/manifest.json but missing":
                lambda r: (r / PREFIX / "index/b.json").unlink(),
            "atlas/v1/records/1.jsonl is 14 bytes": lambda r: (r / PREFIX / "records/1.jsonl").write_bytes(b"x" * 14),
            "atlas/v1/records/1.jsonl has sha256":
                lambda r: (r / PREFIX / "records/1.jsonl").write_bytes(b'{"record":9}\n'),
            "holds more than an atlas store: README": lambda r: (r / "README").write_bytes(b"x"),
            "is a symbolic link": lambda r: (r / PREFIX / "link.json").symlink_to(r / PREFIX / "index/a.json"),
            "lists itself": manifest_edit(lambda m: m["files"].update({MANIFEST: {"bytes": 1, "sha256": "0" * 64}})),
            "manifest key atlas/v1/../escape.json is not a store path": manifest_edit(
                lambda m: m["files"].update({"atlas/v1/../escape.json": {"bytes": 1, "sha256": "0" * 64}})),
            "does not list atlas/v1/current.json": manifest_edit(lambda m: m["files"].pop(CURRENT)),
            "is not a wikilean.atlas.manifest/1 manifest": manifest_edit(lambda m: m.update({"schema": "other/1"})),
            "manifest entry for atlas/v1/index/a.json is not {bytes, sha256}":
                manifest_edit(lambda m: m["files"][PREFIX + "index/a.json"].update({"bytes": True})),
            "is not valid JSON (duplicate key": lambda r: (r / MANIFEST).write_bytes(
                b'{"schema":"wikilean.atlas.manifest/1","files":{},"files":{}}'),
            "has no atlas/v1/manifest.json": lambda r: (r / MANIFEST).unlink(),
        }
        for expected, mutate in cases.items():
            with self.subTest(expected):
                self.assertIn(expected, self.refused(mutate))


class ArgumentTests(UploadTestCase):
    """Refusals before anything is read from or written to the bucket."""

    def assert_refused_without_requests(self, code: int, err: str, expected: str) -> None:
        self.assertEqual(code, 2, err)
        self.assertIn("REFUSED, nothing was written", err)
        self.assertIn(expected, err)
        self.assertEqual(self.s3.log, [])

    def test_missing_credentials_are_refused_by_name(self) -> None:
        store = self.store("a", BUILD_A)
        for missing in (ACCESS_VAR, SECRET_VAR):
            env = {name: value for name, value in self.env.items() if name != missing}
            code, _out, err = self.run_upload(store, env=env)
            self.assert_refused_without_requests(code, err, f"{missing} is not set in the environment")

    def test_no_option_takes_a_credential(self) -> None:
        help_text = subprocess.run([sys.executable, str(UPLOAD), "--help"], capture_output=True, text=True,
                                   check=True).stdout
        options = set(re.findall(r"--[a-z][a-z-]*", help_text))
        self.assertIn("--credentials-file", options)
        for option in options:
            self.assertIsNone(re.search(r"(key|secret|token|password|bearer)(-id)?$", option), option)

    def test_a_credentials_file_must_be_private_and_is_never_echoed(self) -> None:
        store = self.store("a", BUILD_A)
        good = self.tmp / "r2.env"
        good.write_text(f"# the atlas token\nexport {ACCESS_VAR}={ACCESS_KEY_ID}\n"
                        f"{SECRET_VAR}=\"{SECRET_ACCESS_KEY}\"\n", encoding="utf-8")
        good.chmod(0o600)
        code, out, err = self.run_upload(store, "--credentials-file", str(good), env={})
        self.assertEqual(code, 0, out + err)
        code, out, err = self.run_upload(store, "--credentials-file", str(good), "--execute", env={})
        self.assertEqual(code, 0, out + err)
        rollback = shlex.split(out.splitlines()[-1])
        self.assertEqual(rollback[rollback.index("--credentials-file") + 1], str(good.absolute()))

        self.s3.log.clear()
        good.chmod(0o644)
        code, _out, err = self.run_upload(store, "--credentials-file", str(good), env={})
        self.assert_refused_without_requests(code, err, "can be read by others (mode 0644); make it 0600")

        link = self.tmp / "link.env"
        good.chmod(0o600)
        link.symlink_to(good)
        code, _out, err = self.run_upload(store, "--credentials-file", str(link), env={})
        self.assert_refused_without_requests(code, err, "not a symbolic link")

        for content, expected in (
                (f"{SECRET_ACCESS_KEY}\n", "line 1 of the credentials file"),
                (f"{ACCESS_VAR}={ACCESS_KEY_ID}\nSOMETHING_ELSE={SECRET_ACCESS_KEY}\n",
                 "sets a name this script does not read"),
                (f"{ACCESS_VAR}={ACCESS_KEY_ID}\n{SECRET_VAR}=has spaces in it\n",
                 f"{SECRET_VAR} in the credentials file")):
            bad = self.tmp / "bad.env"
            bad.write_text(content, encoding="utf-8")
            bad.chmod(0o600)
            code, _out, err = self.run_upload(store, "--credentials-file", str(bad), env={})
            self.assert_refused_without_requests(code, err, expected)

    def test_only_r2_or_a_loopback_test_server_can_receive_requests(self) -> None:
        for endpoint in ("https://example.com", "http://10.0.0.1:9000", "http://127.0.0.1", "http://user@127.0.0.1:9",
                         "http://127.0.0.1:9/path"):
            with self.subTest(endpoint):
                with self.assertRaises(upload_store.Refused):
                    upload_store.loopback_endpoint(endpoint)
        for account in ("", "0123", "0123456789ABCDEF0123456789ABCDEF", "x" * 32):
            with self.subTest(account):
                with self.assertRaises(upload_store.Refused):
                    upload_store.r2_endpoint(account)
        self.assertEqual(upload_store.r2_endpoint("0123456789abcdef0123456789abcdef").origin,
                         "https://0123456789abcdef0123456789abcdef.r2.cloudflarestorage.com")
        for good in ("http://127.0.0.1:9", "http://localhost:9", "http://[::1]:9"):
            self.assertEqual(upload_store.loopback_endpoint(good).scheme, "http")

    def test_bucket_names_follow_r2s_rules(self) -> None:
        store = self.store("a", BUILD_A)
        for name in ("ab", "Atlas", "-atlas", "atlas-", "atlas_store", "a" * 64):
            with self.subTest(name):
                argv = ["--store", str(store), f"--bucket={name}", "--endpoint", self.s3.endpoint,
                        "--builder", str(self.builder), "--", "--record-set", "x"]
                err = io.StringIO()
                self.assertEqual(upload_store.main(argv, environ=self.env, stdout=io.StringIO(), stderr=err), 2)
                self.assertIn("--bucket must be an R2 bucket name", err.getvalue())
        self.assertEqual(self.s3.log, [])

    def test_the_builder_arguments_cannot_change_what_is_checked(self) -> None:
        store = self.store("a", BUILD_A)
        for args in (("--out", "elsewhere"), ("--out=elsewhere",), ("--ou", "elsewhere"), ("--check",),
                     ("--replace",), ("--rep",), ("--help",), ("--he",), ("-h",)):
            with self.subTest(args):
                code, _out, err = self.run_upload(store, builder_args=("--record-set", "x", *args))
                self.assert_refused_without_requests(code, err, "this script passes --out <store> --check itself")
        code, _out, err = self.run_upload(store, builder_args=())
        self.assert_refused_without_requests(code, err, "give the builder's arguments after --")
        code, _out, err = self.run_upload(store, builder_args=("--record-set", "x", "--execute"))
        self.assert_refused_without_requests(code, err, "--execute after -- would go to the builder")


class CheckTests(UploadTestCase):
    """The builder's --check runs first, on this store, without the credentials, and must pass."""

    def test_a_failing_check_refuses_before_any_request(self) -> None:
        store = self.store("a", BUILD_A)
        self.env["FAKE_BUILDER_MODE"] = "differs"
        code, out, err = self.run_upload(store, "--execute")
        self.assertEqual(code, 2)
        self.assertIn("the builder's --check failed with exit 1", err)
        self.assertIn("| atlas --check: differs: atlas/v1/current.json", out)
        self.assertEqual(self.s3.log, [])

    def test_exit_zero_without_the_builders_report_is_not_a_pass(self) -> None:
        store = self.store("a", BUILD_A)
        self.env["FAKE_BUILDER_MODE"] = "silent"
        code, _out, err = self.run_upload(store, "--execute")
        self.assertEqual(code, 2)
        self.assertIn(f"did not report {store} as matching all 10 files", err)
        self.assertEqual(self.s3.log, [])

    def test_a_store_that_changes_during_the_check_is_refused(self) -> None:
        store = self.store("a", BUILD_A)
        self.env["FAKE_BUILDER_MODE"] = "rebuild"
        code, _out, err = self.run_upload(store, "--execute")
        self.assertEqual(code, 2)
        self.assertIn("the store changed while the builder was checking it", err)
        self.assertEqual(self.s3.log, [])

    def test_the_check_is_this_store_and_runs_without_the_credentials(self) -> None:
        store = self.store("a", BUILD_A)
        self.env["COPY_UNDER_ANOTHER_NAME"] = f"prefix-{SECRET_ACCESS_KEY}"
        code, out, err = self.run_upload(store, builder_args=("--record-set", "set-a", "--campaign", "c"))
        self.assertEqual(code, 0, out + err)
        [call] = [json.loads(line) for line in self.builder_log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(call["argv"], ["--record-set", "set-a", "--campaign", "c", "--out", str(store), "--check"])
        self.assertNotIn(ACCESS_VAR, call["env"])
        self.assertNotIn(SECRET_VAR, call["env"])
        self.assertNotIn("COPY_UNDER_ANOTHER_NAME", call["env"])
        dumped = json.dumps(call)
        self.assertNotIn(ACCESS_KEY_ID, dumped)
        self.assertNotIn(SECRET_ACCESS_KEY, dumped)
        # No bytecode from the check, whatever the caller's environment: this one does not ask for that.
        self.assertNotIn("PYTHONDONTWRITEBYTECODE", self.env)
        self.assertEqual(call["dont_write_bytecode"], 1)
        self.assertEqual(call["env"]["PYTHONDONTWRITEBYTECODE"], "1")
        command = shlex.join([sys.executable, "-B", str(self.builder.resolve())])
        self.assertIn(f"check     {command} --record-set set-a", out)


class PublishTests(UploadTestCase):
    def test_a_dry_run_reads_the_bucket_and_writes_nothing(self) -> None:
        self.seed(self.store("a", BUILD_A))
        self.s3.put_object("other/keep.json", b"{}\n")
        before = dict(self.s3.objects)
        store_b = self.store("b", BUILD_B)
        code, out, err = self.run_upload(store_b)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(set(self.s3.methods()), {"GET"})
        self.assertEqual(self.s3.objects, before)
        self.assertIn("nothing was written", out)
        uploads = re.findall(r"^  upload  (\S+)", out, re.M)
        self.assertEqual(sorted(uploads), sorted(store_tree(store_b)))
        self.assertEqual(uploads[-1], CURRENT)
        self.assertEqual(set(re.findall(r"^  delete  (\S+)", out, re.M)), STALE_AFTER_B)
        self.assertIn("live      campaign a, built 2026-11-01T00:00:00Z", out)

    def test_publishing_puts_current_json_after_every_other_object_then_deletes(self) -> None:
        self.seed(self.store("a", BUILD_A))
        self.s3.put_object("other/keep.json", b"{}\n")
        self.s3.log.clear()
        store_b = self.store("b", BUILD_B)
        self.publish(store_b)
        writes = self.writes()
        puts = [key for method, key in writes if method == "PUT"]
        deletes = [key for method, key in writes if method == "DELETE"]
        self.assertEqual(sorted(puts), sorted(store_tree(store_b)))
        self.assertEqual(puts[-2:], [MANIFEST, CURRENT])
        self.assertEqual(set(deletes), STALE_AFTER_B)
        self.assertEqual(writes.index(("PUT", CURRENT)), len(puts) - 1, "every PUT before the DELETEs")
        rank = {top: index for index, top in enumerate(("universes", "records", "reader", "articles", "index"))}
        tops = [rank[key.split("/")[2]] for key in puts[:-2]]
        self.assertEqual(tops, sorted(tops), "leaves, then articles, then index shards")
        last_write = max(index for index, (method, _) in enumerate(self.s3.log) if method in ("PUT", "DELETE"))
        checked = {key for method, key in self.s3.log[last_write + 1:] if method == "GET"}
        self.assertTrue(set(store_tree(store_b)) <= checked, "every object read back after the deletes")
        self.assertEqual(self.s3.under_prefix(), store_tree(store_b))
        self.assertEqual(self.s3.objects["other/keep.json"], b"{}\n")

    def test_deletes_stay_under_the_prefix(self) -> None:
        self.seed(self.store("a", BUILD_A))
        neighbours = ["atlas/v1x/keep.json", "atlas/v2/keep.json", "atlas/keep.json", "other/atlas/v1/keep.json",
                      "atlas/v1-old/keep.json", "atlas/V1/keep.json"]
        for key in neighbours:
            self.s3.put_object(key, b"keep\n")
        self.s3.put_object("atlas/v1/", b"")  # a folder marker under the prefix goes, like any unlisted key
        self.publish(self.store("b", BUILD_B))
        deleted = {key for method, key in self.s3.log if method == "DELETE"}
        self.assertEqual(deleted, STALE_AFTER_B | {"atlas/v1/"})
        for key in neighbours:
            self.assertEqual(self.s3.objects.get(key), b"keep\n", key)

    def test_deletes_come_from_a_fresh_listing_and_say_when_it_changed(self) -> None:
        self.seed(self.store("a", BUILD_A))
        late = PREFIX + "articles/late.json"
        self.s3.after_put[CURRENT] = {late: b"written during the upload\n", "other/late.json": b"x\n"}
        code, out, err = self.run_upload(self.store("b", BUILD_B), "--execute")
        self.assertEqual(code, 0, out + err)
        self.assertIn("note      the bucket changed after the plan was printed: it now holds 5 objects", out)
        self.assertIn(f"delete    {late}  ok", out)
        self.assertEqual(self.s3.under_prefix(), store_tree(self.tmp / "b"))
        self.assertIn("other/late.json", self.s3.objects)

    def test_a_listing_with_keys_outside_the_prefix_is_refused(self) -> None:
        self.s3.extra_listing = ["other/x.json"]
        code, _out, err = self.run_upload(self.store("a", BUILD_A), "--execute")
        self.assertEqual(code, 2)
        self.assertIn("returned keys outside it (other/x.json)", err)
        self.assertEqual(self.writes(), [])

    def test_keys_a_server_could_read_as_other_keys_are_never_deleted(self) -> None:
        store = self.store("a", BUILD_A)
        for key in ("atlas/v1/../index/a.json", "atlas/v1/./x.json", "atlas/v1//x.json", "atlas/v1/a\\b.json",
                    "atlas/v1/x/..", "atlas/v1/\x1b[2Jx.json", "atlas/v1/café.json"):
            with self.subTest(key):
                self.s3.objects.clear()
                self.s3.etags.clear()
                self.s3.log.clear()
                self.s3.put_object(key, b"x")
                code, _out, err = self.run_upload(store, "--execute")
                self.assertEqual(code, 2, err)
                self.assertIn("will not delete", err)
                self.assertNotIn("\x1b", err)
                self.assertEqual(self.writes(), [])

    def test_a_wrong_etag_stops_before_current_json(self) -> None:
        self.seed(self.store("a", BUILD_A))
        self.s3.wrong_etag.add(PREFIX + "articles/1.json")
        code, _out, err = self.run_upload(self.store("b", BUILD_B), "--execute")
        self.assertEqual(code, 1)
        self.assertIn("PUT atlas/v1/articles/1.json: the bucket answered ETag 000", err)
        self.assertIn("Stopped before current.json. It still names the previous build, so the atlas stays published "
                      "and /atlas's totals are that build's, but 3 of the new build's 8 other objects are written (and "
                      "perhaps atlas/v1/articles/1.json, the upload that failed), 1 of them replacing objects of the "
                      "previous build.", err)
        self.assertNotIn(("PUT", CURRENT), self.writes())
        self.assertNotIn("DELETE", [method for method, _ in self.writes()])
        self.assertEqual(json.loads(self.s3.objects[CURRENT])["campaign"], "a")

    def test_bytes_that_change_after_upload_fail_the_final_check(self) -> None:
        self.s3.corrupt.add(PREFIX + "records/1.jsonl")
        code, _out, err = self.run_upload(self.store("a", BUILD_A), "--execute")
        self.assertEqual(code, 1)
        self.assertIn("sha256: atlas/v1/records/1.jsonl reads back as", err)
        self.assertIn("The new current.json is in place", err)

    def test_a_lost_object_fails_the_final_check(self) -> None:
        self.s3.drop.add(PREFIX + "index/a.json")
        code, _out, err = self.run_upload(self.store("a", BUILD_A), "--execute")
        self.assertEqual(code, 1)
        self.assertIn("missing: atlas/v1/index/a.json is not in the bucket", err)

    def test_a_delete_that_did_nothing_fails_the_final_check(self) -> None:
        self.seed(self.store("a", BUILD_A))
        self.s3.keep_on_delete.add(PREFIX + "articles/2.json")
        code, _out, err = self.run_upload(self.store("b", BUILD_B), "--execute")
        self.assertEqual(code, 1)
        self.assertIn("unexpected: atlas/v1/articles/2.json", err)

    def test_transient_failures_are_retried(self) -> None:
        self.s3.fail = {("PUT", PREFIX + "articles/1.json"): [503], ("GET", "?list"): [500, 429]}
        self.publish(self.store("a", BUILD_A))
        self.assertEqual(self.s3.log.count(("PUT", PREFIX + "articles/1.json")), 2)
        self.assertEqual(self.s3.under_prefix(), store_tree(self.tmp / "a"))

    def test_a_failure_before_current_json_says_what_readers_already_see(self) -> None:
        self.seed(self.store("a", BUILD_A))
        self.s3.fail = {("PUT", PREFIX + "index/a.json"): [500, 500, 500]}
        code, _out, err = self.run_upload(self.store("b", BUILD_B), "--execute")
        self.assertEqual(code, 3)
        self.assertIn("PUT atlas/v1/index/a.json failed after 3 attempts (HTTP 500 InternalError: injected failure)",
                      err)
        self.assertIn("Stopped before current.json. It still names the previous build, so the atlas stays published "
                      "and /atlas's totals are that build's, but 5 of the new build's 8 other objects are written (and "
                      "perhaps atlas/v1/index/a.json, the upload that failed), 2 of them replacing objects of the "
                      "previous build. Reader pages, the dashboard's article list, article boxes and every JSON view "
                      "but /api/atlas/current read objects by key, so they already show these. Run again with "
                      "--execute to finish, or --rollback-to the previous build's directory.", err)
        self.assertNotIn(("PUT", CURRENT), self.writes())
        self.assertNotIn("Nothing was written", err)
        # As the message says: current.json is still A's, but what the site reads by key is B's already.
        self.assertEqual(json.loads(self.s3.objects[CURRENT])["campaign"], "a")
        for name in ("universes/u0.json", "articles/1.json", "articles/3.json"):
            self.assertEqual(self.s3.objects[PREFIX + name], BUILD_B[name], name)
        self.assertEqual(self.s3.objects[PREFIX + "index/a.json"], BUILD_A["index/a.json"])

    def test_a_store_file_that_changes_during_the_upload_stops_before_current_json(self) -> None:
        self.seed(self.store("a", BUILD_A))
        store_b = self.store("b", BUILD_B)
        article = store_b / PREFIX / "articles" / "1.json"
        self.s3.on_put[PREFIX + "universes/u0.json"] = lambda: article.write_bytes(b'{"edited":"meanwhile"}\n')
        code, _out, err = self.run_upload(store_b, "--execute")
        self.assertEqual(code, 1)
        self.assertIn("atlas/v1/articles/1.json changed on disk after the check", err)
        self.assertIn("Stopped before current.json", err)
        self.assertNotIn("perhaps", err, "nothing of the changed file was sent")
        self.assertNotIn(("PUT", PREFIX + "articles/1.json"), self.writes())
        self.assertEqual(json.loads(self.s3.objects[CURRENT])["campaign"], "a")

    def test_a_failure_at_current_json_says_it_may_hold_either_build(self) -> None:
        self.seed(self.store("a", BUILD_A))
        self.s3.fail = {("PUT", CURRENT): [503, 503, 503]}
        code, _out, err = self.run_upload(self.store("b", BUILD_B), "--execute")
        self.assertEqual(code, 3)
        self.assertIn("Stopped at current.json: every other object of the new build is written, and current.json may "
                      "name either build. Run again with --execute, or --rollback-to the previous build's directory, "
                      "before anything else.", err)
        self.assertNotIn("DELETE", [method for method, _ in self.writes()])

    def test_a_failure_after_current_json_says_the_new_build_is_live(self) -> None:
        self.seed(self.store("a", BUILD_A))
        self.s3.fail = {("DELETE", PREFIX + "index/b.json"): [500, 500, 500]}
        code, _out, err = self.run_upload(self.store("b", BUILD_B), "--execute")
        self.assertEqual(code, 3)
        self.assertIn("The new build is published (current.json in place and checked)", err)
        self.assertEqual(json.loads(self.s3.objects[CURRENT])["campaign"], "b")

    def test_error_bodies_never_print_the_credentials(self) -> None:
        self.s3.echo_credentials.add(("PUT", PREFIX + "records/1.jsonl"))
        code, _out, err = self.run_upload(self.store("a", BUILD_A), "--execute")
        self.assertEqual(code, 3)
        self.assertIn("HTTP 403 AccessDenied: Access denied for [redacted] with [redacted]", err)
        self.assertNotIn("StringToSign", err)

    def test_an_unreachable_bucket_fails_cleanly(self) -> None:
        store = self.store("a", BUILD_A)
        self.s3.close()
        code, _out, err = self.run_upload(store, "--execute")
        self.assertEqual(code, 3)
        self.assertIn("listing atlas/v1/ failed after 3 attempts (ConnectionRefusedError", err)
        self.assertTrue(err.rstrip().endswith("\nNothing was written."), err)

    def test_republishing_the_same_build_changes_nothing_and_still_checks_everything(self) -> None:
        store = self.store("a", BUILD_A)
        self.publish(store)
        self.s3.log.clear()
        code, out, err = self.run_upload(store, "--execute")
        self.assertEqual(code, 0, out + err)
        self.assertIn("(0 new, 0 replacing other bytes, 9 unchanged), then current.json, then 0 deletes", out)
        self.assertIn("live      campaign a, built 2026-11-01T00:00:00Z, record set", out)
        self.assertIn("(this build's current.json)", out)
        self.assertEqual(self.s3.under_prefix(), store_tree(store))
        # Every object goes up again, "unchanged" ones too: a listing's ETag cannot show bytes that changed after
        # their upload, and a re-run must repair them (test_a_rerun_after_each_failure_converges).
        self.assertEqual(self.s3.methods().count("PUT"), len(store_tree(store)))
        self.assertNotIn("DELETE", self.s3.methods())

    def test_every_listing_page_is_read(self) -> None:
        self.s3.page_size = 2
        self.seed(self.store("a", BUILD_A))
        self.publish(self.store("b", BUILD_B))
        self.assertEqual(self.s3.under_prefix(), store_tree(self.tmp / "b"))
        self.assertGreater(self.s3.log.count(("GET", "?list")), 3)

    def test_rollback_republishes_a_previous_build_the_same_way(self) -> None:
        store_a, store_b = self.store("a", BUILD_A), self.store("b", BUILD_B, "2026-11-02T00:00:00Z")
        self.publish(store_a)
        self.publish(store_b)
        self.s3.log.clear()
        code, out, err = self.run_upload(store_a, "--execute", rollback=True)
        self.assertEqual(code, 0, out + err)
        self.assertIn("atlas rollback, PUBLISHING", out)
        self.assertIn(f"rollback  the build in {store_a} replaces the live one", out)
        self.assertIn("done      rolled back to campaign a", out)
        self.assertEqual(self.s3.under_prefix(), store_tree(store_a))
        writes = self.writes()
        puts = [key for method, key in writes if method == "PUT"]
        self.assertEqual(puts[-1], CURRENT)
        self.assertEqual({key for method, key in writes if method == "DELETE"},
                         {PREFIX + name for name in ("records/3.jsonl", "articles/3.json", "index/c.json")})
        self.assertEqual(writes.index(("PUT", CURRENT)), len(puts) - 1)
        [call] = [json.loads(line) for line in self.builder_log.read_text(encoding="utf-8").splitlines()[-1:]]
        self.assertEqual(call["argv"][-3:], ["--out", str(store_a), "--check"])

    def test_store_and_rollback_each_refuse_the_other_direction(self) -> None:
        self.seed(self.store("live", BUILD_B, "2026-11-02T00:00:00Z"))
        code, _out, err = self.run_upload(self.store("older", BUILD_A, "2026-11-01T00:00:00Z"), "--execute")
        self.assertEqual(code, 2)
        self.assertIn("the live build was built at 2026-11-02T00:00:00Z, after this one (2026-11-01T00:00:00Z): "
                      "publishing this store would roll the atlas back. To do that on purpose, use --rollback-to", err)
        code, _out, err = self.run_upload(self.store("newer", BUILD_A, "2026-11-03T00:00:00Z"), "--execute",
                                          rollback=True)
        self.assertEqual(code, 2)
        self.assertIn("is newer than the live one (2026-11-02T00:00:00Z), so this is not a rollback", err)
        self.assertEqual(self.writes(), [])

    def test_ctrl_c_says_what_was_written(self) -> None:
        self.seed(self.store("a", BUILD_A))
        store_b = self.store("b", BUILD_B)
        with mock.patch.object(upload_store, "run_check", side_effect=KeyboardInterrupt):
            code, _out, err = self.run_upload(store_b, "--execute")
        self.assertEqual(code, 130)
        self.assertIn("INTERRUPTED before anything was written", err)
        self.assertEqual(self.s3.log, [])

        original = upload_store.Bucket.put

        def put(bucket: upload_store.Bucket, key: str, data: bytes) -> str:
            if key == PREFIX + "articles/1.json":
                raise KeyboardInterrupt
            return original(bucket, key, data)

        with mock.patch.object(upload_store.Bucket, "put", put):
            code, _out, err = self.run_upload(store_b, "--execute")
        self.assertEqual(code, 130)
        self.assertIn("INTERRUPTED: Stopped before current.json. It still names the previous build", err)
        self.assertNotIn(("PUT", CURRENT), self.writes())
        self.assertEqual(json.loads(self.s3.objects[CURRENT])["campaign"], "a")


class FailureStateTests(UploadTestCase):
    """Every failure says that nothing was written, or what the bucket holds and what the site's readers see.
    current.json is the publish switch: without it every atlas route answers 503, and with it every route
    reads its objects by key."""

    def test_a_first_publish_that_stops_before_current_json_says_nothing_was_published(self) -> None:
        self.s3.fail = {("PUT", PREFIX + "index/a.json"): [500, 500, 500]}
        code, _out, err = self.run_upload(self.store("a", BUILD_A), "--execute")
        self.assertEqual(code, 3, err)
        self.assertIn("Stopped before current.json. Nothing was published before this run and current.json is "
                      "still absent, so every atlas route answers 503 and no article box shows, although 6 of the "
                      "new build's 9 other objects are written (and perhaps atlas/v1/index/a.json, the upload that "
                      "failed). Run again with --execute to finish, or --unpublish to remove them.", err)
        self.assertNotIn("previous build", err)
        self.assertNotIn(CURRENT, self.s3.objects)
        self.assertEqual(len(self.s3.under_prefix()), 6)

    def test_a_first_publish_that_stops_at_or_after_current_json_offers_unpublish_not_a_rollback(self) -> None:
        store = self.store("a", BUILD_A)
        self.s3.fail = {("PUT", CURRENT): [503, 503, 503]}
        code, _out, err = self.run_upload(store, "--execute")
        self.assertEqual(code, 3, err)
        self.assertIn("Stopped at current.json: every other object of the new build is written, and current.json may "
                      "now name it, which publishes the build, or still be absent, so every atlas route answers 503. "
                      "Run again with --execute, or --unpublish, before anything else.", err)
        self.assertNotIn("previous build", err)

        self.reset_bucket()
        self.s3.drop.add(PREFIX + "index/a.json")
        code, _out, err = self.run_upload(store, "--execute")
        self.assertEqual(code, 1, err)
        self.assertIn("The new current.json is in place. Run again with --execute, or remove it with --unpublish.",
                      err)
        self.assertNotIn("previous build", err)

    def test_a_request_that_fails_before_any_write_says_nothing_was_written(self) -> None:
        self.s3.fail = {("GET", "?list"): [403]}
        code, _out, err = self.run_upload(self.store("a", BUILD_A), "--execute")
        self.assertEqual(code, 3, err)
        self.assertIn("FAILED: listing atlas/v1/: HTTP 403 InternalError: injected failure\nNothing was written.", err)
        self.assertEqual(self.writes(), [])

    def test_an_internal_error_says_what_state_the_bucket_is_in(self) -> None:
        store_a, store_b = self.store("a", BUILD_A), self.store("b", BUILD_B)
        self.seed(store_a)
        with mock.patch.object(upload_store, "run_check", side_effect=ValueError("before any write")):
            code, _out, err = self.run_upload(store_b, "--execute")
        self.assertEqual(code, 3, err)
        self.assertIn("internal error (ValueError: before any write); nothing was written", err)
        self.assertEqual(self.writes(), [])

        original = upload_store.Bucket.put

        def put(bucket: upload_store.Bucket, key: str, data: bytes) -> str:
            if key == PREFIX + "articles/1.json":
                raise ValueError(f"unexpected, holding {SECRET_ACCESS_KEY}")
            return original(bucket, key, data)

        with mock.patch.object(upload_store.Bucket, "put", put):
            code, _out, err = self.run_upload(store_b, "--execute")
        self.assertEqual(code, 3, err)
        self.assertIn("FAILED: internal error (ValueError: unexpected, holding [redacted])\nStopped before "
                      "current.json. It still names the previous build", err)
        self.assertNotIn("Nothing was written", err)

        with mock.patch.object(upload_store, "verify_bucket", side_effect=ValueError("in the check")):
            code, _out, err = self.run_upload(store_b, "--execute")
        self.assertEqual(code, 3, err)
        self.assertIn("FAILED: internal error (ValueError: in the check)\nThe new build is published (current.json "
                      "in place and checked), but the final check of every object did not finish", err)

    def test_a_wrong_etag_on_current_json_says_it_may_name_either_build(self) -> None:
        self.seed(self.store("a", BUILD_A))
        self.s3.wrong_etag.add(CURRENT)
        code, _out, err = self.run_upload(self.store("b", BUILD_B), "--execute")
        self.assertEqual(code, 1, err)
        self.assertIn("MISMATCH: PUT atlas/v1/current.json: the bucket answered ETag 000", err)
        self.assertIn("Stopped at current.json: every other object of the new build is written, and current.json may "
                      "name either build.", err)
        self.assertNotIn("DELETE", self.s3.methods())

    def test_ctrl_c_after_current_json_says_the_new_build_is_published(self) -> None:
        store_a, store_b = self.store("a", BUILD_A), self.store("b", BUILD_B)
        self.seed(store_a)
        with mock.patch.object(upload_store.Bucket, "delete", side_effect=KeyboardInterrupt):
            code, _out, err = self.run_upload(store_b, "--execute")
        self.assertEqual(code, 130, err)
        self.assertIn("INTERRUPTED: The new build is published (current.json in place and checked), but objects it "
                      "no longer lists may remain under atlas/v1/, and reader pages, the dashboard's article list, "
                      "article boxes and every JSON view but /api/atlas/current still serve any of them. Run again "
                      "with --execute to finish the deletes.", err)
        self.assertEqual(json.loads(self.s3.objects[CURRENT])["campaign"], "b")

        with mock.patch.object(upload_store, "verify_bucket", side_effect=KeyboardInterrupt):
            code, _out, err = self.run_upload(store_b, "--execute")
        self.assertEqual(code, 130, err)
        self.assertIn("INTERRUPTED: The new build is published (current.json in place and checked), but the final "
                      "check of every object did not finish. Run again with --execute.", err)

    def test_a_rerun_after_each_failure_converges(self) -> None:
        store_a, store_b = self.store("a", BUILD_A), self.store("b", BUILD_B)
        faults = {
            "an upload fails": (3, lambda s3: s3.fail.update({("PUT", PREFIX + "index/a.json"): [500] * 3})),
            "current.json fails": (3, lambda s3: s3.fail.update({("PUT", CURRENT): [503] * 3})),
            "a delete fails": (3, lambda s3: s3.fail.update({("DELETE", PREFIX + "index/b.json"): [500] * 3})),
            "a delete does nothing": (1, lambda s3: s3.keep_on_delete.add(PREFIX + "articles/2.json")),
            "stored bytes change after an upload": (1, lambda s3: s3.corrupt.add(PREFIX + "records/1.jsonl")),
        }
        for name, (first_code, fault) in faults.items():
            with self.subTest(name):
                self.reset_bucket()
                self.seed(store_a)
                fault(self.s3)
                code, out, err = self.run_upload(store_b, "--execute")
                self.assertEqual(code, first_code, out + err)
                self.clear_faults()
                code, out, err = self.run_upload(store_b, "--execute")
                self.assertEqual(code, 0, out + err)
                self.assertEqual(self.s3.under_prefix(), store_tree(store_b))


class ListingTrustTests(UploadTestCase):
    """Every listing, the planning one, the deletes' and the final check's, is held to the same rules."""

    def test_odd_keys_are_deleted_under_their_exact_names(self) -> None:
        store = self.store("a", BUILD_A)
        odd = {PREFIX + name for name in ("a b.json", "a+b.json", "a%20b.json", "q?x.json", "h#x.json", "amp&x.json",
                                          "lt<x.json")}
        for key in odd:
            self.s3.put_object(key, b"stale\n")
        code, out, err = self.run_upload(store, "--execute")
        self.assertEqual(code, 0, out + err)
        self.assertIn("listing   7 objects under atlas/v1/ (keys URL-encoded, as asked)", out)
        self.assertEqual({key for method, key in self.writes() if method == "DELETE"}, odd)
        self.assertEqual(self.s3.under_prefix(), store_tree(store))

    def test_a_delete_listing_with_keys_outside_the_prefix_deletes_nothing(self) -> None:
        self.seed(self.store("a", BUILD_A))
        self.s3.on_put[CURRENT] = lambda: setattr(self.s3, "extra_listing", ["other/precious.json"])
        code, _out, err = self.run_upload(self.store("b", BUILD_B), "--execute")
        self.assertEqual(code, 1, err)
        self.assertIn("the bucket's listing of atlas/v1/ returned keys outside it (other/precious.json); none of its "
                      "keys was deleted. Keys outside atlas/v1/ are not store objects: do not delete them", err)
        self.assertIn("The new build is published (current.json in place and checked)", err)
        self.assertNotIn("remove it by hand", err)
        self.assertNotIn("DELETE", self.s3.methods())

    def test_a_final_listing_with_keys_outside_the_prefix_is_a_listing_fault(self) -> None:
        self.seed(self.store("a", BUILD_A))
        store_b = self.store("b", BUILD_B)
        self.s3.on_delete[PREFIX + "records/2.jsonl"] = lambda: setattr(self.s3, "extra_listing",
                                                                        ["other/precious.json"])
        code, _out, err = self.run_upload(store_b, "--execute")
        self.assertEqual(code, 1, err)
        self.assertIn("listing fault: the bucket listed other/precious.json, which is outside atlas/v1/; it is not a "
                      "store object and was not touched", err)
        self.assertNotIn("remove it by hand", err)
        self.assertNotIn("unexpected:", err)
        self.assertEqual({key for method, key in self.writes() if method == "DELETE"}, STALE_AFTER_B)
        self.assertEqual(self.s3.under_prefix(), store_tree(store_b))

    def test_a_listing_that_does_not_say_its_keys_are_encoded_refuses_a_key_it_could_misread(self) -> None:
        # The server encodes keys without saying so, so "a b.json" lists as "a+b.json".
        self.s3.encoding_reply = "silent"
        self.s3.put_object(PREFIX + "a b.json", b"stale\n")
        store = self.store("a", BUILD_A)
        for options in ((), ("--execute",)):
            with self.subTest(options):
                code, _out, err = self.run_upload(store, *options)
                self.assertEqual(code, 3, err)
                self.assertIn("does not say whether its keys are URL-encoded", err)
                self.assertIn("atlas/v1/a+b.json names a different key decoded than as listed", err)
                self.assertIn("Nothing was written.", err)
        self.assertEqual(self.writes(), [])
        self.assertEqual(set(self.s3.objects), {PREFIX + "a b.json"})

        # A server that sends keys as they are and holds "a+b.json": it could be "a b.json" encoded.
        self.reset_bucket()
        self.s3.encoding_reply = "ignore"
        self.s3.put_object(PREFIX + "a+b.json", b"stale\n")
        code, _out, err = self.run_unpublish("--execute")
        self.assertEqual(code, 3, err)
        self.assertIn("atlas/v1/a+b.json names a different key decoded than as listed", err)
        self.assertEqual(self.writes(), [])

    def test_a_listing_without_an_encoding_is_read_as_listed_when_no_key_reads_two_ways(self) -> None:
        store = self.store("a", BUILD_A)
        for reply, stale in (("silent", "stale.json"), ("ignore", "a b.json")):
            with self.subTest(reply):
                self.reset_bucket()
                self.s3.encoding_reply = reply
                self.s3.put_object(PREFIX + stale, b"stale\n")
                code, out, err = self.run_upload(store, "--execute")
                self.assertEqual(code, 0, out + err)
                self.assertIn("(the answer has no EncodingType, so keys are read as listed;", out)
                self.assertIn(("DELETE", PREFIX + stale), self.writes())
                self.assertEqual(self.s3.under_prefix(), store_tree(store))


class UnpublishTests(UploadTestCase):
    """--unpublish deletes every object under atlas/v1/, current.json first, with no store and no builder."""

    def test_a_dry_run_lists_every_object_current_json_first_and_deletes_nothing(self) -> None:
        store_a = self.store("a", BUILD_A)
        self.seed(store_a)
        self.s3.put_object("other/keep.json", b"{}\n")
        before = dict(self.s3.objects)
        code, out, err = self.run_unpublish()
        self.assertEqual(code, 0, out + err)
        self.assertEqual(set(self.s3.methods()), {"GET"})
        self.assertEqual(self.s3.objects, before)
        deletes = re.findall(r"^  delete  (\S+)", out, re.M)
        self.assertEqual(deletes[0], CURRENT)
        self.assertEqual(sorted(deletes), sorted(store_tree(store_a)))
        self.assertIn("live      campaign a, built 2026-11-01T00:00:00Z", out)
        self.assertIn("plan      10 deletes, every object under atlas/v1/ (current.json first)", out)
        self.assertIn("  delete  atlas/v1/current.json  ", out)
        self.assertIn("(first: once it is gone every atlas route answers 503 within about a minute)", out)
        self.assertIn("dry run   nothing was deleted", out)
        self.assertFalse(self.builder_log.exists(), "no builder runs")

    def test_it_deletes_current_json_first_then_the_rest_and_nothing_outside_the_prefix(self) -> None:
        store_a = self.store("a", BUILD_A)
        self.seed(store_a)
        neighbours = ["other/keep.json", "atlas/v1x/keep.json", "atlas/v2/keep.json", "atlas/keep.json",
                      "atlas/V1/keep.json", "other/atlas/v1/keep.json"]
        for key in neighbours:
            self.s3.put_object(key, b"keep\n")
        code, out, err = self.run_unpublish("--execute")
        self.assertEqual(code, 0, out + err)
        writes = self.writes()
        self.assertEqual({method for method, _ in writes}, {"DELETE"})
        deleted = [key for _, key in writes]
        self.assertEqual(deleted[0], CURRENT)
        self.assertEqual(deleted[1:], sorted(set(store_tree(store_a)) - {CURRENT}, key=upload_store.delete_rank))
        self.assertEqual(self.s3.under_prefix(), {})
        for key in neighbours:
            self.assertEqual(self.s3.objects[key], b"keep\n", key)
        last_delete = max(index for index, (method, _) in enumerate(self.s3.log) if method == "DELETE")
        self.assertIn(("GET", "?list"), self.s3.log[last_delete + 1:], "the prefix is listed again after the deletes")
        self.assertIn("verify    nothing is left under atlas/v1/", out)
        self.assertIn("done      unpublished campaign a, built 2026-11-01T00:00:00Z", out)
        # current.json is the publish switch: the atlas is offline once it is gone, each Worker isolate
        # seeing it within a minute.
        self.assertIn("delete    atlas/v1/current.json  ok: every atlas route answers 503 within about a minute and "
                      "the article boxes go", out)
        self.assertIn("The atlas is offline: every atlas route answers 503 within about a minute and the article "
                      "boxes go, as each Worker isolate's copy of current.json expires", out)
        self.publish(store_a)  # a kept build brings the atlas back
        self.assertEqual(self.s3.under_prefix(), store_tree(store_a))

    def test_a_listing_it_cannot_trust_is_refused_before_any_delete(self) -> None:
        self.seed(self.store("a", BUILD_A))
        self.s3.extra_listing = ["other/x.json"]
        code, _out, err = self.run_unpublish("--execute")
        self.assertEqual(code, 2, err)
        self.assertIn("returned keys outside it (other/x.json)", err)
        self.s3.extra_listing = []
        self.s3.put_object(PREFIX + "../index/a.json", b"x")
        code, _out, err = self.run_unpublish("--execute")
        self.assertEqual(code, 2, err)
        self.assertIn("will not delete", err)
        self.assertEqual(self.writes(), [])

    def test_it_takes_no_store_builder_or_builder_arguments(self) -> None:
        store = self.store("a", BUILD_A)
        for options, expected in ((("--store", str(store)), "not allowed with argument"),
                                  (("--rollback-to", str(store)), "not allowed with argument"),
                                  (("--builder", str(self.builder)), "--unpublish runs no builder"),
                                  (("--", "--record-set", "x"), "--unpublish runs no builder check")):
            with self.subTest(options):
                code, _out, err = self.run_unpublish(*options)
                self.assertEqual(code, 2, err)
                self.assertIn("REFUSED, nothing was written", err)
                self.assertIn(expected, err)
        self.assertEqual(self.s3.log, [])

    def test_an_empty_prefix_needs_no_deletes(self) -> None:
        self.s3.put_object("other/keep.json", b"{}\n")
        code, out, err = self.run_unpublish("--execute")
        self.assertEqual(code, 0, out + err)
        self.assertIn("live      nothing published", out)
        self.assertIn("plan      nothing to delete: atlas/v1/ is empty", out)
        self.assertEqual(self.writes(), [])
        self.assertIn("other/keep.json", self.s3.objects)

    def test_each_failure_says_what_is_left_and_a_rerun_finishes(self) -> None:
        self.seed(self.store("a", BUILD_A))
        self.s3.fail = {("DELETE", CURRENT): [500, 500, 500]}
        code, _out, err = self.run_unpublish("--execute")
        self.assertEqual(code, 3, err)
        self.assertIn("Stopped at current.json: it may still be in place, so the atlas may still be published, and no "
                      "other object was deleted. Run --unpublish --execute again.", err)
        self.assertEqual(self.writes(), [("DELETE", CURRENT)] * 3)

        self.s3.fail = {("DELETE", PREFIX + "articles/1.json"): [500, 500, 500]}
        code, _out, err = self.run_unpublish("--execute")
        self.assertEqual(code, 3, err)
        # current.json is the publish switch: with it gone, what remains is served by no route.
        self.assertIn("current.json is gone, so every atlas route answers 503 within about a minute and the article "
                      "boxes go, but objects remain under atlas/v1/. Run --unpublish --execute again to remove them.",
                      err)
        self.assertNotIn("still serve", err)
        self.assertNotIn(CURRENT, self.s3.objects)

        self.s3.keep_on_delete.add(PREFIX + "records/1.jsonl")
        code, _out, err = self.run_unpublish("--execute")
        self.assertEqual(code, 1, err)
        self.assertIn("the bucket's listing of atlas/v1/ is not empty after the deletes:\n  "
                      "left: atlas/v1/records/1.jsonl (13 bytes)\ncurrent.json is gone", err)

        self.s3.keep_on_delete.clear()
        with mock.patch.object(upload_store.Bucket, "delete", side_effect=KeyboardInterrupt):
            code, _out, err = self.run_unpublish("--execute")
        self.assertEqual(code, 130, err)
        self.assertIn("INTERRUPTED: current.json is gone, so every atlas route answers 503 within about a minute", err)

        code, out, err = self.run_unpublish("--execute")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.s3.under_prefix(), {})


class RollbackCommandTests(UploadTestCase):
    def test_a_publish_prints_the_command_that_rolls_back_to_it(self) -> None:
        store_a = self.store("a", BUILD_A)
        code, out, err = self.run_upload(store_a, "--execute", builder_args=("--record-set", "set a"))
        self.assertEqual(code, 0, out + err)
        lines = out.splitlines()
        index = next(i for i, line in enumerate(lines) if line.startswith("keep      "))
        self.assertIn("every record set and side file the builder arguments name, unchanged", lines[index])
        self.assertIn(f"this builder (sha256 {sha256(self.builder.read_bytes())})", lines[index])
        command = shlex.split(lines[index + 1])
        self.assertEqual(command[:4], [sys.executable, str(UPLOAD), "--rollback-to", str(store_a)])
        self.assertEqual(command[command.index("--builder") + 1], str(self.builder.resolve()))
        split = command.index("--")
        self.assertEqual(command[split:], ["--", "--record-set", "set a"])
        self.assertNotIn("--execute", command)

        # It works: once a newer build is live, the printed command (with --execute) brings this one back.
        self.publish(self.store("b", BUILD_B, "2026-11-02T00:00:00Z"))
        env = {**{k: v for k, v in os.environ.items() if k not in (ACCESS_VAR, SECRET_VAR)}, **self.env}
        result = subprocess.run([*command[:split], "--execute", *command[split:]], env=env, capture_output=True,
                                text=True, timeout=120)
        self.outputs.append(result.stdout + result.stderr)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("done      rolled back to campaign a", result.stdout)
        self.assertEqual(self.s3.under_prefix(), store_tree(store_a))


class RealBuilderTests(UploadTestCase):
    """build_store.py --check itself, on a copy of the golden fixture store and its record set."""

    def golden_copy(self) -> Path:
        root = self.tmp / "golden"
        shutil.copytree(GOLDEN_STORE, root)
        return root

    def test_the_golden_store_passes_the_real_check_and_publishes(self) -> None:
        root = self.golden_copy()
        code, out, err = self.run_upload(root, "--execute", builder=upload_store.DEFAULT_BUILDER,
                                         builder_args=("--record-set", str(RECORDSET)))
        self.assertEqual(code, 0, out + err)
        self.assertIn(f"| atlas --check: {root} matches ({len(store_tree(root))} files)", out)
        self.assertEqual(self.s3.under_prefix(), store_tree(root))

    def test_a_store_the_record_set_does_not_build_is_refused_by_the_real_check(self) -> None:
        root = self.golden_copy()
        article = root / PREFIX / "articles" / "1001.json"
        article.write_bytes(article.read_bytes().replace(b'"title":"', b'"title":"Not '))
        reseal_manifest(root, PREFIX + "articles/1001.json")
        upload_store.load_store(root)  # the manifest agrees, so only the builder can tell
        code, out, err = self.run_upload(root, "--execute", builder=upload_store.DEFAULT_BUILDER,
                                         builder_args=("--record-set", str(RECORDSET)))
        self.assertEqual(code, 2, out + err)
        self.assertIn("atlas --check: differs: atlas/v1/articles/1001.json", out)
        self.assertIn("the builder's --check failed with exit 1", err)
        self.assertEqual(self.s3.log, [])


class CommandLineTests(UploadTestCase):
    def test_the_script_runs_end_to_end_from_the_command_line(self) -> None:
        store = self.store("a", BUILD_A)
        env = {**{k: v for k, v in os.environ.items() if k not in (ACCESS_VAR, SECRET_VAR)}, **self.env}
        base = [sys.executable, str(UPLOAD), "--store", str(store), "--bucket", BUCKET, "--endpoint",
                self.s3.endpoint, "--builder", str(self.builder)]
        for extra in ([], ["--execute"]):
            result = subprocess.run([*base, *extra, "--", "--record-set", "x"], env=env, capture_output=True,
                                    text=True, timeout=120)
            self.outputs.append(result.stdout + result.stderr)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("verify    10 objects under atlas/v1/: none missing, none extra", result.stdout)
        self.assertEqual(self.s3.under_prefix(), store_tree(store))

    def test_the_tls_context_verifies_certificates_and_host_names(self) -> None:
        context, source = upload_store.tls_context()
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertTrue(source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
