#!/usr/bin/env python3
"""Verify an offline pack, then run its Python reducer with a fail-fast network guard.

This is a cooperative Python boundary. Use runner/container network isolation as an
additional enforcement layer for authoritative builds.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path, PurePosixPath

from authority_contracts import (
    VerificationError,
    load_canonical_json,
    validate_offline_pack,
    verify_offline_pack_files,
)


def run(
    manifest_path: Path,
    *,
    root: Path | None = None,
    arguments: list[str] | None = None,
) -> int:
    manifest_path = manifest_path.resolve(strict=True)
    verification_root = (root or manifest_path.parent).resolve(strict=True)
    document, _ = load_canonical_json(manifest_path)
    pack = validate_offline_pack(document)
    verify_offline_pack_files(pack, verification_root, manifest_path=manifest_path)

    reducer_relative = PurePosixPath(pack["reducer"]["path"])
    reducer_path = verification_root.joinpath(*reducer_relative.parts)
    if reducer_path.suffix != ".py":
        raise VerificationError(
            "$.reducer.path: cooperative offline runner v1 requires a Python reducer"
        )

    guard_dir = str(Path(__file__).resolve().parent)
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TZ": "UTC",
        "PYTHONHASHSEED": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": guard_dir,
        "WIKILEAN_OFFLINE": "1",
        "WIKILEAN_OFFLINE_PACK": str(manifest_path),
        "WIKILEAN_OFFLINE_ROOT": str(verification_root),
        "WIKILEAN_REDUCER_CONFIG": str(
            verification_root.joinpath(*PurePosixPath(pack["configuration"]["path"]).parts)
        ),
    }
    command = [
        sys.executable,
        "-I",
        "-S",
        "-B",
        "-c",
        (
            "import runpy,sys; "
            f"sys.path[:0]=[{guard_dir!r},{str(reducer_path.parent)!r}]; "
            "import offline_guard; "
            f"sys.argv={[str(reducer_path), *(arguments or [])]!r}; "
            f"runpy.run_path({str(reducer_path)!r},run_name='__main__')"
        ),
    ]
    process = subprocess.run(command, cwd=verification_root, env=environment, check=False)
    return process.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument(
        "--root",
        type=Path,
        help="offline-pack root (default: directory containing --manifest)",
    )
    parser.add_argument("reducer_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    reducer_args = args.reducer_args
    if reducer_args[:1] == ["--"]:
        reducer_args = reducer_args[1:]
    try:
        return run(args.manifest, root=args.root, arguments=reducer_args)
    except (OSError, VerificationError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
