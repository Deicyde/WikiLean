#!/usr/bin/env python3
"""Export or independently replay private Mathlib tag and MathWorld ID sources."""
from __future__ import annotations

import argparse
import datetime as dt
import os
import secrets
import stat
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent))
import derived_identifier_sources as core

io = core.io
LOADED_PROGRAM = io.sha(io.read(Path(__file__).resolve()))


def implementation():
    core.origins()
    io.require(io.sha(io.read(Path(__file__).resolve())) == LOADED_PROGRAM, "identifier exporter changed since import")
    programs = {path: io.read(core.ROOT / path) for path in core.TOOL_FILES}
    io.require(all(io.sha(programs[path]) == digest for path, digest in core.LOADED.items()), "identifier loaded helper changed")
    return programs


def captured_export(path):
    files = io.capture_tree(path)
    raw = files.pop("export.json")
    document = io.exact(io.parse(raw, "export"), {"schema", "normalization_profile_id", "normalized_at", "source_manifest_ids", "files", "export_id"}, "export")
    io.require(document["schema"] == core.EXPORT_SCHEMA and raw == io.canonical(document), "invalid canonical identifier export")
    io.require(document["files"] == {name: {"sha256": io.sha(data), "bytes": len(data)} for name, data in sorted(files.items())}, "identifier export member closure differs")
    io.require(document["export_id"] == core.contracts.domain_hash(core.EXPORT_SCHEMA,
        {key: value for key, value in document.items() if key != "export_id"}), "identifier export ID differs")
    return document, files


def verify(path, roots):
    implementation()
    document, files = captured_export(path)
    plan = core.validate_plan(io.parse(files["plan.json"], "identifier plan"))
    source_data = core.capture_parents(plan, roots)
    profile = io.parse(files["normalization/profile.json"], "normalizer profile")
    io.require(profile["profile_id"] == document["normalization_profile_id"], "identifier profile differs")
    programs = {name.removeprefix("implementation/"): raw for name, raw in files.items() if name.startswith("implementation/")}
    expected = core.build_documents(plan, *source_data, profile, programs, document["normalized_at"])
    io.require(expected == {**files, "export.json": io.canonical(document)}, "identifier export differs from independent reduction")
    for name in ("mathlib-tag-xrefs", "mathworld-identifiers"):
        manifest = io.parse(files["source-manifests/" + name + ".json"], "identifier manifest")
        core.contracts.verify_source_manifest_files(manifest, path)
    implementation()
    return document


def export(plan, roots, store, *, normalized_at=None):
    io.real_path(store)
    io.require(store.is_dir() and stat.S_IMODE(store.lstat().st_mode) == 0o700 and store.lstat().st_uid == os.getuid(), "identifier store must be private and owned")
    for root in roots.values():
        io.real_path(root)
        io.require(store != root and store not in root.parents and root not in store.parents, "identifier store overlaps source root")
    profile, programs = core.current_profile(), implementation()
    source_data = core.capture_parents(plan, roots)
    when = normalized_at or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    files = core.build_documents(plan, *source_data, profile, programs, when)
    io.require(core.current_profile() == profile, "identifier producer generation changed")
    implementation()
    document = io.parse(files["export.json"], "identifier export")
    target = store / document["export_id"].removeprefix("sha256:")
    with io.stage_io.owned_directory(store, store / (".identifier-export-" + secrets.token_hex(12))) as owned:
        for name, raw in files.items():
            io.write(owned.path, name, raw)
        io.require(io.capture_tree(owned.path) == files, "staged identifier bytes differ")
        for current, _dirs, _files in os.walk(owned.path, topdown=False):
            io.stage_io.fsync_directory(Path(current))
        try:
            io.stage_io.publish_directory_no_replace(owned, target)
        except FileExistsError:
            io.require(io.capture_tree(target) == files, "existing identifier export differs")
            return target
        try:
            io.require(io.capture_tree(target) == files, "published identifier export differs")
            implementation()
        except BaseException:
            io.stage_io.remove_owned_directory(io.stage_io.OwnedDirectory(target, owned.device, owned.inode))
            raise
    return target


def root_bindings(values):
    roots = {}
    for value in values:
        name, separator, path = value.partition("=")
        io.require(separator and name and path and name not in roots, "roots require unique NAME=ABSOLUTE_PATH bindings")
        roots[name] = Path(path)
        io.real_path(roots[name])
    return roots


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("export")
    create.add_argument("plan", type=Path)
    create.add_argument("--store", required=True, type=Path)
    create.add_argument("--root", action="append", default=[])
    check = commands.add_parser("verify")
    check.add_argument("bundle", type=Path)
    check.add_argument("--root", action="append", default=[])
    args = parser.parse_args()
    try:
        roots = root_bindings(args.root)
        if args.command == "export":
            raw = io.read(args.plan.absolute())
            plan = core.validate_plan(io.parse(raw, "identifier plan"))
            io.require(raw == io.canonical(plan), "identifier plan must be canonical")
            print(export(plan, roots, args.store.absolute()))
        else:
            result = verify(args.bundle.absolute(), roots)
            print(io.canonical({key: value for key, value in result.items() if key != "files"}).decode())
    except (io.ExportError, core.contracts.VerificationError, io.git_snapshot.GitSnapshotError,
            OSError, KeyError, ValueError, TypeError) as exc:
        print("Identifier source export failed: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
