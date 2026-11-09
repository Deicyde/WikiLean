"""Bounded, byte-preserving transport shared by the contributor and intake daemon.

No archive extraction, shell, model calls, or fetched article text. The server's
Python daemon and the contributor both call the *same* normative verifier over a
fresh temporary copy of the exact uploaded files.
"""
from __future__ import annotations

import hashlib
import json
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import verify_submission as intake

SCHEMA = "wikilean.submission-upload/1"
MAX_BYTES = 4 * 1024 * 1024
BASE_URL = "https://wikilean.jackmccarthy.org"
PATH_RE = re.compile(r"(?:SEAL\.json|universe\.json|sources\.jsonl|records/enwiki-[1-9][0-9]*-[1-9][0-9]*\.jsonl|receipts/[0-9a-f]{64}\.json)\Z")


class TransportError(ValueError):
    pass


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def unpack(body: bytes) -> dict[str, bytes]:
    if len(body) > MAX_BYTES:
        raise TransportError("upload exceeds 4 MiB; export a smaller sealed shard")
    try:
        envelope = intake.build_store.strict_json(body.decode("utf-8"))
        if (not isinstance(envelope, dict) or set(envelope) != {"schema", "files"}
                or envelope["schema"] != SCHEMA or not isinstance(envelope["files"], dict)):
            raise TransportError("invalid submission envelope")
        files = envelope["files"]
        if "SEAL.json" not in files or len(files) > 512:
            raise TransportError("expected SEAL.json and at most 512 files")
        if any(not PATH_RE.fullmatch(path) or not isinstance(text, str)
               for path, text in files.items()):
            raise TransportError("only sealed record-set paths and UTF-8 file contents are allowed")
        return {path: text.encode("utf-8") for path, text in files.items()}
    except (UnicodeError, ValueError) as exc:
        raise TransportError("invalid UTF-8 submission envelope: " + str(exc)[:300]) from exc


def verify_upload(body: bytes, *, machine_id: str, received_at: str,
                  body_sha256: str, seal_sha256: str) -> intake.Accepted | intake.Refusal:
    if sha(body) != body_sha256:
        return intake.Refusal("body_digest", 422, False, "upload digest mismatch")
    try:
        files = unpack(body)
    except TransportError as exc:
        return intake.Refusal("envelope", 422, False, str(exc))
    with tempfile.TemporaryDirectory(prefix="wikilean-intake-") as tmp:
        root = Path(tmp)
        for path, data in files.items():
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as stream:
                stream.write(data)
        return intake.verify(root, scope=intake.KeyScope(machine_id=machine_id, max_body_bytes=MAX_BYTES),
                             received_at=received_at,
                             envelope=intake.Envelope(len(body), sha(body), body_sha256, seal_sha256))


def prepare(set_dir: Path, *, received_at: str) -> tuple[bytes, str]:
    # read_record_set refuses symlinks, unexpected files and hash mismatches.
    # Revalidate the captured bytes, not a mutable directory, before upload.
    _, seal_sha, files = intake.build_store.read_record_set(set_dir)
    files["SEAL.json"] = (set_dir / "SEAL.json").read_bytes()
    body = json.dumps({"schema": SCHEMA, "files": {p: b.decode("utf-8") for p, b in files.items()}},
                      ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    outcome = verify_upload(body, machine_id="local-preflight", received_at=received_at,
                            body_sha256=sha(body), seal_sha256=seal_sha)
    if isinstance(outcome, intake.Refusal):
        raise TransportError(outcome.report())
    return body, seal_sha


def origin(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if (parsed.scheme != "https" and not (parsed.scheme == "http" and loopback)
            or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
        raise TransportError("base URL must be an HTTPS origin (HTTP is allowed only on loopback)")
    return value.rstrip("/")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request(base: str, path: str, token: str, *, body: bytes | None = None,
            headers: dict[str, str] | None = None, timeout: float = 30) -> tuple[int, bytes]:
    if not path.startswith("/api/submissions") or "?" in path or "#" in path or ".." in path:
        raise TransportError("invalid submission API path")
    if not token or any(ch.isspace() for ch in token):
        raise TransportError("missing or malformed WikiLean token in the environment")
    req = urllib.request.Request(origin(base) + path, data=body,
                                 headers={"Authorization": "Bearer " + token,
                                          "User-Agent": "WikiLean-contributor/1",
                                          "Content-Type": "application/json", **(headers or {})})
    try:
        response = urllib.request.build_opener(NoRedirect()).open(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        response = exc
    except (OSError, urllib.error.URLError) as exc:
        # No request/header repr: it may carry the contributor's token.
        raise TransportError("submission connection failed; reuse the same sealed set to retry") from exc
    with response:
        try:
            data = response.read(MAX_BYTES + 1)
        except OSError as exc:
            raise TransportError("submission response interrupted; reuse the same sealed set to retry") from exc
        if len(data) > MAX_BYTES:
            raise TransportError("submission response exceeds its byte limit")
        return response.code, data


def response_json(status: int, data: bytes) -> dict:
    try:
        result = json.loads(data)
    except (ValueError, UnicodeError) as exc:
        raise TransportError(f"submission service returned HTTP {status} without a JSON receipt") from exc
    if not isinstance(result, dict) or not result.get("ok") or status not in (200, 202):
        code = result.get("code", "http_error") if isinstance(result, dict) else "http_error"
        error = result.get("error", "request refused") if isinstance(result, dict) else "request refused"
        raise TransportError(f"HTTP {status} ({code}): {str(error)[:1000]}")
    return result


def receipt(result: dict) -> dict:
    value = result.get("submission")
    if (not isinstance(value, dict)
            or not isinstance(value.get("id"), str)
            or not re.fullmatch(r"sub-[0-9a-f]{64}", value["id"])
            or value.get("state") not in ("queued", "validating", "accepted", "rejected", "failed")
            or value.get("published") is not False
            or any(not isinstance(value.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", value[key])
                   for key in ("body_sha256", "seal_sha256"))):
        raise TransportError("submission service returned an invalid receipt")
    return value
