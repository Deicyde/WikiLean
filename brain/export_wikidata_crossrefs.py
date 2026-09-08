#!/usr/bin/env python3
"""Export/verify private Wikidata crossref fragments; no acquisition or publishing."""
from __future__ import annotations

import argparse
import datetime as dt
import os
import secrets
import stat
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent))
import wikidata_crossref_sources as core

LOADED_PROGRAM = core.sha(core.read(Path(__file__).resolve()))


def implementation():
    core.origins()
    core.require(core.sha(core.read(Path(__file__).resolve())) == LOADED_PROGRAM, "exporter changed since import")
    programs = {path: core.read(core.ROOT / path) for path in core.TOOL_FILES}
    core.require(all(core.sha(programs[path]) == digest for path, digest in core.LOADED.items()), "loaded helpers changed since import")
    return programs


def captured_export(path):
    files = core.capture_tree(path)
    document = core.parse(files.pop("export.json"), "export")
    core.exact(document, {"schema", "export_id", "entity_bundle_id", "curated_commit", "curated_tree",
        "normalization_profile_id", "normalized_at", "original_normalization_lineage_id", "requested_qids",
        "crossref_qids", "source_manifest_ids", "files"}, "export")
    core.require(document["schema"] == core.EXPORT_SCHEMA and core.canonical(document) == core.read(path / "export.json", private=True),
                 "invalid canonical export document")
    core.require(document["files"] == {name: {"sha256": core.sha(raw), "bytes": len(raw)} for name, raw in sorted(files.items())},
                 "export member closure differs")
    core.require(document["export_id"] == core.contracts.domain_hash(core.EXPORT_SCHEMA,
        {key: value for key, value in document.items() if key != "export_id"}), "export identity differs")
    return document, files


def verify(path, *, scratch_parent=None):
    implementation()
    document, files = captured_export(path)
    core.contracts._hash(document["entity_bundle_id"], "entity bundle ID")
    prefix = "acquisition/" + document["entity_bundle_id"].removeprefix("sha256:") + "/"
    captured = {name[len(prefix):]: raw for name, raw in files.items() if name.startswith(prefix)}
    verified = core.verify_captured_bundle(captured, scratch_parent=scratch_parent)
    profile = core.parse(files["normalization/profile.json"], "normalization profile")
    core.require(profile["profile_id"] == document["normalization_profile_id"], "normalizer profile identity differs")
    programs = {name.removeprefix("implementation/"): raw for name, raw in files.items() if name.startswith("implementation/")}
    acquired_programs = {name.removeprefix("acquisition-implementation/"): raw for name, raw in files.items() if name.startswith("acquisition-implementation/")}
    proof = {name.removeprefix("curated/proof/"): raw for name, raw in files.items() if name.startswith("curated/proof/")}
    expected = core.build_export(captured, verified, files["curated/source_registry.json"], document["curated_commit"],
        document["curated_tree"], proof, core.parse(files["curated/git-tool.json"], "Git tool"), profile, programs,
        acquired_programs, document["normalized_at"])
    core.require(expected == {**files, "export.json": core.canonical(document)}, "export differs from independent replay")
    for name in ("entities", "registry", "crossrefs"):
        manifest = core.parse(files["source-manifests/" + name + ".json"], "source manifest")
        core.contracts.verify_source_manifest_files(manifest, path)
    implementation()
    return document


def export(bundle, repository, commit, store, *, normalized_at=None):
    core.real_path(store)
    core.require(store.is_dir() and store.lstat().st_uid == os.getuid() and stat.S_IMODE(store.lstat().st_mode) == 0o700,
                 "store must be an existing private owned directory")
    for path in (bundle, repository):
        core.real_path(path)
        core.require(store != path and store not in path.parents and path not in store.parents, "output store overlaps an input")
    profile, programs = core.current_profile(), implementation()
    captured = core.entities._bundle_bytes(bundle)
    verified = core.verify_captured_bundle(captured, scratch_parent=store)
    registry, tree, proof, git_tool = core.capture_git(repository, commit)
    acquired_programs = {path: core.read(core.ROOT / path) for path in core.acquisition_programs(captured)}
    when = normalized_at or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    files = core.build_export(captured, verified, registry, commit, tree, proof, git_tool, profile, programs, acquired_programs, when)
    core.require(core.current_profile() == profile, "normalizer profile changed during export")
    implementation()
    document = core.parse(files["export.json"], "export")
    target = store / document["export_id"].removeprefix("sha256:")
    with core.stage_io.owned_directory(store, store / (".crossref-export-" + secrets.token_hex(12))) as owned:
        for name, raw in files.items():
            core.write(owned.path, name, raw)
        core.require(core.capture_tree(owned.path) == files, "staged export differs")
        # All file writes fsync; seal each directory before the atomic rename.
        for current, _dirs, _files in os.walk(owned.path, topdown=False):
            core.stage_io.fsync_directory(Path(current))
        try:
            core.stage_io.publish_directory_no_replace(owned, target)
        except FileExistsError:
            core.require(core.capture_tree(target) == files, "existing immutable export differs")
            return target
        try:
            core.require(core.capture_tree(target) == files, "published export differs")
            implementation()
        except BaseException:
            core.stage_io.remove_owned_directory(core.stage_io.OwnedDirectory(target, owned.device, owned.inode))
            raise
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("export")
    build.add_argument("bundle", type=Path)
    build.add_argument("--repository", required=True, type=Path)
    build.add_argument("--curated-commit", required=True)
    build.add_argument("--store", required=True, type=Path)
    check = commands.add_parser("verify")
    check.add_argument("bundle", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "export":
            print(export(args.bundle.absolute(), args.repository.absolute(), args.curated_commit, args.store.absolute()))
        else:
            result = verify(args.bundle.absolute())
            print(core.canonical({key: value for key, value in result.items() if key != "files"}).decode())
    except (core.ExportError, core.entities.WikidataEntityBundleError, core.contracts.VerificationError,
            core.git_snapshot.GitSnapshotError, OSError, KeyError, ValueError, TypeError) as exc:
        print("Wikidata crossref export failed: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
