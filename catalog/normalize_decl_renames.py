#!/usr/bin/env python3
"""Normalize reviewed declaration-rename source locations without re-verifying claims.

This is a syntax-only export of an existing curated input. It strips an absolute
checkout prefix only when the remaining source path exactly matches the row's
declared module, and preserves its line citations and all review evidence.
It neither supplies a new Mathlib revision nor asserts that a historical line or
oracle remains valid at the current checkout. Source authority still requires the
original revision/oracle evidence described in BRAIN-SQLITE-HANDOFF.md.

Usage:
    python3 catalog/normalize_decl_renames.py --input /path/to/decl_renames.jsonl \
        --output /path/to/normalized/decl_renames.jsonl
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path, PurePosixPath

from harvest_mathlib_tags import write_rows


def module_location(module: str) -> tuple[str, str, tuple[str, ...]]:
    """Identify Mathlib/Archive or Lean-core paths without assigning a revision."""
    if not isinstance(module, str):
        raise ValueError("rename module must be a string")
    # Preserve this existing review annotation verbatim in the emitted row.
    name = module.removesuffix(" (Lean core, not Mathlib)")
    parts = name.split(".")
    if any(not part or "/" in part or "\\" in part or ":" in part for part in parts):
        raise ValueError("rename module contains an invalid path component")
    relative = "/".join(parts) + ".lean"
    if parts[0] in {"Mathlib", "Archive"} and len(parts) > 1:
        return "mathlib", relative, (relative,)
    if parts[0] in {"Init", "Lean"} and len(parts) > 1:
        # Installed Lean sources live under src/lean; in the Lean repository
        # the same module lives under src. The root explicitly distinguishes
        # these historical references from the Mathlib repository.
        return "lean4", "src/" + relative, ("src/" + relative, "src/lean/" + relative)
    raise ValueError("rename module must belong to Mathlib, Archive, Init, or Lean")


def normalize_location(location: str, module: str) -> str:
    """Return an exact module-relative source location, rejecting ambiguous paths."""
    _root, expected, suffixes = module_location(module)
    if not isinstance(location, str) or not location:
        raise ValueError("rename source location must be a nonempty string")
    if unicodedata.normalize("NFC", location) != location or any(
        unicodedata.category(character).startswith("C") for character in location
    ) or "\\" in location:
        raise ValueError("rename source location must be a normalized POSIX path")
    citation = r":[1-9][0-9]*(?: \([A-Za-z][A-Za-z0-9 -]*\))?"
    match = re.fullmatch(r"(.+\.lean)((?:" + citation + r")(?:, " + citation + r")*)?", location)
    if match is None:
        raise ValueError("rename source location must name a Lean file and optional positive line")
    path, line = match.group(1), match.group(2) or ""
    components = path.split("/")
    if any(part in {"", ".", ".."} for part in components[1:]) or ":" in path:
        raise ValueError("rename source location contains an ambiguous path component")
    if path == expected:
        return expected + line
    if PurePosixPath(path).is_absolute() and any(path.endswith("/" + suffix) for suffix in suffixes):
        return expected + line
    raise ValueError("rename source location does not match its declared module")


def normalize_records(records: list[dict]) -> list[dict]:
    """Keep reviewed claims intact; change only file_line spellings and their root."""
    if not records or not isinstance(records[0], dict) \
            or set(records[0]) != {"_meta"} or not isinstance(records[0]["_meta"], dict):
        raise ValueError("rename input must begin with exactly one metadata envelope")
    meta = records[0]["_meta"]
    if "file_line_format" in meta and meta["file_line_format"] != "repository-relative":
        raise ValueError("rename metadata declares a different source-location format")
    output = [{"_meta": {**meta, "file_line_format": "repository-relative"}}]
    for index, record in enumerate(records[1:], start=2):
        if not isinstance(record, dict) or "_meta" in record:
            raise ValueError(f"rename row {index} must be a reviewed record")
        try:
            location = normalize_location(record.get("file_line"), record.get("module"))
            root, _path, _suffixes = module_location(record.get("module"))
            if "file_line_root" in record and record["file_line_root"] != root:
                raise ValueError("source-location root disagrees with the module")
        except ValueError as exc:
            raise ValueError(f"rename row {index}: {exc}") from exc
        output.append({**record, "file_line": location, "file_line_root": root})
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.input.resolve() == args.output.resolve():
            raise ValueError("write the normalized export to a separate path for review")
        with args.input.open(encoding="utf-8") as handle:
            records = [json.loads(line) for line in handle if line.strip()]
        normalized = normalize_records(records)
        write_rows(args.output, normalized)
    except (OSError, ValueError) as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1
    print(f"normalized {len(normalized) - 1} reviewed locations -> {args.output}")
    print("syntax-only export; historical revision and oracle evidence remain unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
