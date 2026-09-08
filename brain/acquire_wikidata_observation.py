#!/usr/bin/env python3
"""Acquire one complete Wikidata universe/edge/description evidence generation.

Use acquire-wikidata-observation.sh with an explicit reviewed request plan.
No legacy output is changed. The output is one verified private immutable
bundle; its three normalized files must be bound together in source authority.
The supported transport does not retry or follow redirects, ensuring every
successful bundle has an exact, complete request transcript.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(ROOT / "brain"))
import stage_io  # noqa: E402
import wikidata_observation as observation  # noqa: E402

ENVIRONMENT = {"LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin", "NO_PROXY": "*"}
USER_AGENT = "WikiLean/1.0 (https://wikilean.jackmccarthy.org)"
HTTP_TRAILER = re.compile(rb"wikilean-observation-http-v2\t([0-9]{3})\t([^\r\n\t]*)\t([^\r\n]*)\n\Z")
DEFAULT_STORE = ROOT / "catalog" / ".cache" / "wikidata" / "observation-bundles"
FAILED_ATTEMPT_SCHEMA = "wikilean.wikidata-observation-failed-attempt/v2"


class RequestFailure(observation.ObservationError):
    """A failed attempt's bounded private evidence; never authority input."""

    def __init__(self, index: int, category: str, raw: bytes = b"", *,
                 status: int | None = None, content_type: str | None = None,
                 curl_exit_code: int | None = None, retry_after: dict | None = None):
        super().__init__(f"request {index}: {category}")
        self.index, self.category, self.raw = index, category, raw
        self.status, self.content_type = status, content_type
        self.curl_exit_code, self.retry_after = curl_exit_code, retry_after
        self.diagnostics_path: Path | None = None


def parsed_retry_after(raw: bytes) -> dict | None:
    """Retain only a bounded wait instruction, never arbitrary header text."""
    if not raw or len(raw) > 128:
        return None
    if re.fullmatch(rb"[0-9]{1,15}", raw):
        return {"kind": "delay-seconds", "seconds": int(raw)}
    if not re.fullmatch(rb"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun), [0-9]{2} (?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) [0-9]{4} [0-9]{2}:[0-9]{2}:[0-9]{2} GMT", raw):
        return None
    try:
        timestamp = dt.datetime.strptime(raw.decode("ascii"), "%a, %d %b %Y %H:%M:%S GMT")
    except ValueError:
        return None
    return {"kind": "http-date", "value": timestamp.isoformat() + "Z"}


def runtime_identity(curl: Path) -> dict:
    if platform.python_implementation() != "CPython" or sys.version_info[:2] != (3, 12):
        raise observation.ObservationError("CPython 3.12 is required")
    curl = curl.resolve(strict=True)
    version = subprocess.run([str(curl), "--disable", "--version"], env=ENVIRONMENT,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=10).stdout
    toolchain = {
        "schema": observation.TOOLCHAIN_SCHEMA,
        "profile_id": observation.reviewed_profiles()["current_profile"],
        "observation_policy": observation.OBSERVATION_POLICY,
        "transport_policy": observation.TRANSPORT_POLICY,
        "python": {"implementation": platform.python_implementation(), "version": platform.python_version(),
                   "sha256": observation.sha(Path(sys.executable).resolve().read_bytes()), "startup": ["-I", "-S"]},
        "curl": {"sha256": observation.sha(curl.read_bytes()), "version": version.decode("utf-8").splitlines()[0]},
        "files": [{"path": relative, "sha256": observation.sha(observation.read_regular(ROOT / relative))}
                  for relative in observation.LOCAL_TOOL_FILES],
    }
    observation.validate_toolchain(toolchain)
    return toolchain


def require_isolated_startup() -> None:
    if not all((sys.flags.isolated, sys.flags.ignore_environment, sys.flags.no_site,
                sys.flags.no_user_site, sys.flags.safe_path)):
        raise observation.ObservationError("use the isolated CPython launcher (-I -S)")


def _transport(request: observation.Request, curl: Path, index: int) -> dict:
    accept = "application/json" if request.stage == "descriptions" else "application/sparql-results+json"
    args = [str(curl), "--disable", "--silent", "--show-error", "--fail-with-body",
            "--proto", "=https", "--tlsv1.2", "--connect-timeout", "30", "--max-time", "180",
            "--max-filesize", str(observation.MAX_RESPONSE_BYTES), "--header", f"Accept: {accept}",
            "--header", f"User-Agent: {USER_AGENT}",
            "--write-out", "%{stderr}wikilean-observation-http-v2\\t%{http_code}\\t%{content_type}\\t%header{retry-after}\\n"]
    payload = None
    if request.kind == "http_post":
        args += ["--request", "POST", "--header", "Content-Type: application/x-www-form-urlencoded",
                 "--data-binary", "@-", request.uri]
        payload = request.parameters
    else:
        args += ["--request", "GET", request.uri + "?" + request.parameters.decode("ascii")]
    try:
        result = subprocess.run(args, input=payload, env=ENVIRONMENT, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=190, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        partial = getattr(exc, "stdout", None)
        raise RequestFailure(index, "transport failed", partial if isinstance(partial, bytes) else b"") from exc
    trailer = HTTP_TRAILER.search(result.stderr)
    status = int(trailer[1]) if trailer is not None else None
    content_type = trailer[2].decode("ascii", errors="replace") if trailer is not None else None
    retry_after = parsed_retry_after(trailer[3]) if trailer is not None else None
    if result.returncode or trailer is None:
        raise RequestFailure(index, "unsuccessful HTTP transport", result.stdout,
                             status=status, content_type=content_type,
                             curl_exit_code=result.returncode, retry_after=retry_after)
    try:
        record = observation.response_record(index, request, result.stdout, status, content_type)
        observation.validate_response_payload(request, result.stdout)
    except observation.ObservationError as exc:
        raise RequestFailure(index, "invalid HTTP response or payload", result.stdout,
                             status=status, content_type=content_type,
                             curl_exit_code=result.returncode, retry_after=retry_after) from exc
    return record


def record_failed_attempt(store: Path, plan_bytes: bytes, request: observation.Request,
                          failure: RequestFailure) -> Path:
    """Save one bounded failed response and exact request, without source claims.

    Only canonical public endpoints are named; curl stderr, effective URLs,
    environment values and credentials are never serialized. The response is
    private opaque diagnostic data and is not a receipt or normalization input.
    """
    if request.uri not in {observation.WDQS, observation.API}:
        raise observation.ObservationError("diagnostics require a reviewed public endpoint")
    if len(request.parameters) > 2 * 1024 * 1024 or len(plan_bytes) > 16 * 1024 * 1024:
        raise observation.ObservationError("failed-attempt request metadata exceeds bounds")
    raw = failure.raw[:observation.MAX_RESPONSE_BYTES]
    files = {"response.bin": raw, "request.parameters": request.parameters, "request-plan.json": plan_bytes}
    document = {
        "schema": FAILED_ATTEMPT_SCHEMA, "authority": False,
        "observation_policy": observation.OBSERVATION_POLICY,
        "request_index": failure.index, "stage": request.stage,
        "request": request.descriptor(), "category": failure.category,
        "http_status": failure.status,
        "curl_exit_code": failure.curl_exit_code, "retry_after": failure.retry_after,
        "response": {"received_bytes": len(failure.raw), "received_sha256": observation.sha(failure.raw),
                     "stored_bytes": len(raw), "stored_sha256": observation.sha(raw),
                     "truncated_to_bound": len(raw) != len(failure.raw)},
        "files": [{"path": path, "sha256": observation.sha(data), "bytes": len(data)}
                  for path, data in sorted(files.items())],
    }
    files["failure.json"] = observation.canonical(document)
    target = store / ("failed-attempt-" + uuid.uuid4().hex)
    temporary = store / (".failed-attempt-" + uuid.uuid4().hex + ".tmp")
    with stage_io.owned_directory(store, temporary) as owned:
        for relative, data in files.items():
            # stage_io seals mode 0644 members inside owner-only 0700 roots.
            stage_io.write_bytes_exclusive(owned.path / relative, data, mode=0o644)
        stage_io.fsync_directory(owned.path)
        stage_io.publish_directory_no_replace(owned, target)
    stage_io.fsync_directory(store)
    return target


def _private_directory(path: Path) -> None:
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o700 \
            or metadata.st_uid != os.getuid() or path.is_symlink():
        raise observation.ObservationError("observation store must be a current-user-owned 0700 real directory")
    for parent in path.parents:
        if parent.is_symlink():
            raise observation.ObservationError("observation store has a linked ancestor")


def prepare_store(store: Path) -> Path:
    if not store.is_absolute() or ".." in store.parts:
        raise observation.ObservationError("store must be an explicit absolute path")
    anchor = store
    while not anchor.exists() and not anchor.is_symlink():
        anchor = anchor.parent
    stage_io.ensure_private_directory(anchor, store)
    _private_directory(store)
    return store


@contextmanager
def writer_lock(store: Path):
    """One lock covers acquisition through publication; kernel release survives SIGKILL."""
    path = store / ".observation.lock"
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() \
                or metadata.st_nlink != 1 or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise observation.ObservationError("invalid observation writer lock")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        current = path.lstat()
        if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise observation.ObservationError("observation lock was substituted")
        yield
    finally:
        os.close(descriptor)


def publish_records(plan: dict, records: list[dict], *, store: Path, toolchain: dict,
                    audit_time: str, before_publish: Callable | None = None) -> Path:
    """Offline publication helper; live acquisition also holds writer_lock around requests."""
    store = prepare_store(store)
    with writer_lock(store):
        return _publish_locked(plan, records, store=store, toolchain=toolchain,
                               audit_time=audit_time, before_publish=before_publish)


def _publish_locked(plan: dict, records: list[dict], *, store: Path, toolchain: dict,
                    audit_time: str, before_publish: Callable | None = None) -> Path:
    bundle_id, files = observation.bundle_files(plan, records, toolchain, audit_time)
    target = store / bundle_id.removeprefix("sha256:")
    temporary = store / f".observation-{uuid.uuid4().hex}.tmp"
    with stage_io.owned_directory(store, temporary) as owned:
        for directory in ("normalized", "requests"):
            stage_io.ensure_private_directory(owned.path, owned.path / directory)
        for relative, raw in sorted(files.items()):
            stage_io.write_bytes_exclusive(owned.path / relative, raw, mode=0o644)
        for directory in ("normalized", "requests"):
            stage_io.fsync_directory(owned.path / directory)
        stage_io.fsync_directory(owned.path)
        if before_publish is not None:
            before_publish(owned.path, target)
        observation.verify_bundle(owned.path, expected_id=bundle_id, allow_staging=True)
        try:
            stage_io.publish_directory_no_replace(owned, target)
        except FileExistsError:
            observation.verify_bundle(target, expected_id=bundle_id)
    stage_io.fsync_directory(store)
    observation.verify_bundle(target, expected_id=bundle_id)
    return target


def acquire(plan_path: Path, *, store: Path, curl: Path) -> Path:
    require_isolated_startup()
    # The hashed executable and every launched request must use the same resolved path.
    curl = curl.resolve(strict=True)
    plan_bytes = observation.read_regular(plan_path, max_bytes=16 * 1024 * 1024)
    plan = observation.validate_plan(observation.parse(plan_bytes, "request plan", canonical_required=True))
    toolchain = runtime_identity(curl)
    store = prepare_store(store)
    with writer_lock(store):
        records = []
        total = 0
        for index, request in enumerate(observation.requests_for(plan)):
            try:
                record = _transport(request, curl, index)
            except RequestFailure as failure:
                try:
                    diagnostic = record_failed_attempt(store, plan_bytes, request, failure)
                    failure.diagnostics_path = diagnostic
                    failure.add_note(f"private failed-attempt diagnostics: {diagnostic}")
                except (observation.ObservationError, OSError) as diagnostic_error:
                    failure.add_note(f"could not retain failed-attempt diagnostics: {type(diagnostic_error).__name__}")
                raise
            records.append(record)
            total += len(record["body_base64"])
            if total > observation.MAX_TRANSCRIPT_BYTES * 4 // 3 + 4 * len(records):
                raise observation.ObservationError("aggregate transcript exceeds operational bound")
            time.sleep(0.3 if request.stage == "descriptions" else 2.0)
        if runtime_identity(curl) != toolchain:
            raise observation.ObservationError("acquisition runtime changed before publication")
        if observation.read_regular(plan_path, max_bytes=16 * 1024 * 1024) != plan_bytes:
            raise observation.ObservationError("reviewed plan changed during acquisition")
        audit_time = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        return _publish_locked(plan, records, store=store, toolchain=toolchain, audit_time=audit_time)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    parser.add_argument("--store", type=Path, default=DEFAULT_STORE)
    parser.add_argument("--curl", type=Path)
    args = parser.parse_args()
    try:
        require_isolated_startup()
        curl = args.curl or Path(shutil.which("curl", path="/usr/bin:/bin") or "/nonexistent")
        print(acquire(args.plan, store=args.store, curl=curl))
        return 0
    except (observation.ObservationError, observation.contracts.VerificationError, OSError, subprocess.SubprocessError) as exc:
        print(f"Wikidata observation acquisition failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        if isinstance(exc, RequestFailure) and exc.diagnostics_path is not None:
            print(f"Private failed-attempt diagnostics: {exc.diagnostics_path}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
