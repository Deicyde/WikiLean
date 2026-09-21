#!/usr/bin/env python3
"""Verify a canonical WikiLean source manifest or complete offline pack."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from authority_contracts import (
    PACK_SCHEMA,
    PACK_SCHEMA_V2,
    PACK_SCHEMA_V3,
    REDUCER_INPUT_INVENTORY_SCHEMA_V2,
    SOURCE_SCHEMA,
    SOURCE_SCHEMA_V2,
    SOURCE_SCHEMA_V3,
    VerificationError,
    load_canonical_json,
    validate_offline_pack,
    validate_reducer_input_inventory,
    validate_source_manifest,
    verify_offline_pack_files,
    verify_source_manifest_files,
)


def verify(manifest_path: Path, root: Path | None = None) -> dict[str, object]:
    manifest_path = manifest_path.resolve(strict=True)
    verification_root = (root or manifest_path.parent).resolve(strict=True)
    document, _ = load_canonical_json(manifest_path)
    if not isinstance(document, dict):
        raise VerificationError(f"{manifest_path}: manifest must be an object")
    schema = document.get("schema")
    if schema in {SOURCE_SCHEMA, SOURCE_SCHEMA_V2}:
        manifest = validate_source_manifest(document)
        files = verify_source_manifest_files(manifest, verification_root)
        return {
            "ok": True,
            "schema": schema,
            "source_manifest_id": manifest["source_manifest_id"],
            "files": files,
        }
    if schema == SOURCE_SCHEMA_V3:
        raise VerificationError(
            "$.schema: standalone source-manifest/v3 cannot prove its evidence closure; "
            "verify the enclosing offline-pack/v3"
        )
    if schema in {PACK_SCHEMA, PACK_SCHEMA_V2, PACK_SCHEMA_V3}:
        pack = validate_offline_pack(document)
        counts = verify_offline_pack_files(pack, verification_root, manifest_path=manifest_path)
        return {
            "ok": True,
            "schema": schema,
            "offline_pack_id": pack["offline_pack_id"],
            "source_set_root": pack["source_set_root"],
            **counts,
        }
    if schema == REDUCER_INPUT_INVENTORY_SCHEMA_V2:
        inventory = validate_reducer_input_inventory(document)
        return {
            "ok": True,
            "schema": schema,
            "inventory_id": inventory["inventory_id"],
            "inputs": len(inventory["inputs"]),
            "stages": len(inventory["stages"]),
        }
    raise VerificationError(f"$.schema: unknown schema/version {schema!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument(
        "--root",
        type=Path,
        help="root for manifest paths (default: directory containing --manifest)",
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
