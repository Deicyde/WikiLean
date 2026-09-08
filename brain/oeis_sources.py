#!/usr/bin/env python3
"""Acquire fresh bounded OEIS names/entries or replay their restricted export."""
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
import oeis_source_evidence as core

LOADED = {name: core.io.read(core.ROOT / name) for name in core.TOOL_FILES}
ENVIRONMENT = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}
TRAILER = b"\nWIKILEAN_OEIS_RESULT\t"


def implementation():
    core.origins()
    core.require(Path(core.__file__).resolve(strict=True) == core.ROOT / "brain/oeis_source_evidence.py", "OEIS core origin differs")
    programs = {name: core.io.read(core.ROOT / name) for name in core.TOOL_FILES}
    core.require(programs == LOADED, "OEIS implementation changed after loading")
    return programs


def require_startup():
    core.require(platform.python_implementation() == "CPython" and sys.version_info[:2] == (3, 12)
        and sys.flags.isolated and sys.flags.no_site, "OEIS acquisition requires CPython3.12 -I -S")


def now():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def runtime_identity(curl):
    require_startup()
    profile, programs = core.current_profile(), implementation()
    before = core.sha(core.io.read(curl))
    version = subprocess.run([str(curl), "-q", "--version"], env=dict(ENVIRONMENT), check=True,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10).stdout.decode().splitlines()[0]
    core.require(core.sha(core.io.read(curl)) == before, "curl changed while identifying it")
    tool = {"schema": core.TOOL_SCHEMA, "profile_id": profile["profile_id"], "files": profile["files"],
        "python": {"sha256": core.sha(core.io.read(Path(sys.executable).resolve(strict=True))),
            "version": "CPython " + platform.python_version() + " -I -S"}, "curl": {"sha256": before, "version": version}}
    core.validate_tool(tool)
    return tool, programs


def command(spec, curl):
    params = core.parameters(spec)
    args = [str(curl), "-q", "--silent", "--show-error", "--fail-with-body", "--request", "GET",
        "--proxy", "", "--noproxy", "*", "--proto", "=https", "--proto-redir", "=https", "--max-redirs", "0",
        "--connect-timeout", "30", "--max-time", str(core.POLICY["timeout_seconds"]), "--max-filesize", str(spec[3]),
        "--output", "-", "--write-out", "%{stderr}\\nWIKILEAN_OEIS_RESULT\\t%{http_code}\\t%{content_type}\\n"]
    for name, value in sorted(params["headers"].items()): args.extend(["--header", name + ": " + value])
    return [*args, "--url", params["uri"] + ("?" + params["query_urlencoded"] if params["query_urlencoded"] else "")]


class TransportFailure(core.io.ExportError):
    def __init__(self, message, raw, response):
        super().__init__(message)
        self.raw, self.response = raw, response


def response_metadata(raw, code, tail):
    match = re.search(re.escape(TRAILER) + rb"([0-9]{3})\t([^\r\n]*)\n\Z", tail)
    status, media = 0, ""
    if match:
        status = int(match[1])
        token = match[2].split(b";", 1)[0].strip().lower()
        if re.fullmatch(rb"[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+", token): media = token.decode("ascii")
    return {"curl_exit_code": int(code), "http_status": status, "content_type": media, "sha256": core.sha(raw), "bytes": len(raw)}


def transport(spec, curl):
    raw, tail, failure, code = bytearray(), b"", None, -1
    process = subprocess.Popen(command(spec, curl), env=dict(ENVIRONMENT), stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True)
    try:
        deadline = time.monotonic() + core.POLICY["timeout_seconds"] + 5
        with selectors.DefaultSelector() as selector:
            for stream in (process.stdout, process.stderr):
                os.set_blocking(stream.fileno(), False); selector.register(stream, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    failure = "OEIS transport deadline exceeded"; break
                for key, _ in selector.select(min(remaining, 1)):
                    chunk = os.read(key.fd, 1024 * 1024)
                    if not chunk: selector.unregister(key.fileobj)
                    elif key.fileobj is process.stderr: tail = (tail + chunk)[-8192:]
                    elif len(raw) + len(chunk) > spec[3]:
                        raw.extend(chunk[:max(0, spec[3] - len(raw))]); failure = "OEIS response bound exceeded"; break
                    else: raw.extend(chunk)
                if failure: break
        if failure: process.kill()
        code = process.wait(timeout=5)
    except Exception as exc:
        failure = "OEIS transport interrupted: " + type(exc).__name__
    finally:
        if process.poll() is None:
            process.kill(); process.wait(timeout=5)
        code = process.returncode if process.returncode is not None else code
        process.stdout.close(); process.stderr.close()
    result = bytes(raw)
    response = response_metadata(result, code, tail)
    if failure: raise TransportFailure(failure, result, response)
    return result, response


def retain_incomplete(store, plan, tool, programs, raw, responses, pending, error):
    schema = "wikilean.oeis-incomplete-attempt/v1"
    files = {"plan.json": core.canonical(plan), "tool.json": core.canonical(tool),
        "profile.json": core.canonical(core.validate_tool(tool)), "responses.json": core.canonical(responses),
        "failure.json": core.canonical({"schema": schema, "authority": False, "recorded_at": now(), "failure_type": type(error).__name__,
            "received_objects": sorted(raw), "pending_request": pending}),
        **{"raw/" + name: data for name, data in raw.items()},
        **{"implementation/" + name: data for name, data in programs.items()}}
    return core.archive.publish(core.archive.manifest_files(files, schema), store.parent / (store.name + "-incomplete"), schema)


def acquire(plan_path, roots, store, curl):
    require_startup()
    curl = curl.resolve(strict=True)
    plan_bytes = core.io.read(plan_path)
    plan = core.validate_plan(core.parse(plan_bytes, "OEIS plan"))
    core.require(plan_bytes == core.canonical(plan), "OEIS plan must be canonical")
    parents = core.capture_parents(plan, roots)
    tool, programs = runtime_identity(curl)
    raw, responses, pending, total = {}, {}, None, 0
    try:
        for index, spec in enumerate(core.request_specs(plan), 1):
            core.require(runtime_identity(curl) == (tool, programs) and core.io.read(plan_path) == plan_bytes, "OEIS runtime or plan changed before request")
            time.sleep(core.POLICY["minimum_request_interval_milliseconds"] / 1000)
            print(f"Acquiring OEIS {spec[0]} ({index}/{len(plan['anchored_qids']) + 1})", file=sys.stderr, flush=True)
            pending = {"object": spec[0], "request": core.parameters(spec)}
            try:
                data, response = transport(spec, curl)
            except TransportFailure as exc:
                raw[spec[0]], responses[spec[0]] = exc.raw, exc.response
                raise
            raw[spec[0]], responses[spec[0]] = data, response
            total += len(data)
            core.require(total <= core.POLICY["maximum_total_response_bytes"], "OEIS response budget exceeded")
            core.validate_response(spec, data, response)
            if spec[0] == "names_gz": core.names_inventory(data, plan["minimum_names_inventory"])
            else: core.entry_document(spec[0].removeprefix("entry-").upper(), data)
            pending = None
        core.require(runtime_identity(curl) == (tool, programs) and core.io.read(plan_path) == plan_bytes, "OEIS runtime or plan changed during capture")
        files = core.capture_files(plan, raw, responses, tool, programs, parents, now())
    except BaseException as exc:
        try:
            path = retain_incomplete(store, plan, tool, programs, raw, responses, pending, exc)
            print(f"Incomplete OEIS attempt retained: {path}", file=sys.stderr)
        except Exception:
            print("Incomplete OEIS attempt could not be retained", file=sys.stderr)
        raise
    target = core.archive.publish(files, store, core.CAPTURE_SCHEMA)
    core.verify_capture(target, roots)
    return target


def export(capture_path, roots, store):
    core.require(capture_path != store and capture_path not in store.parents and store not in capture_path.parents, "OEIS capture/export stores overlap")
    profile, programs = core.current_profile(), implementation()
    capture, _ = core.verify_capture(capture_path, roots)
    files = core.build_export(capture, profile, programs, roots, now())
    core.require(core.current_profile() == profile and implementation() == programs, "OEIS normalizer changed")
    target = core.archive.publish(files, store, core.EXPORT_SCHEMA)
    core.verify_export(target, roots)
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("acquire", "export", "verify-capture", "verify-export"))
    for name in ("plan", "capture", "store", "bundle", "curl"): parser.add_argument("--" + name, type=Path)
    parser.add_argument("--root", action="append", default=[])
    args = parser.parse_args()
    try:
        roots = {}
        for value in args.root:
            name, separator, path = value.partition("=")
            core.require(separator and name and path and name not in roots, "roots require unique NAME=ABSOLUTE_PATH bindings")
            roots[name] = Path(path); core.io.real_path(roots[name])
        if args.mode == "acquire":
            if args.plan is None or args.store is None or args.capture is not None or args.bundle is not None: parser.error("acquire requires plan and store")
            print(acquire(args.plan, roots, args.store, args.curl or Path("/usr/bin/curl")))
        elif args.mode == "export":
            if args.capture is None or args.store is None or any(v is not None for v in (args.plan, args.bundle, args.curl)): parser.error("export requires capture and store")
            print(export(args.capture, roots, args.store))
        else:
            if args.bundle is None or any(v is not None for v in (args.plan, args.capture, args.store, args.curl)): parser.error("verification requires bundle")
            implementation()
            result = core.verify_export(args.bundle, roots) if args.mode == "verify-export" else core.verify_capture(args.bundle, roots)[1]
            print(core.canonical(result).decode())
            implementation()
    except (core.io.ExportError, core.archive.EvidenceError, core.contracts.VerificationError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print("OEIS operation failed: " + type(exc).__name__, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__": raise SystemExit(main())
