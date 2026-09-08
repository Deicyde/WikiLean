#!/usr/bin/env python3
"""Normalize reviewed Git exports; use CPython3.12 -I -S and an explicit PyYAML root.

The CLI loads only the selected package, never a caller-selected site-packages
directory. This is source-tool evidence, not a trusted OCI replay receipt.
"""
from __future__ import annotations

import argparse
import datetime as dt
import platform
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import git_harvest_dependencies as dependencies


def initialize(package_root=None):
    if package_root is not None:
        dependencies.load_yaml(package_root)
    import git_harvest_sources as core
    return core


def export(plan, roots, store, *, normalized_at=None):
    core = initialize()
    core.require(store.is_absolute() and store.resolve(strict=False) == store, "harvest store must have real absolute ancestry")
    for root in roots.values():
        core.require(store != root and store not in root.parents and root not in store.parents, "harvest store overlaps a parent bundle")
    before = core.current_profile()
    captured = core.capture_parents(plan, roots)
    when = normalized_at or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    files = core.build_documents(plan, captured, *before, when)
    core.require(core.current_profile() == before, "normalizer implementation or dependency changed during reduction")
    target = core.parent.archive.publish(files, store, core.EXPORT_SCHEMA)
    core.verify_export(target, roots)
    core.require(core.current_profile() == before, "normalizer changed during publication")
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("export", "verify"))
    parser.add_argument("--pyyaml-root", required=True, type=Path)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--store", type=Path)
    parser.add_argument("--root", action="append", default=[])
    args = parser.parse_args()
    try:
        dependencies.require(platform.python_implementation() == "CPython" and sys.version_info[:2] == (3, 12)
            and sys.flags.isolated and sys.flags.no_site, "harvest CLI requires CPython3.12 with -I -S")
        core = initialize(args.pyyaml_root)
        roots = {}
        for value in args.root:
            name, separator, path = value.partition("=")
            core.require(separator and name and path and name not in roots, "parents require unique NAME=ABSOLUTE_PATH bindings")
            roots[name] = Path(path)
            core.require(roots[name].is_absolute() and roots[name].resolve(strict=True) == roots[name], "parent root ancestry is unsafe")
        before = core.current_implementation()
        if args.command == "export":
            core.require(args.plan is not None and args.store is not None and args.bundle is None, "export requires plan and store")
            raw = core.parent.read_regular(args.plan)
            plan = core.validate_plan(core.parse(raw, "harvester plan"))
            core.require(raw == core.canonical(plan), "harvester plan must be canonical")
            print(export(plan, roots, args.store))
        else:
            core.require(args.bundle is not None and args.plan is None and args.store is None, "verify requires only bundle and parents")
            print(core.canonical(core.verify_export(args.bundle, roots)).decode())
        core.require(core.current_implementation() == before, "normalizer changed during command")
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        print("Git harvest failed: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
