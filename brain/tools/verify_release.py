#!/usr/bin/env python3
"""Verify a WikiLean release manifest and all local artifacts/attestations."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from authority_contracts import (
    VerificationError,
    load_canonical_json,
    validate_release_manifest,
    verify_release_files,
)


def verify(manifest_path: Path, root: Path | None = None) -> dict[str, object]:
    manifest_path = manifest_path.resolve(strict=True)
    verification_root = (root or manifest_path.parent).resolve(strict=True)
    document, _ = load_canonical_json(manifest_path)
    manifest = validate_release_manifest(document)
    counts = verify_release_files(manifest, verification_root)
    return {
        "ok": True,
        "schema": manifest["schema"],
        "profile": manifest["profile"],
        "release_id": manifest["release_id"],
        **counts,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument(
        "--root",
        type=Path,
        help="root for artifact paths (default: directory containing --manifest)",
    )
    args = parser.parse_args(argv)
    try:
        result = verify(args.manifest, args.root)
    except (OSError, VerificationError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
