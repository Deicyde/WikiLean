#!/usr/bin/env python3
"""Copy verified pack inputs into the exact old reducer's private directory layout.

This prepares a comparison workspace, not an execution or approved baseline.
Source acquisition, proposal folding and the old nightly script are excluded.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import stat
import sys
from pathlib import Path, PurePosixPath

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent))
import authority_contracts as contracts
import prepare_replay_v2 as preparation
import build_context
from ingest import git_snapshot

LEGACY_COMMIT = "ebac34dc1d07b66ce97692c31a914a084328f5df"
LEGACY_TREE = "753f8e002466ec27d75cd6c41360e1a930dc178b"
PROGRAM_PATHS = (
    "brain/build_snapshot.py", "brain/build_common.py", "brain/store.py",
    "brain/build_shards.py", "brain/build_cells.py", "brain/layout.py",
    "brain/build_frontier.py", "brain/frontier_suitability.py",
    "brain/build_cell_shards.py", "site/build_brain_page.py",
)
ROOT_PREFIXES = {"repo": "", "external": "inputs/external/", "mathlib": "inputs/mathlib/",
                 "decl_oracle": "inputs/decl_oracle/"}
OUTPUT_DIRECTORIES = ("brain/data", "site/assets", "site/out", "manage/data")
FIXED_MTIME_NS = 1788825600000000000  # Explicit 2026-09-08T00:00:00Z baseline staging schedule.


def require(value, message):
    if not value:
        raise ValueError(message)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


PROGRAMS = {"preparer": Path(__file__).resolve(), "contracts": Path(contracts.__file__).resolve(),
            "environment": Path(contracts.execution_environment_contract.__file__).resolve(),
            "copy": Path(preparation.__file__).resolve(), "context": Path(build_context.__file__).resolve(),
            "git": Path(git_snapshot.__file__).resolve()}
LOADED_PROGRAMS = {name: contracts.execution_environment_contract.secure_file_digest(path)
                   for name, path in PROGRAMS.items()}


def implementation():
    current = {name: contracts.execution_environment_contract.secure_file_digest(path)
               for name, path in PROGRAMS.items()}
    require(current == LOADED_PROGRAMS, "preparer implementation differs from loaded code")
    return [{"name": name, "sha256": current[name][0], "bytes": current[name][1]} for name in sorted(current)]


def mapped_path(root, path):
    require(root in ROOT_PREFIXES, "legacy layout has no declared root mapping for " + str(root))
    contracts.validate_literal_relative_path(path, "legacy input path")
    return ROOT_PREFIXES[root] + path


def loaded_pack(manifest, root, expected_pack_id):
    pack, raw = contracts.load_canonical_json(manifest)
    contracts.validate_offline_pack(pack)
    require(pack["schema"] == contracts.PACK_SCHEMA_V3 and pack["offline_pack_id"] == expected_pack_id,
            "expected exact offline-pack/v3 is required")
    contracts.verify_offline_pack_files(pack, root, manifest_path=manifest)
    inventory = contracts.parse_json_bytes(contracts.verify_file_ref(root, pack["inventory"], "legacy inventory"), location="inventory")
    contracts.validate_reducer_input_inventory(inventory)
    sources = {}
    objects = {}
    for ref in pack["source_manifests"]:
        source = contracts.parse_json_bytes(contracts.verify_file_ref(root, ref, "legacy source"), location="source manifest")
        sources[source["source_manifest_id"]] = source
        for item in source["objects"]:
            objects[(source["source_manifest_id"], item["name"])] = item
    return pack, raw, inventory, sources, objects


def input_plan(pack, inventory, objects):
    declarations = {item["id"]: item for item in inventory["inputs"]}
    require(set(declarations) == {item["input_id"] for item in pack["input_bindings"]},
            "pack and inventory input groups differ")
    inputs, absences = [], []
    for binding in pack["input_bindings"]:
        item = declarations[binding["input_id"]]
        require(item["root"] in ROOT_PREFIXES, "unknown old-layout root")
        if binding["state"] == "absent":
            key = "path" if item["cardinality"] == "one" else "path_pattern"
            # Patterns remain declarations; do not reinterpret them as a file.
            absences.append({"input_id": item["id"], "logical_root": item["root"],
                             key: ROOT_PREFIXES[item["root"]] + item[key]})
        else:
            require(binding["state"] == "present", "only complete present/absent bindings may be prepared")
            for member in binding["members"]:
                obj = objects[(member["source_manifest_id"], member["object"])]
                require("normalized" in obj["roles"], "legacy input is not a normalized source member")
                inputs.append({"path": mapped_path(item["root"], member["path"]),
                    "input_id": item["id"], "source_manifest_id": member["source_manifest_id"],
                    "object": member["object"], "sha256": obj["sha256"], "bytes": obj["bytes"],
                    "logical_root": item["root"], "member_path": member["path"], "packed_path": obj["path"]})
    paths = [item["path"] for item in inputs]
    preparation._reject_destination_collisions([*PROGRAM_PATHS, *paths], "old code/input layout")
    code_entries = set(PROGRAM_PATHS) | set(paths) | set(OUTPUT_DIRECTORIES)
    for path in tuple(code_entries):
        code_entries.update(str(parent) for parent in PurePosixPath(path).parents if str(parent) != ".")
    for absent in absences:
        if "path" in absent:
            require(absent["path"] not in code_entries, "absent input aliases a materialized code/input path")
        else:
            require(not any(contracts._matches_relative_pattern(path, absent["path_pattern"]) for path in code_entries),
                    "absent wildcard aliases a materialized code/input path")
    return sorted(inputs, key=lambda item: item["path"]), sorted(absences, key=lambda item: item["input_id"])


def legacy_programs(repository, git):
    result = {}
    for path in (*PROGRAM_PATHS, "manage/halo.py"):
        snapshot = git_snapshot.read_text_snapshot(repository, scope=path, git=git)
        require(snapshot.commit == LEGACY_COMMIT and snapshot.tree == LEGACY_TREE,
                "legacy checkout HEAD differs from exact reviewed old commit/tree")
        require(len(snapshot.files) == 1 and snapshot.files[0].path == path, "legacy program scope differs")
        result[path] = snapshot.files[0].text.encode("utf-8")
    return result


def prepare(manifest, pack_root, legacy_repo, destination, expected_pack_id, *, git="/usr/bin/git"):
    initial_implementation = implementation()
    manifest, pack_root, legacy_repo, destination = map(Path, (manifest, pack_root, legacy_repo, destination))
    for path in (manifest, pack_root, legacy_repo):
        require(path.is_absolute() and path.resolve(strict=True) == path, "inputs must be real absolute paths")
    require(destination.is_absolute() and destination.parent.resolve(strict=True) == destination.parent,
            "destination needs a real absolute parent")
    for source in (pack_root, legacy_repo):
        require(not (source == destination or source in destination.parents or destination in source.parents),
                "legacy preparation must be disjoint from source roots")
    pack, manifest_raw, inventory, _sources, objects = loaded_pack(manifest, pack_root, expected_pack_id)
    configuration_raw = contracts.verify_file_ref(pack_root, pack["configuration"], "legacy configuration")
    config = build_context.ReducerConfiguration.from_document(
        contracts.parse_json_bytes(configuration_raw, location="legacy configuration"))
    require(config.cell_attach_kinds == ("generalization", "special_case") and config.layout_enabled and
            config.layout_iterations == 200, "pack configuration differs from reviewed old stage recipe")
    inputs, absences = input_plan(pack, inventory, objects)
    programs = legacy_programs(legacy_repo, git)
    destination.mkdir(mode=0o700)  # No reuse, replacement, or ambient old checkout files.
    code = destination / "code"
    code.mkdir(mode=0o700)
    (destination / "input").mkdir(mode=0o700)
    (destination / "scratch").mkdir(mode=0o700)
    for directory in OUTPUT_DIRECTORIES:
        (code / directory).mkdir(parents=True, exist_ok=True)
        (destination / "output" / directory).mkdir(parents=True, exist_ok=True)
    for path, raw in programs.items():
        relative = "code/" + path if path in PROGRAM_PATHS else "support/" + path
        target = preparation._ensure_parent(destination, relative)
        preparation._write_exclusive(target, raw)
    for item in inputs:
        obj = objects[(item["source_manifest_id"], item["object"])]
        preparation._copy_verified_file(pack_root, preparation._CopyPlan(obj, "code/" + item["path"], "legacy input"), destination)
        # The old mixed brain/data directory receives a writable output mount.
        # Keep its exact input files in a separate readonly source for bind overlays.
        if item["path"].startswith("brain/data/"):
            preparation._copy_verified_file(pack_root, preparation._CopyPlan(obj, "input/" + item["path"], "mixed legacy input"), destination)
    for directory in (code, destination / "input", destination / "support"):
        for path in directory.rglob("*"):
            if path.is_file():
                os.utime(path, ns=(FIXED_MTIME_NS, FIXED_MTIME_NS))
    # Cold start: only exact old programs and declared inputs may exist in code/.
    actual = {path.relative_to(code).as_posix() for path in code.rglob("*") if path.is_file()}
    require(actual == set(PROGRAM_PATHS) | {item["path"] for item in inputs}, "unexpected file in old reducer layout")
    for item in inputs:
        require(contracts.digest_file(code / item["path"]) == (item["sha256"], item["bytes"]), "prepared legacy input differs")
    for path in PROGRAM_PATHS:
        require((code / path).read_bytes() == programs[path], "prepared legacy program differs")
    overlays = {"input/" + item["path"]: item for item in inputs if item["path"].startswith("brain/data/")}
    actual_overlays = {path.relative_to(destination).as_posix() for path in (destination / "input").rglob("*") if path.is_file()}
    require(actual_overlays == set(overlays), "mixed input overlay closure differs")
    for relative, item in overlays.items():
        require(contracts.digest_file(destination / relative) == (item["sha256"], item["bytes"]), "mixed input overlay bytes differ")
    support = destination / "support/manage/halo.py"
    require({path.relative_to(destination / "support").as_posix() for path in (destination / "support").rglob("*") if path.is_file()} == {"manage/halo.py"},
            "legacy support closure differs")
    require(support.read_bytes() == programs["manage/halo.py"], "retained halo program differs")
    for directory in (code, destination / "input", destination / "support"):
        for path in directory.rglob("*"):
            metadata = path.lstat()
            require(stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode),
                    "non-regular node in prepared legacy closure")
            if stat.S_ISREG(metadata.st_mode):
                require(metadata.st_mtime_ns == FIXED_MTIME_NS, "prepared legacy staging mtime differs")
    contracts.verify_offline_pack_files(pack, pack_root, manifest_path=manifest)
    require(manifest.read_bytes() == manifest_raw, "pack manifest changed during preparation")
    configuration = {"attach": ["generalization", "special_case"], "layout_enabled": True,
                     "layout_iterations": 200, "external_node_cap": config.external_node_cap,
                     "staging_mtime_epoch_seconds": FIXED_MTIME_NS // 1_000_000_000,
                     "pack_configuration_sha256": sha(configuration_raw)}
    record = {"schema": "wikilean.legacy-baseline-preparation/v1", "scope": "baseline-diagnostic",
        "authority": False, "baseline_approved": False, "executed": False,
        "pack": {"offline_pack_id": pack["offline_pack_id"], "source_set_root": pack["source_set_root"],
                 "reducer_inventory_id": pack["inventory"]["inventory_id"]},
        "legacy": {"git_commit": LEGACY_COMMIT, "git_tree": LEGACY_TREE,
            "program_files": [{"path": path, "sha256": sha(programs[path]), "bytes": len(programs[path])} for path in sorted(PROGRAM_PATHS)]},
        "inputs": inputs, "absences": absences, "configuration": configuration,
        "halo_program": {"path": "support/manage/halo.py", "sha256": sha(programs["manage/halo.py"]), "bytes": len(programs["manage/halo.py"])},
        "output_directories": list(OUTPUT_DIRECTORIES), "input_count": len(inputs)}
    record["implementation"] = implementation()
    require(record["implementation"] == initial_implementation, "preparer implementation changed")
    preparation._write_exclusive(destination / "preparation.json", contracts.canonical_json_bytes(record))
    preparation._fsync_tree(destination)
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("manifest", "pack-root", "legacy-repo", "destination"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--expected-pack-id", required=True)
    parser.add_argument("--git", default="/usr/bin/git")
    args = parser.parse_args(argv)
    try:
        record = prepare(args.manifest, args.pack_root, args.legacy_repo, args.destination, args.expected_pack_id, git=args.git)
        sys.stdout.buffer.write(contracts.canonical_json_bytes(record))
    except (ValueError, OSError, contracts.VerificationError, git_snapshot.GitSnapshotError) as exc:
        print("Legacy preparation failed: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
