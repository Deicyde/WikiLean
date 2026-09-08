#!/usr/bin/env python3
"""Acquire exact official GitHub Mathlib source/docs evidence through fresh GETs.

Run with isolated CPython3.12 (-I -S). Existing GitHub host authentication is
used by gh; tokens and credential-bearing redirect URLs are never recorded.
The source archive and pages ZIP are freshly downloaded, even when earlier
copies exist. No receipt is inferred from caller-supplied retained content.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import platform
import shutil
import subprocess
import sys
import threading
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent))
import mathlib_source_evidence as evidence


def environment():
    result = {"PATH": "/opt/homebrew/bin:/usr/bin:/bin", "LANG": "C", "LC_ALL": "C",
              "GH_HOST": "github.com", "GH_PROMPT_DISABLED": "1", "NO_COLOR": "1"}
    # Pass only the existing official GitHub authentication/configuration route.
    # In particular GH_DEBUG, proxy, paging, browser and extension settings cannot
    # introduce log output or alternate transport into the captured operation.
    for key in ("HOME", "GH_CONFIG_DIR", "XDG_CONFIG_HOME", "GH_TOKEN", "GITHUB_TOKEN"):
        if key in os.environ:
            result[key] = os.environ[key]
    return result


def require_startup():
    if platform.python_implementation() != "CPython" or sys.version_info[:2] != (3, 12) \
            or not all((sys.flags.isolated, sys.flags.no_site, sys.flags.safe_path, sys.flags.ignore_environment)):
        raise evidence.EvidenceError("use isolated CPython3.12 with -I -S")


def runtime_identity(gh):
    profile = evidence.current_profile()
    version = subprocess.run([str(gh), "--version"], env=environment(), check=True,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10).stdout.decode().splitlines()[0]
    tool = {"schema": "wikilean.mathlib-source-acquirer-tool/v1", "profile_id": profile["profile_id"], "files": profile["files"],
        "python": {"sha256": evidence.sha(evidence.read_regular(Path(sys.executable).resolve())),
                   "version": "CPython " + platform.python_version() + " -I -S"},
        "gh": {"sha256": evidence.sha(evidence.read_regular(gh)), "version": version}}
    evidence.validate_tool(tool)
    return tool


def transport(spec, gh):
    params = evidence.parameters(spec)
    args = [str(gh), "api", "--hostname", "github.com", "--method", params["method"]]
    for key, value in sorted(params["headers"].items()):
        args += ["--header", key + ": " + value]
    args.append(params["path"])
    # Streaming limits the body before publication; a timer bounds a stalled
    # stream without retaining stderr that could contain signed redirect URLs.
    with subprocess.Popen(args, env=environment(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
        timer = threading.Timer(600, process.kill)
        timer.daemon = True
        timer.start()
        try:
            chunks, size = [], 0
            while True:
                chunk = process.stdout.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > spec[4]:
                    process.kill()
                    raise evidence.EvidenceError(f"{spec[1]}: response exceeded its bound")
                chunks.append(chunk)
            if process.wait() != 0:
                raise evidence.EvidenceError(f"{spec[1]}: GitHub API request failed")
            return b"".join(chunks)
        finally:
            timer.cancel()


def acquire(plan_path, store, gh):
    require_startup()
    gh = gh.resolve(strict=True)
    plan_bytes = evidence.read_regular(plan_path, 1024 * 1024)
    plan = evidence.validate_plan(evidence.parse(plan_bytes, "reviewed plan"))
    if evidence.canonical(plan) != plan_bytes:
        raise evidence.EvidenceError("reviewed plan must be canonical")
    tool = runtime_identity(gh)
    raw = {}
    try:
        for index, spec in enumerate(evidence.request_specs(plan)):
            print(f"Acquiring {spec[1]} ({index + 1}/7)", file=sys.stderr, flush=True)
            raw[spec[1]] = transport(spec, gh)
        if runtime_identity(gh) != tool or evidence.read_regular(plan_path) != plan_bytes:
            raise evidence.EvidenceError("plan or acquisition implementation changed during requests")
        evidence.normalize(plan, raw)
    except BaseException as exc:
        # Preserve diagnostics without issuing a complete acquisition receipt.
        # This schema is deliberately rejected by verify_capture and exporters.
        failed = {"plan.json": plan_bytes, "tool.json": evidence.canonical(tool),
                  **{"raw/" + name: data for name, data in raw.items()},
                  "attempt.json": evidence.canonical({"status": "incomplete-unreviewed-attempt",
                      "recorded_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                      "failure_type": type(exc).__name__, "received_objects": sorted(raw)})}
        failed_schema = "wikilean.mathlib-source-incomplete-attempt/v1"
        try:
            retained = evidence.publish(evidence.manifest_files(failed, failed_schema),
                store.parent / (store.name + "-incomplete"), failed_schema)
            print(f"Incomplete attempt retained: {retained}", file=sys.stderr)
        except Exception:
            print("Incomplete attempt could not be retained", file=sys.stderr)
        raise
    when = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    files = evidence.capture_files(plan, raw, tool, when)
    result = evidence.publish(files, store, evidence.CAPTURE_SCHEMA)
    evidence.verify_capture(result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--gh", type=Path, default=Path(shutil.which("gh") or "/nonexistent"))
    args = parser.parse_args()
    try:
        print(acquire(args.plan, args.store, args.gh))
    except (evidence.EvidenceError, evidence.contracts.VerificationError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"Mathlib acquisition failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
