#!/usr/bin/env python3
"""Acquire or independently verify private public-Git source evidence.

The acquire command always performs the two exact fresh GETs. It has no import
existing-files mode. Export consumes a complete verified capture and rebuilds
every Git blob/tree from retained bytes. Use isolated CPython3.12 with -I -S.
"""
from __future__ import annotations

import argparse
import datetime as dt
import platform
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import public_git_source_evidence as core

LOADED_IMPLEMENTATION = {name: core.read_regular(core.ROOT / name) for name in core.TOOL_FILES}


def implementation():
    core.origins()
    actual = {name: core.read_regular(core.ROOT / name) for name in core.TOOL_FILES}
    core.require(actual == LOADED_IMPLEMENTATION, "public Git implementation changed after loading")
    return actual


def now():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def runtime_identity(gh):
    core.github.require_startup()
    profile = core.current_profile()
    programs = implementation()
    core.verify_programs(profile, programs)
    before = core.sha(core.read_regular(gh))
    result = subprocess.run([str(gh), "--version"], env=core.github.environment(), check=True,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10)
    core.require(core.sha(core.read_regular(gh)) == before, "GitHub CLI changed while identifying it")
    tool = {"schema": core.TOOL_SCHEMA, "profile_id": profile["profile_id"], "files": profile["files"],
        "python": {"sha256": core.sha(core.read_regular(Path(sys.executable).resolve(strict=True))),
                   "version": "CPython " + platform.python_version() + " -I -S"},
        "gh": {"sha256": before, "version": result.stdout.decode().splitlines()[0]}}
    core.validate_tool(tool)
    return tool, programs


def acquire(plan_path, store, gh):
    core.github.require_startup()
    gh = gh.resolve(strict=True)
    raw_plan = core.read_regular(plan_path, 1024 * 1024)
    plan = core.validate_plan(core.parse(raw_plan, "reviewed public Git plan"))
    core.require(raw_plan == core.canonical(plan), "reviewed public Git plan must be canonical")
    tool, programs = runtime_identity(gh)
    raw = {}
    try:
        for index, spec in enumerate(core.request_specs(plan), 1):
            # Check the selected exact executable and complete program generation
            # before each network operation, not just at the end of the batch.
            core.require(runtime_identity(gh) == (tool, programs), "acquisition runtime changed before request")
            print(f"Acquiring {plan['repository']} {spec[1]} ({index}/2)", file=sys.stderr, flush=True)
            raw[spec[1]] = core.github.transport(spec, gh)
        core.require(runtime_identity(gh) == (tool, programs) and core.read_regular(plan_path) == raw_plan,
                     "plan or acquisition runtime changed during requests")
        files = core.capture_files(plan, raw, tool, programs, now())
    except BaseException as exc:
        diagnostic_schema = "wikilean.public-git-incomplete-attempt/v1"
        diagnostic = {"plan.json": raw_plan, "tool.json": core.canonical(tool),
            **{"raw/" + name: data for name, data in raw.items()},
            "attempt.json": core.canonical({"status": "incomplete-unreviewed-attempt", "recorded_at": now(),
                "failure_type": type(exc).__name__, "received_objects": sorted(raw)})}
        try:
            result = core.archive.publish(core.archive.manifest_files(diagnostic, diagnostic_schema),
                store.parent / (store.name + "-incomplete"), diagnostic_schema)
            print(f"Incomplete attempt retained: {result}", file=sys.stderr)
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
    _, _, _, capture = core.verify_capture(capture_path)
    files = core.build_export(capture, profile, programs, now())
    core.require(core.current_profile() == profile and implementation() == programs, "normalizer changed during export")
    target = core.archive.publish(files, store, core.EXPORT_SCHEMA)
    core.verify_export(target)
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("acquire", "export", "verify-capture", "verify-export"))
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--capture", type=Path)
    parser.add_argument("--store", type=Path)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--gh", type=Path)
    args = parser.parse_args()
    try:
        if args.mode == "acquire":
            if args.plan is None or args.store is None or args.capture is not None or args.bundle is not None:
                parser.error("acquire requires --plan and --store")
            print(acquire(args.plan, args.store, args.gh or Path(shutil.which("gh") or "/nonexistent")))
        elif args.mode == "export":
            if args.capture is None or args.store is None or any(value is not None for value in (args.plan, args.bundle, args.gh)):
                parser.error("export requires --capture and --store")
            print(export(args.capture, args.store))
        else:
            if args.bundle is None or any(value is not None for value in (args.plan, args.capture, args.store, args.gh)):
                parser.error("verification requires only --bundle")
            implementation()
            if args.mode == "verify-export":
                print(core.canonical(core.verify_export(args.bundle)).decode())
            else:
                plan, _, _, _ = core.verify_capture(args.bundle)
                print(core.canonical({"source": plan["source"], "commit": plan["commit"]}).decode())
            implementation()
    except (core.EvidenceError, core.contracts.VerificationError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(f"Public Git source operation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
