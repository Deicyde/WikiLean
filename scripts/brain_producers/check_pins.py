"""Do Brain producer exports that share an input agree on its pin?

    python3 scripts/brain_producers/check_pins.py EXPORT_DIR...

Reads each export's manifest.json and compares, across every export whose manifest has the
input, the pins below: the trough commit (producers 1-3), the tauceti.bmp sha256 and docs
commit (producers 1 and 2), the mathlib.bmp sha256 and docs commit (producers 1 and 3). A
null pin disagrees with a set one. Prints one line per pin; exits 1 naming every mismatch,
2 when a manifest cannot be read.
"""

from __future__ import annotations

import json
import os
import sys

# Producer 4 (rh-decls) shares no input with the others; it is listed so a run over every
# export does not stop at its manifest.
SCHEMAS = {"wikilean.brain.plans/v1", "wikilean.brain.tauceti-decls/v1", "wikilean.brain.mathlib-decls/v1",
           "wikilean.brain.rh-decls/v1"}
PINS = [("trough", "commit"), ("tauceti_decls", "sha256"), ("tauceti_decls", "docs_commit"),
        ("mathlib_decls", "sha256"), ("mathlib_decls", "docs_commit")]


class ManifestError(Exception):
    pass


def read_manifest(export: str) -> dict:
    path = os.path.join(export, "manifest.json")
    try:
        with open(path, encoding="utf-8") as f:
            m = json.load(f)
    except (OSError, ValueError) as e:
        raise ManifestError(f"{export}: cannot read manifest.json ({e})") from None
    if not isinstance(m, dict) or m.get("schema") not in SCHEMAS or not isinstance(m.get("inputs"), dict):
        raise ManifestError(f"{export}: not a Brain producer manifest (schema {m.get('schema')!r})"
                            if isinstance(m, dict) else f"{export}: manifest is not an object")
    return m


def check(manifests: dict[str, dict]) -> tuple[list[str], list[str]]:
    """(lines, mismatches) for {export dir: manifest}."""
    lines, bad = [], []
    for name, field in PINS:
        seen = {exp: (m["inputs"][name] if isinstance(m["inputs"][name], dict) else {}).get(field) for exp, m in manifests.items()
                if name in m["inputs"]}
        if len(seen) < 2:
            continue
        label = f"{name}.{field}"
        if len(set(seen.values())) == 1:
            lines.append(f"ok        {label} = {next(iter(seen.values()))} ({len(seen)} exports)")
        else:
            msg = f"{label}: " + ", ".join(f"{exp}={val}" for exp, val in seen.items())
            lines.append(f"MISMATCH  {msg}")
            bad.append(msg)
    return lines, bad


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    try:
        manifests = {exp: read_manifest(exp) for exp in argv}
    except ManifestError as e:
        print(f"check_pins: {e}", file=sys.stderr)
        return 2
    for exp, m in manifests.items():
        print(f"export    {exp}: {m['schema']} logical_root {m.get('logical_root')}")
    lines, bad = check(manifests)
    for line in lines:
        print(line)
    if bad:
        print(f"check_pins: {len(bad)} mismatch(es)", file=sys.stderr)
        return 1
    print("check_pins: all shared pins agree")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
