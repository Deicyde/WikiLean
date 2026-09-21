#!/usr/bin/env python3
"""Fresh private ProofWiki gzip capture and independent binary-identity export.

Acquisition requires CPython3.12 -I -S, a reviewed canonical plan, and one fresh
credential-free bounded curl GET. Failed attempts are diagnostic records only.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import platform
import re
import selectors
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import public_file_source_evidence as core

LOADED_IMPLEMENTATION = {name: core.read_regular(core.ROOT / name) for name in core.TOOL_FILES}
TRAILER = b"\nWIKILEAN_PUBLIC_FILE_RESULT\t"
DIAGNOSTIC_BODY_LIMIT = 64 * 1024 * 1024
PROCESS_EXIT_TIMEOUT_SECONDS = 5
ENVIRONMENT = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}


def implementation():
    core.origins()
    core.require(Path(core.__file__).resolve() == core.ROOT / "brain/public_file_source_evidence.py", "public-file core origin differs")
    actual = {name: core.read_regular(core.ROOT / name) for name in core.TOOL_FILES}
    core.require(actual == LOADED_IMPLEMENTATION, "public-file implementation changed after loading")
    return actual


def require_startup():
    core.require(platform.python_implementation() == "CPython" and sys.version_info[:2] == (3, 12)
        and sys.flags.isolated == 1 and sys.flags.no_site == 1, "acquisition requires isolated CPython3.12 -I -S")


def now():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def runtime_identity(curl):
    require_startup()
    profile, programs = core.current_profile(), implementation()
    core.verify_programs(profile, programs)
    before = core.sha(core.read_regular(curl))
    result = subprocess.run([str(curl), "-q", "--version"], env=dict(ENVIRONMENT), check=True,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10)
    core.require(core.sha(core.read_regular(curl)) == before, "curl changed while identifying it")
    tool = {"schema": core.TOOL_SCHEMA, "profile_id": profile["profile_id"], "files": profile["files"],
        "python": {"sha256": core.sha(core.read_regular(Path(sys.executable).resolve(strict=True))),
            "version": "CPython " + platform.python_version() + " -I -S"},
        "curl": {"sha256": before, "version": result.stdout.decode("utf-8").splitlines()[0]}}
    core.validate_tool(tool)
    return tool, programs


class TransportFailure(core.EvidenceError):
    def __init__(self, message, raw, response):
        super().__init__(message)
        self.raw, self.response = raw, response


def command(plan, profile, curl):
    parameters = core.parameters(plan, profile)
    args = [str(curl), "-q", "--silent", "--show-error", "--fail-with-body", "--request", "GET",
        "--proxy", "", "--noproxy", "*", "--proto", "=https", "--proto-redir", "=https",
        "--max-redirs", "0", "--connect-timeout", "30", "--max-time", "600",
        "--max-filesize", str(parameters["transport"]["maximum_response_bytes"]),
        "--output", "-", "--write-out", "%{stderr}\\nWIKILEAN_PUBLIC_FILE_RESULT\\t%{http_code}\\t%{content_type}\\n"]
    for name, value in sorted(parameters["headers"].items()):
        args.extend(["--header", name + ": " + value])
    return [*args, "--url", plan["uri"]]


def response_metadata(raw, returncode, stderr_tail):
    # Retain only parsed numeric status and a normalized media type; curl error
    # text and response headers never enter the evidence/diagnostic documents.
    match = re.search(re.escape(TRAILER) + rb"([0-9]{3})\t([^\r\n]*)\n\Z", stderr_tail)
    status, media = 0, ""
    if match:
        status = int(match[1])
        value = match[2].split(b";", 1)[0].strip().lower()
        if re.fullmatch(rb"[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+", value):
            media = value.decode("ascii")
    return {"curl_exit_code": int(returncode), "http_status": status, "content_type": media,
        "sha256": core.sha(raw), "bytes": len(raw)}


def transport(plan, profile, curl):
    policy = core.validate_plan(plan, profile)
    limit = policy["maximum_bytes"]
    raw, stderr_tail = bytearray(), b""
    failure = None
    process = subprocess.Popen(command(plan, profile, curl), env=dict(ENVIRONMENT), stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True)
    try:
        deadline = time.monotonic() + 605
        with selectors.DefaultSelector() as selector:
            for stream in (process.stdout, process.stderr):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    failure = "public-file GET exceeded the transport deadline"
                    break
                for key, _ in selector.select(min(remaining, 1)):
                    chunk = os.read(key.fd, 1024 * 1024)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    elif key.fileobj is process.stderr:
                        stderr_tail = (stderr_tail + chunk)[-8192:]
                    elif len(raw) + len(chunk) > limit:
                        raw.extend(chunk[:max(0, limit - len(raw))])
                        failure = "public-file GET exceeded the response bound"
                        break
                    else:
                        raw.extend(chunk)
                if failure:
                    break
        if failure:
            process.kill()
        returncode = process.wait(timeout=PROCESS_EXIT_TIMEOUT_SECONDS)
    except BaseException as exc:
        # An EOF does not prove the child exited. Keep the bytes already read
        # on wait timeouts or reader failures, and terminate before reporting.
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        captured = bytes(raw)
        response = response_metadata(captured, process.returncode, stderr_tail)
        raise TransportFailure("public-file transport interrupted: " + type(exc).__name__, captured, response) from exc
    finally:
        if process.poll() is None:
            process.kill(); process.wait(timeout=5)
        process.stdout.close(); process.stderr.close()
    captured = bytes(raw)
    response = response_metadata(captured, returncode, stderr_tail)
    if failure:
        raise TransportFailure(failure, captured, response)
    return captured, response


def retain_incomplete(store, plan_bytes, tool, programs, profile, raw, response, error):
    schema = "wikilean.public-file-incomplete-attempt/v1"
    body = raw[:DIAGNOSTIC_BODY_LIMIT]
    files = {"plan.json": plan_bytes, "tool.json": core.canonical(tool), "profile.json": core.canonical(profile),
        "request.json": core.canonical(core.parameters(core.parse(plan_bytes, "plan"), profile)),
        "attempt.json": core.canonical({"status": "incomplete-unreviewed-attempt", "recorded_at": now(),
            "failure_type": type(error).__name__, "captured_bytes": len(raw), "captured_sha256": core.sha(raw),
            "body_complete": not isinstance(error, TransportFailure) and response is not None and response["curl_exit_code"] == 0,
            "retained_bytes": len(body), "retained_sha256": core.sha(body), "body_truncated": len(body) != len(raw),
            "response": response}), "raw/response-prefix.bin": body,
        **{"implementation/" + name: data for name, data in programs.items()}}
    return core.archive.publish(core.archive.manifest_files(files, schema), store.parent / (store.name + "-incomplete"), schema)


def acquire(plan_path, store, curl):
    require_startup()
    curl = curl.resolve(strict=True)
    raw_plan = core.read_regular(plan_path, 1024 * 1024)
    plan = core.parse(raw_plan, "reviewed public-file plan")
    core.require(raw_plan == core.canonical(plan), "reviewed public-file plan must be canonical")
    tool, programs = runtime_identity(curl)
    profile = core.validate_tool(tool)
    core.validate_plan(plan, profile)
    raw, response = b"", None
    try:
        core.require(runtime_identity(curl) == (tool, programs), "acquisition runtime changed before request")
        print("Acquiring the reviewed ProofWiki compressed dump (1/1)", file=sys.stderr, flush=True)
        raw, response = transport(plan, profile, curl)
        core.normalize(plan, raw, response, profile)
        core.require(runtime_identity(curl) == (tool, programs) and core.read_regular(plan_path) == raw_plan,
            "plan or acquisition runtime changed during request")
        files = core.capture_files(plan, raw, response, tool, programs, now())
    except BaseException as exc:
        if isinstance(exc, TransportFailure):
            raw, response = exc.raw, exc.response
        try:
            diagnostic = retain_incomplete(store, raw_plan, tool, programs, profile, raw, response, exc)
            print(f"Incomplete attempt retained: {diagnostic}", file=sys.stderr)
        except Exception:
            print("Incomplete attempt could not be retained", file=sys.stderr)
        raise
    target = core.archive.publish(files, store, core.CAPTURE_SCHEMA)
    core.verify_capture(target)
    return target


def export(capture_path, store):
    core.require(capture_path != store and capture_path not in store.parents and store not in capture_path.parents,
        "capture and export store must have disjoint ancestry")
    profile, programs = core.current_profile(), implementation()
    *_, capture = core.verify_capture(capture_path)
    files = core.build_export(capture, profile, programs, now())
    core.require(core.current_profile() == profile and implementation() == programs, "normalizer changed during export")
    target = core.archive.publish(files, store, core.EXPORT_SCHEMA)
    core.verify_export(target)
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("acquire", "export", "verify-capture", "verify-export"))
    for name in ("plan", "capture", "store", "bundle", "curl"):
        parser.add_argument("--" + name, type=Path)
    args = parser.parse_args()
    try:
        if args.mode == "acquire":
            if args.plan is None or args.store is None or args.capture is not None or args.bundle is not None:
                parser.error("acquire requires --plan and --store")
            print(acquire(args.plan, args.store, args.curl or Path("/usr/bin/curl")))
        elif args.mode == "export":
            if args.capture is None or args.store is None or any(value is not None for value in (args.plan, args.bundle, args.curl)):
                parser.error("export requires --capture and --store")
            print(export(args.capture, args.store))
        else:
            if args.bundle is None or any(value is not None for value in (args.plan, args.capture, args.store, args.curl)):
                parser.error("verification requires only --bundle")
            implementation()
            if args.mode == "verify-export":
                print(core.canonical(core.verify_export(args.bundle)).decode())
            else:
                plan, raw, *_ = core.verify_capture(args.bundle)
                print(core.canonical({"source": plan["source"], "sha256": core.sha(raw), "bytes": len(raw)}).decode())
            implementation()
    except (core.EvidenceError, core.contracts.VerificationError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(f"Public-file source operation failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
