#!/usr/bin/env python3
"""Capture a fresh scoped OpenAlex/arXiv citation walk, export it, or independently verify it."""
from __future__ import annotations

import argparse
import base64
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
import openalex_source_evidence as core

LOADED_IMPLEMENTATION = {name: core.read_regular(core.ROOT / name) for name in core.TOOL_FILES}
ENVIRONMENT = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}
TRAILER = b"\nWIKILEAN_OPENALEX_RESULT\t"
PROCESS_EXIT_TIMEOUT_SECONDS = 5


def implementation():
    core.origins()
    core.require(Path(core.__file__).resolve() == core.ROOT / "brain/openalex_source_evidence.py", "OPENALEX core origin differs")
    programs = {name: core.read_regular(core.ROOT / name) for name in core.TOOL_FILES}
    core.require(programs == LOADED_IMPLEMENTATION, "OPENALEX implementation changed since import")
    return programs


def require_startup():
    core.require(platform.python_implementation() == "CPython" and sys.version_info[:2] == (3, 12)
        and sys.flags.isolated == 1 and sys.flags.no_site == 1, "OPENALEX acquisition requires CPython3.12 -I -S")


def now():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def runtime_identity(curl):
    require_startup()
    profile, programs = core.current_profile(), implementation()
    before = core.sha(core.read_regular(curl))
    version = subprocess.run([str(curl), "-q", "--version"], env=dict(ENVIRONMENT), check=True,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10).stdout.decode("utf-8").splitlines()[0]
    core.require(core.sha(core.read_regular(curl)) == before, "curl changed while identifying it")
    tool = {"schema": core.TOOL_SCHEMA, "profile_id": profile["profile_id"], "files": profile["files"],
        "python": {"sha256": core.sha(core.read_regular(Path(sys.executable).resolve(strict=True))),
            "version": "CPython " + platform.python_version() + " -I -S"},
        "curl": {"sha256": before, "version": version}}
    core.validate_tool(tool)
    return tool, programs


def command(parameters, curl):
    args = [str(curl), "-q", "--silent", "--show-error", "--fail-with-body", "--request", "GET",
        "--proxy", "", "--noproxy", "*", "--proto", "=https", "--proto-redir", "=https", "--max-redirs", "0",
        "--connect-timeout", "30", "--max-time", "120", "--max-filesize", str(core.POLICY["maximum_response_bytes"]),
        "--output", "-", "--write-out", "%{stderr}\\nWIKILEAN_OPENALEX_RESULT\\t%{http_code}\\t%{content_type}\\t%header{retry-after}\\t%header{location}\\t%header{x-ratelimit-limit-usd}\\t%header{x-ratelimit-prepaid-remaining-usd}\\t%header{x-ratelimit-cost-usd}\\t%header{x-ratelimit-remaining-usd}\\n"]
    for key, value in sorted(parameters["headers"].items()):
        args.extend(["--header", key + ": " + value])
    return [*args, "--url", parameters["uri"]]


class TransportFailure(core.EvidenceError):
    def __init__(self, message, raw, response):
        super().__init__(message)
        self.raw, self.response = raw, response


def parsed_retry_after(raw):
    if not raw:
        return None
    if re.fullmatch(rb"[0-9]{1,15}", raw):
        return {"kind": "delay-seconds", "seconds": int(raw)}
    return {"kind": "unsupported-or-excessive"}


def response_metadata(raw, code, tail):
    match = re.search(re.escape(TRAILER) + rb"([0-9]{3})\t([^\t\r\n]*)\t([^\t\r\n]*)\t([^\t\r\n]*)\t([^\t\r\n]*)\t([^\t\r\n]*)\t([^\t\r\n]*)\t([^\t\r\n]*)\n\Z", tail)
    status, media, after, location = 0, "", None, None
    quota = {key: "" for key in ("limit_usd", "prepaid_remaining_usd", "cost_usd", "remaining_usd")}
    if match:
        status = int(match[1]); after = parsed_retry_after(match[3])
        token = match[2].split(b";", 1)[0].strip().lower()
        if re.fullmatch(rb"[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+", token): media = token.decode("ascii")
        if match[4]:
            location = match[4].decode("ascii") if re.fullmatch(rb"https://api\.openalex\.org/works/W[0-9]{1,20}(?:\?select=id,doi,referenced_works)?", match[4]) else "unsupported"
        for key, value in zip(quota, match.groups()[4:]):
            quota[key] = value.decode("ascii") if re.fullmatch(rb"[0-9]{1,12}(?:\.[0-9]{1,12})?", value) else ("" if not value else "unsupported")
    return {"curl_exit_code": int(code), "http_status": status, "content_type": media,
        "sha256": core.sha(raw), "bytes": len(raw), "retry_after": after, "location": location, "quota": quota}


def transport(parameters, curl):
    raw, tail, failure = bytearray(), b"", None
    code = -1
    limit = core.POLICY["maximum_response_bytes"]
    process = subprocess.Popen(command(parameters, curl), env=dict(ENVIRONMENT), stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True)
    try:
        deadline = time.monotonic() + 125
        with selectors.DefaultSelector() as selector:
            for stream in (process.stdout, process.stderr):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    failure = "OPENALEX response deadline exceeded"
                    break
                for key, _ in selector.select(min(remaining, 1)):
                    chunk = os.read(key.fd, 1024 * 1024)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    elif key.fileobj is process.stderr:
                        tail = (tail + chunk)[-8192:]
                    elif len(raw) + len(chunk) > limit:
                        raw.extend(chunk[:max(0, limit - len(raw))])
                        failure = "OPENALEX response size bound exceeded"
                        break
                    else:
                        raw.extend(chunk)
                if failure:
                    break
        if failure:
            process.kill()
        code = process.wait(timeout=PROCESS_EXIT_TIMEOUT_SECONDS)
    except BaseException as exc:
        failure = "OPENALEX transport interrupted: " + type(exc).__name__
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        code = process.returncode if process.returncode is not None else code
        process.stdout.close()
        process.stderr.close()
    result = bytes(raw)
    response = response_metadata(result, code, tail)
    if failure:
        raise TransportFailure(failure, result, response)
    return result, response


def retain_incomplete(store, plan, tool, programs, records, pending, error, selector):
    schema = "wikilean.openalex-incomplete-attempt/v1"
    profile = core.validate_tool(tool)
    files = {"plan.json": core.canonical(plan), "tool.json": core.canonical(tool), "profile.json": core.canonical(profile),
        "selector.json": selector, "transcript.json": core.canonical({"completed_requests": records, "failed_request": pending}),
        "failure.json": core.canonical({"schema": schema, "authority": False, "recorded_at": now(),
            "failure_type": type(error).__name__, "retained_attempt_count": len(records)}),
        **{"implementation/" + name: data for name, data in programs.items()}}
    return core.archive.publish(core.archive.manifest_files(files, schema), store.parent / (store.name + "-incomplete"), schema)


def acquire(plan_path, store, curl, selector_path):
    require_startup()
    curl = curl.resolve(strict=True)
    raw_plan = core.read_regular(plan_path, 1024 * 1024)
    plan = core.validate_plan(core.parse(raw_plan, "plan"))
    core.require(raw_plan == core.canonical(plan), "reviewed OPENALEX plan must be canonical")
    tool, programs = runtime_identity(curl)
    selector = core.validate_selector(plan, core.read_regular(selector_path, core.POLICY["maximum_selector_bytes"]), programs["brain/ingest/openalex_citations.py"])
    records, pending = [], None
    state = core.WalkState(plan, programs["brain/ingest/openalex_citations.py"])
    try:
        while state.next_name is not None:
            core.require(len(records) < core.POLICY["maximum_attempts"], "OPENALEX request budget exceeded")
            parameters = core.parameters(state.next_name)
            key = core.sha(core.canonical(parameters))
            core.require(runtime_identity(curl) == (tool, programs) and core.read_regular(plan_path) == raw_plan,
                "OPENALEX runtime or plan changed before request")
            core.require(state.filtered_attempts < core.POLICY["maximum_filtered_attempts"] or state.next_name["phase"].split(":")[0] in {"docs", "direct", "arxiv"}, "OpenAlex keyless request budget exhausted before request")
            interval = core.POLICY["arxiv_interval_milliseconds"] if state.next_name["phase"].startswith("arxiv:") else core.POLICY["minimum_request_interval_milliseconds"]
            time.sleep(interval / 1000)
            print(f"Acquiring OpenAlex {state.next_name['phase']} ({state.count + 1}, filtered {state.filtered_attempts}, attempt {state.ordinal})", file=sys.stderr, flush=True)
            pending = {"request": parameters, "response": None, "body_base64": "", "attempt": state.ordinal,
                "outcome": "failed", "retry_delay_seconds": 0}
            try:
                raw, response = transport(parameters, curl)
            except TransportFailure as exc:
                pending.update(response=exc.response, body_base64=base64.b64encode(exc.raw).decode("ascii"))
                raise
            pending.update(response=response, body_base64=base64.b64encode(raw).decode("ascii"))
            if (response["curl_exit_code"] == 0 and response["http_status"] == 200) or (state.next_name["phase"].startswith("direct:") and ((response["http_status"] == 301 and response["curl_exit_code"] == 0) or (response["http_status"] == 404 and response["curl_exit_code"] in (0, 22)))):
                pending["outcome"] = "succeeded"
            else:
                delay = core.retry_delay(response, state.ordinal)
                core.require(delay is not None, "OPENALEX failed response is nonretryable or retries exhausted")
                pending["retry_delay_seconds"] = delay
            state.accept(pending)
            records.append(pending)
            delay = pending["retry_delay_seconds"]
            pending = None
            if delay:
                print(f"Retrying OpenAlex after {delay}s; all failed bytes retained", file=sys.stderr, flush=True)
                time.sleep(delay)
        core.require(runtime_identity(curl) == (tool, programs) and core.read_regular(plan_path) == raw_plan,
            "OPENALEX runtime or plan changed during walk")
        files = core.capture_files(plan, records, tool, programs, now(), selector)
    except BaseException as exc:
        try:
            path = retain_incomplete(store, plan, tool, programs, records, pending, exc, selector)
            print(f"Incomplete OPENALEX attempt retained: {path}", file=sys.stderr)
        except Exception:
            print("Incomplete OPENALEX attempt could not be retained", file=sys.stderr)
        raise
    target = core.archive.publish(files, store, core.CAPTURE_SCHEMA)
    core.verify_capture(target)
    return target


def export(capture_path, store):
    core.require(capture_path != store and capture_path not in store.parents and store not in capture_path.parents,
        "OPENALEX capture and export stores must have disjoint ancestry")
    profile, programs = core.current_profile(), implementation()
    capture, _ = core.verify_capture(capture_path)
    files = core.build_export(capture, profile, programs, now())
    core.require(core.current_profile() == profile and implementation() == programs, "OPENALEX normalizer changed")
    target = core.archive.publish(files, store, core.EXPORT_SCHEMA)
    core.verify_export(target)
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("acquire", "export", "verify-capture", "verify-export"))
    for name in ("plan", "capture", "store", "bundle", "curl", "selector"):
        parser.add_argument("--" + name, type=Path)
    args = parser.parse_args()
    try:
        if args.mode == "acquire":
            if args.plan is None or args.store is None or args.selector is None or args.capture is not None or args.bundle is not None:
                parser.error("acquire requires --plan, --selector and --store")
            print(acquire(args.plan, args.store, args.curl or Path("/usr/bin/curl"), args.selector))
        elif args.mode == "export":
            if args.capture is None or args.store is None or any(v is not None for v in (args.plan, args.bundle, args.curl, args.selector)):
                parser.error("export requires --capture and --store")
            print(export(args.capture, args.store))
        else:
            if args.bundle is None or any(v is not None for v in (args.plan, args.capture, args.store, args.curl, args.selector)):
                parser.error("verification requires --bundle only")
            implementation()
            result = core.verify_export(args.bundle) if args.mode == "verify-export" else core.verify_capture(args.bundle)[1]
            print(core.canonical(result).decode())
            implementation()
    except (core.EvidenceError, core.contracts.VerificationError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print("OPENALEX operation failed: " + type(exc).__name__, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
