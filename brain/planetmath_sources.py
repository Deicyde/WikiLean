#!/usr/bin/env python3
"""Acquire fresh PlanetMath repository evidence or verify its private export."""
from __future__ import annotations

import argparse
import datetime as dt
import os
import platform
import selectors
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import planetmath_source_evidence as core
import planetmath_normalization as normalization

LOADED = {name: core.io.read(core.ROOT / name) for name in core.TOOL_FILES}


def implementation():
    core.origins()
    for module, path in ((core, "brain/planetmath_source_evidence.py"), (normalization, "brain/planetmath_normalization.py")):
        core.require(Path(module.__file__).resolve(strict=True) == core.ROOT / path, "PlanetMath module origin differs")
    core.require(normalization.core is core, "PlanetMath normalizer imported a different core")
    programs = {name: core.io.read(core.ROOT / name) for name in core.TOOL_FILES}
    core.require(programs == LOADED, "PlanetMath implementation changed after loading")
    return programs


def now(): return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def runtime_identity(gh):
    core.github.require_startup()
    profile, programs = core.current_profile(), implementation()
    before = core.sha(core.io.read(gh))
    version = subprocess.run([str(gh), "--version"], env=core.github.environment(), check=True,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10).stdout.decode().splitlines()[0]
    core.require(core.sha(core.io.read(gh)) == before, "GitHub CLI changed while identifying it")
    tool = {"schema": core.TOOL_SCHEMA, "profile_id": profile["profile_id"], "files": profile["files"],
        "python": {"sha256": core.sha(core.io.read(Path(sys.executable).resolve(strict=True))), "version": "CPython " + platform.python_version() + " -I -S"},
        "gh": {"sha256": before, "version": version}}
    core.validate_tool(tool)
    return tool, programs


def command(spec, gh):
    params = core.parameters(spec)
    args = [str(gh), "api", "--hostname", "github.com", "--method", "GET"]
    for key, value in sorted(params["headers"].items()): args.extend(["--header", key + ": " + value])
    return [*args, params["path"] + ("?" + params["query_urlencoded"] if params["query_urlencoded"] else "")]


class TransportFailure(core.io.ExportError):
    def __init__(self, message, raw, exit_code):
        super().__init__(message)
        self.raw, self.exit_code = raw, exit_code


def transport(spec, gh):
    raw, failure, code = bytearray(), None, -1
    process = subprocess.Popen(command(spec, gh), env=core.github.environment(), stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, close_fds=True)
    try:
        deadline = time.monotonic() + core.POLICY["timeout_seconds"]
        with selectors.DefaultSelector() as selector:
            os.set_blocking(process.stdout.fileno(), False); selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0: failure = "PlanetMath transport deadline exceeded"; break
                for key, _ in selector.select(min(remaining, 1)):
                    chunk = os.read(key.fd, 1024 * 1024)
                    if not chunk: selector.unregister(key.fileobj)
                    elif len(raw) + len(chunk) > spec[3]:
                        raw.extend(chunk[:max(0, spec[3] - len(raw))]); failure = "PlanetMath response bound exceeded"; break
                    else: raw.extend(chunk)
                if failure: break
        if failure: process.kill()
        code = process.wait(timeout=5)
        if code != 0: failure = "PlanetMath GitHub API request failed"
    except BaseException as exc:
        failure = "PlanetMath transport interrupted: " + type(exc).__name__
    finally:
        if process.poll() is None: process.kill(); process.wait(timeout=5)
        code = process.returncode if process.returncode is not None else code
        process.stdout.close()
    if failure: raise TransportFailure(failure, bytes(raw), code)
    return bytes(raw)


def retain_incomplete(store, plan, tool, programs, raw, records, pending, error):
    schema = "wikilean.planetmath-incomplete-attempt/v1"
    files = {"plan.json": core.canonical(plan), "tool.json": core.canonical(tool), "profile.json": core.canonical(core.validate_tool(tool)),
        "request-results.json": core.canonical(records), "failure.json": core.canonical({"schema": schema, "authority": False,
            "recorded_at": now(), "failure_type": type(error).__name__, "received_objects": sorted(raw), "pending_request": pending}),
        **{"raw/" + name: value for name, value in raw.items()}, **{"implementation/" + name: value for name, value in programs.items()}}
    return core.archive.publish(core.archive.manifest_files(files, schema), store.parent / (store.name + "-incomplete"), schema)


def acquire(plan_path, roots, store, gh):
    core.github.require_startup()
    gh = gh.resolve(strict=True)
    plan_bytes = core.io.read(plan_path)
    plan = core.validate_plan(core.parse(plan_bytes, "PlanetMath plan"))
    core.require(plan_bytes == core.canonical(plan), "PlanetMath plan must be canonical")
    parents = core.capture_parents(plan, roots)
    tool, programs = runtime_identity(gh)
    raw, records, pending, total = {}, [], None, 0
    def fetch(spec):
        nonlocal pending, total
        core.require(runtime_identity(gh) == (tool, programs) and core.io.read(plan_path) == plan_bytes, "PlanetMath runtime or plan changed before request")
        time.sleep(core.POLICY["minimum_request_interval_milliseconds"] / 1000)
        print(f"Acquiring PlanetMath {spec[0]} (request {len(records) + 1})", file=sys.stderr, flush=True)
        pending = {"object": spec[0], "request": core.parameters(spec)}
        try: data = transport(spec, gh)
        except TransportFailure as exc:
            raw[spec[0]] = exc.raw; records.append(core.record(spec, exc.raw, exc.exit_code)); raise
        raw[spec[0]] = data; records.append(core.record(spec, data)); total += len(data)
        core.require(total <= core.POLICY["maximum_total_response_bytes"], "PlanetMath response budget exceeded")
        core.response_record(spec, data, records[-1])
        return data
    try:
        walk = core.ListingWalk(plan)
        while not walk.done:
            walk.accept(fetch(core.listing_spec(walk.pages + 1))); pending = None
        trees, heads = {}, {}
        total_files = total_tree_bytes = 0
        for repo in walk.repositories():
            commit, tree = core.head_identity(repo, fetch(core.head_spec(repo))); pending = None
            files = core.tree_from_archive(fetch(core.archive_spec(repo, commit)), tree); pending = None
            total_files += len(files); total_tree_bytes += sum(len(data) for _, data in files.values())
            core.require(total_files <= core.POLICY["maximum_total_tree_files"] and total_tree_bytes <= core.POLICY["maximum_total_tree_bytes"], "PlanetMath complete tree budget exceeded")
            # The complete tree has already passed its hash and resource checks.
            # The legacy parser reads only these files; keep other blobs out of
            # the acquisition working set while their archive remains retained.
            core.require(not any(part.endswith(".tex") for path in files for part in path.split("/")[:-1]),
                "PlanetMath .tex directories are unreadable by the legacy parser")
            trees[repo["name"]] = {path: value for path, value in files.items() if path.endswith(".tex")}
            heads[repo["name"]] = commit
        normalization.project(plan, trees, heads, core.qid_map(parents[3]), programs)
        core.require(runtime_identity(gh) == (tool, programs) and core.io.read(plan_path) == plan_bytes, "PlanetMath runtime or plan changed during capture")
        del trees, heads, files
        files = core.capture_files(plan, raw, records, tool, programs, now())
    except BaseException as exc:
        try:
            path = retain_incomplete(store, plan, tool, programs, raw, records, pending, exc)
            print(f"Incomplete PlanetMath attempt retained: {path}", file=sys.stderr)
        except Exception: print("Incomplete PlanetMath attempt could not be retained", file=sys.stderr)
        raise
    target = core.archive.publish(files, store, core.CAPTURE_SCHEMA)
    del files, raw
    core.verify_capture(target)
    return target


def export(capture_path, roots, store):
    core.require(capture_path != store and capture_path not in store.parents and store not in capture_path.parents, "PlanetMath capture/export stores overlap")
    profile, programs = core.current_profile(), implementation()
    capture, _ = core.verify_capture(capture_path)
    files = normalization.build_export(capture, profile, programs, roots, now())
    core.require(core.current_profile() == profile and implementation() == programs, "PlanetMath normalizer changed")
    target = core.archive.publish(files, store, core.EXPORT_SCHEMA)
    del files, capture
    normalization.verify_export(target, roots)
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("acquire", "export", "verify-capture", "verify-export"))
    for name in ("plan", "capture", "store", "bundle", "gh"): parser.add_argument("--" + name, type=Path)
    parser.add_argument("--root", action="append", default=[])
    args = parser.parse_args()
    try:
        roots = {}
        for value in args.root:
            name, separator, path = value.partition("=")
            core.require(separator and name and path and name not in roots, "roots require unique NAME=ABSOLUTE_PATH")
            roots[name] = Path(path); core.io.real_path(roots[name])
        if args.mode == "acquire":
            if args.plan is None or args.store is None or args.capture is not None or args.bundle is not None: parser.error("acquire requires plan and store")
            print(acquire(args.plan, roots, args.store, args.gh or Path(shutil.which("gh") or "/nonexistent")))
        elif args.mode == "export":
            if args.capture is None or args.store is None or any(v is not None for v in (args.plan, args.bundle, args.gh)): parser.error("export requires capture and store")
            print(export(args.capture, roots, args.store))
        else:
            if args.bundle is None or any(v is not None for v in (args.plan, args.capture, args.store, args.gh)): parser.error("verification requires bundle")
            implementation()
            result = normalization.verify_export(args.bundle, roots) if args.mode == "verify-export" else core.verify_capture(args.bundle)[1]
            print(core.canonical(result).decode()); implementation()
    except (core.io.ExportError, core.archive.EvidenceError, core.contracts.VerificationError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print("PlanetMath operation failed: " + type(exc).__name__, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__": raise SystemExit(main())
