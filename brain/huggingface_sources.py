#!/usr/bin/env python3
"""Acquire, export, and independently verify pinned public Hugging Face evidence.

Acquisition requires CPython3.12 -I -S, fresh public GETs, and a reviewed whole
tool generation. No cached historical bytes can be relabeled as new acquisition.
All outputs remain private and require explicit source-plan review.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import platform
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from urllib.parse import urlencode

sys.path.append(str(Path(__file__).resolve().parent))
import huggingface_source_evidence as evidence

LOADED_PROGRAM_SHA256 = evidence.file_ref(Path(__file__).resolve())["sha256"]


def require_loaded_program():
    if evidence.file_ref(Path(__file__).resolve())["sha256"] != LOADED_PROGRAM_SHA256:
        raise evidence.EvidenceError("producer program changed since import")


def timestamp():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def environment():
    return {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}


def require_startup():
    if platform.python_implementation() != "CPython" or sys.version_info[:2] != (3, 12) \
            or not all((sys.flags.isolated, sys.flags.no_site, sys.flags.safe_path, sys.flags.ignore_environment)):
        raise evidence.EvidenceError("acquisition requires isolated CPython3.12 -I -S")


def runtime_identity(curl):
    require_loaded_program()
    version = subprocess.run([str(curl), "--version"], env=environment(), check=True, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, timeout=10).stdout.decode().splitlines()[0]
    tool = {"schema": "wikilean.huggingface-source-acquirer-tool/v1", "profile": evidence.current_profile(),
            "python": {"sha256": evidence.file_ref(Path(sys.executable).resolve())["sha256"],
                       "version": "CPython " + platform.python_version() + " -I -S"},
            "curl": {"sha256": evidence.file_ref(curl)["sha256"], "version": version}}
    evidence.validate_tool(tool)
    return tool


def transport(spec, curl, root):
    params = evidence.parameters(spec)
    url = spec["uri"] + ("?" + urlencode([tuple(pair) for pair in spec["query"]]) if spec["query"] else "")
    args = [str(curl), "--disable", "--fail", "--silent", "--show-error", "--location", "--max-redirs", "5",
            "--proto", "=https", "--proto-redir", "=https", "--noproxy", "*", "--connect-timeout", "30",
            "--max-time", "3600", "--retry", "0"]
    for key, value in sorted(params["headers"].items()):
        args += ["--header", key + ": " + value]
    args.append(url)
    path = root / evidence.raw_path(spec)
    evidence.stage_io.ensure_private_directory(root, path.parent)
    out = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    try:
        os.fchmod(out, 0o644)
        # Stderr can contain signed redirect URLs. It is intentionally discarded;
        # receipts state actual curl success rather than inventing HTTP status.
        with subprocess.Popen(args, env=environment(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
            timer = threading.Timer(3605, process.kill)
            timer.daemon = True
            timer.start()
            try:
                count = 0
                while chunk := process.stdout.read(1024 * 1024):
                    count += len(chunk)
                    if count > spec["limit"]:
                        process.kill()
                        raise evidence.EvidenceError(spec["object"] + ": response exceeds reviewed bound")
                    view = memoryview(chunk)
                    while view:
                        view = view[os.write(out, view):]
                if process.wait() != 0:
                    raise evidence.EvidenceError(spec["object"] + ": public HTTPS request failed")
            except BaseException:
                process.kill()
                process.wait()
                raise
            finally:
                timer.cancel()
        os.fsync(out)
    finally:
        os.close(out)


def acquire(store, curl):
    require_startup()
    curl = curl.resolve(strict=True)
    tool = runtime_identity(curl)
    pins_bytes = evidence.control_bytes(evidence.PINS)
    pins = evidence.validate_pins(evidence.parse(pins_bytes, "reviewed pins"))
    evidence.prepare_store(store)
    with evidence.stage_io.owned_directory(store, store / (".hf-" + uuid.uuid4().hex)) as owned:
        evidence.write(owned.path, "pins-preimage.json", pins_bytes)
        try:
            requests = evidence.specs(pins)
            for number, spec in enumerate(requests, 1):
                print(f"Acquiring {spec['source']}/{spec['object']} ({number}/{len(requests)})", file=sys.stderr, flush=True)
                transport(spec, curl, owned.path)
            if runtime_identity(curl) != tool or evidence.control_bytes(evidence.PINS) != pins_bytes:
                raise evidence.EvidenceError("reviewed pins or implementation changed during acquisition")
            index = evidence.indexed(owned.path)
            evidence.validate_metadata(pins, owned.path, index)
            for name, raw in evidence.controls(pins, tool, index, timestamp()).items():
                evidence.write(owned.path, name, raw)
        except BaseException as exc:
            evidence.write(owned.path, "attempt.json", evidence.canonical({"status": "incomplete-unreviewed-attempt",
                           "recorded_at": timestamp(), "failure_type": type(exc).__name__, "tool": tool}))
            retained = evidence.seal(owned, store, "wikilean.huggingface-source-incomplete-attempt/v1")
            print(f"Incomplete attempt retained: {retained}", file=sys.stderr, flush=True)
            raise
        target = evidence.seal(owned, store, evidence.CAPTURE)
    evidence.verify_capture(target)
    return target


def verify_export(root):
    evidence.origins()
    actual = evidence.verify_bundle(root, evidence.EXPORT)
    capture = root / "acquisition"
    pins, tool, index, licenses = evidence.verify_capture(capture)
    profile = evidence.read_control(root / "normalization/tool-profile.json")
    lineage = evidence.read_control(root / ("evidence/" + sorted(evidence.DATASETS.values())[0] + "-lineage.json"))
    files, copies = evidence.export_documents(capture, pins, tool, index, licenses, profile, lineage["audit"]["normalized_at"])
    expected = {"acquisition/" + name: descriptor for name, descriptor in index.items()}
    expected["acquisition/manifest.json"] = evidence.file_ref(capture / "manifest.json")
    expected.update({name: {"sha256": evidence.sha(raw), "bytes": len(raw)} for name, raw in files.items()})
    expected.update({name: descriptor for name, (_path, descriptor) in copies.items()})
    if actual != expected:
        raise evidence.EvidenceError("export independent evidence reconstruction differs")
    return {"schema": "wikilean.huggingface-source-verification/v1", "csv_files": 6,
            "csv_bytes": sum(p["size"] for r in pins["datasets"].values() for p in r["files"].values()),
            "sources": sorted(evidence.DATASETS.values()), "licenses": licenses,
            "normalization_profile": profile["profile_id"], "source_publishable": False}


def export(capture, store):
    require_loaded_program()
    if capture == store or capture in store.parents or store in capture.parents:
        raise evidence.EvidenceError("capture and export store must have disjoint ancestry")
    pins, tool, index, licenses = evidence.verify_capture(capture)
    profile = evidence.current_profile()
    files, copies = evidence.export_documents(capture, pins, tool, index, licenses, profile, timestamp())
    evidence.prepare_store(store)
    with evidence.stage_io.owned_directory(store, store / (".hf-" + uuid.uuid4().hex)) as owned:
        for name, descriptor in index.items():
            evidence.copy_file(capture / name, owned.path, "acquisition/" + name, descriptor)
        evidence.copy_file(capture / "manifest.json", owned.path, "acquisition/manifest.json", evidence.file_ref(capture / "manifest.json"))
        for name, raw in files.items():
            evidence.write(owned.path, name, raw)
        for name, (path, descriptor) in copies.items():
            if name in files:
                if {"sha256": evidence.sha(files[name]), "bytes": len(files[name])} != descriptor:
                    raise evidence.EvidenceError("CAS alias differs")
                continue
            evidence.copy_file(path, owned.path, name, descriptor)
        if evidence.current_profile() != profile:
            raise evidence.EvidenceError("normalization implementation changed")
        require_loaded_program()
        target = evidence.seal(owned, store, evidence.EXPORT)
    verify_export(target)
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("acquire", "verify-capture", "export", "verify-export"))
    parser.add_argument("--store", type=Path)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--curl", type=Path, default=Path("/usr/bin/curl"))
    args = parser.parse_args()
    try:
        if args.mode == "acquire":
            if args.store is None or args.bundle is not None:
                parser.error("acquire requires only --store")
            result = acquire(args.store, args.curl)
        elif args.mode == "export":
            if args.bundle is None or args.store is None:
                parser.error("export requires --bundle capture and --store")
            result = export(args.bundle, args.store)
        else:
            if args.bundle is None or args.store is not None:
                parser.error("verification requires only --bundle")
            if args.mode == "verify-capture":
                pins, _tool, _index, licenses = evidence.verify_capture(args.bundle)
                result = {"datasets": sorted(pins["datasets"]), "licenses": licenses}
            else:
                result = verify_export(args.bundle)
        print(evidence.canonical(result).decode() if isinstance(result, dict) else result)
    except (evidence.EvidenceError, evidence.contracts.VerificationError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"Hugging Face evidence failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
