#!/usr/bin/env python3
"""Build or independently verify private derived catalog source fragments.

Every parent source and its evidence must be supplied through the explicit plan
and physical-root map. No tracked corpus, current checkout data, caches, or
network source supplies a reduction input.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import os
import secrets
import stat
import sys
import tempfile
from pathlib import Path

if __name__ == "__main__" and (not sys.flags.isolated or not sys.flags.no_site):
    os.execve(sys.executable, [sys.executable, "-I", "-S", str(Path(__file__).resolve()), *sys.argv[1:]],
              {"PATH": "/usr/bin:/bin", "LC_ALL": "C", "LANG": "C"})
sys.path.insert(0, str(Path(__file__).resolve().parent))
import derived_catalog_sources as core

LOADED_IMPLEMENTATION = {path: (core.ROOT / path).read_bytes() for path in core.TOOL_FILES}


def implementation():
    actual = {path: core.read(core.ROOT / path) for path in core.TOOL_FILES}
    core.require(actual == LOADED_IMPLEMENTATION, "normalizer implementation changed after loading")
    return dict(actual)


def captured_bundle(bundle):
    core.real_path(bundle)
    core.require(stat.S_ISDIR(bundle.lstat().st_mode) and stat.S_IMODE(bundle.lstat().st_mode) == 0o700,
                 "derived export root must remain private")
    raw = core.read(bundle / "export.json")
    export = core.exact(core.contracts.parse_json_bytes(raw, location="export manifest"),
        {"schema", "export_id", "normalization_profile_id", "normalized_at", "curated_tree", "derived_source_manifest_ids", "files"}, "export")
    core.require(raw == core.canonical(export) and export["schema"] == core.EXPORT_SCHEMA, "invalid export control document")
    core.require(export["export_id"] == core.contracts.domain_hash("wikilean.derived-catalog-export.v1",
                 {key: value for key, value in export.items() if key != "export_id"}), "export identity differs")
    files = {}
    for ref in export["files"]:
        core.exact(ref, {"path", "sha256", "bytes", "media_type"}, "export file")
        core.contracts.validate_literal_relative_path(ref["path"], "export file path")
        core.require(ref["path"] not in files and ref["path"] != "export.json", "duplicate export file")
        files[ref["path"]] = core.capture_file(bundle / ref["path"], ref, limit=512 * 1024 * 1024)
    actual = set()
    for directory, dirs, filenames in os.walk(bundle, followlinks=False):
        for name in dirs:
            path = Path(directory) / name
            core.require(not path.is_symlink() and stat.S_IMODE(path.lstat().st_mode) == 0o700, "export directories must be real and private")
        actual.update(str((Path(directory) / name).relative_to(bundle)) for name in filenames)
    core.require(actual == set(files) | {"export.json"}, "export file closure differs")
    return export, files


def verify(bundle, roots, *, scratch_parent=None):
    """Rebuild bytes from retained parent captures and cryptographic Git proof."""
    implementation()
    export, files = captured_bundle(bundle)
    parse = lambda path: core.contracts.parse_json_bytes(files[path], location=path)
    plan, profile = parse("plan.json"), parse("normalization/tool-profile.json")
    core.validate_plan(plan)
    core.require(profile in core.profiles()["profiles"] and profile["profile_id"] == export["normalization_profile_id"],
                 "export profile is not a reviewed complete generation")
    programs = {path: files["implementation/" + path] for path in core.TOOL_FILES}
    core.require(profile["files"] == [{"path": path, "sha256": core.sha(programs[path])} for path in core.TOOL_FILES],
                 "export implementation preimage differs")
    # A historical profile remains verifiable only while its normalization
    # semantics are explicitly handled by this consumer. Version one has a
    # single algorithm; helper generations do not dispatch arbitrary code.
    curated = {name: files["curated/" + name] for name in core.CURATED_PATHS}
    proof = {path.removeprefix("curated/proof/"): raw for path, raw in files.items() if path.startswith("curated/proof/")}
    core.verify_curated_proof(curated, plan["curated_git_commit"], export["curated_tree"], proof)
    with tempfile.TemporaryDirectory(prefix="wikilean-derived-verify-", dir=scratch_parent) as temporary:
        workspace = Path(temporary).resolve(strict=True)
        sources, manifests, objects, captured, _, lineages = core.capture_parents(plan, roots, workspace)
        output = core.reduce_inputs(plan, curated, manifests, objects, captured, lineages)
        expected = core.build_documents(plan, curated, export["curated_tree"], parse("curated/git-tool.json"), proof,
            sources, manifests, objects, output, profile, programs, export["normalized_at"])
    core.require(expected == {**files, "export.json": core.canonical(export)}, "export differs from independent normalization")
    implementation()
    return export


def export(plan, roots, repository, store, *, normalized_at=None):
    plan, roots = copy.deepcopy(plan), dict(roots)
    profile, programs = core.current_profile(), implementation()
    core.real_path(store)
    core.require(store.is_dir() and stat.S_IMODE(store.lstat().st_mode) == 0o700, "export store must be an existing private directory")
    for root in [repository, *roots.values()]:
        core.real_path(root)
        core.require(root != store and root not in store.parents and store not in root.parents,
                     "export store must be disjoint from input roots and repository")
    when = normalized_at or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with tempfile.TemporaryDirectory(prefix=".derived-inputs-", dir=store) as temporary:
        sources, manifests, objects, captured, _, lineages = core.capture_parents(plan, roots, Path(temporary))
        curated, tree, git_tool, proof = core.capture_curated(repository, plan["curated_git_commit"])
        output = core.reduce_inputs(plan, curated, manifests, objects, captured, lineages)
        files = core.build_documents(plan, curated, tree, git_tool, proof, sources, manifests, objects, output, profile, programs, when)
    core.require(core.current_profile() == profile, "normalizer profile changed during reduction")
    implementation()
    document = core.contracts.parse_json_bytes(files["export.json"], location="export")
    target = store / document["export_id"].removeprefix("sha256:")
    with core.stage_io.owned_directory(store, store / (".derived-export-" + secrets.token_hex(12))) as owned:
        for relative, raw in files.items():
            path = owned.path / relative
            core.stage_io.ensure_private_directory(owned.path, path.parent)
            core.stage_io.write_bytes_exclusive(path, raw, mode=0o644)
        # Compare the complete written generation before publication. The
        # reduction above used only descriptor-captured inputs; subsequent
        # verification repeats the derivation independently.
        written, captured_files = captured_bundle(owned.path)
        core.require({**captured_files, "export.json": core.canonical(written)} == files, "staged export bytes differ")
        try:
            try:
                core.stage_io.publish_directory_no_replace(owned, target)
            except FileExistsError:
                existing, existing_files = captured_bundle(target)
                core.require({**existing_files, "export.json": core.canonical(existing)} == files, "existing export differs")
                return target
            # The shared helper verifies the inode and complete tree after
            # rename. Check content as well before reporting success.
            published, published_files = captured_bundle(target)
            core.require({**published_files, "export.json": core.canonical(published)} == files, "published export bytes differ")
            implementation()
        except BaseException as original:
            # A caught error can occur after the kernel renamed the directory
            # but before the shared helper recorded success. Check both names
            # independently and never delete a different inode at either one.
            for candidate in (owned.path, target):
                try:
                    metadata = candidate.lstat()
                    if stat.S_ISDIR(metadata.st_mode) and (metadata.st_dev, metadata.st_ino) == (owned.device, owned.inode):
                        core.stage_io.remove_owned_directory(core.stage_io.OwnedDirectory(candidate, owned.device, owned.inode))
                except FileNotFoundError:
                    pass
                except Exception as cleanup_error:
                    original.add_note(f"derived export cleanup failed at {candidate.name}: {cleanup_error}")
            raise
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("export", "verify"))
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--roots", type=Path, required=True)
    parser.add_argument("--repository", type=Path)
    parser.add_argument("--store", type=Path)
    parser.add_argument("--bundle", type=Path)
    args = parser.parse_args()
    try:
        roots = core.contracts.parse_json_bytes(core.read(args.roots.absolute()), location="physical roots")
        core.require(isinstance(roots, dict) and all(isinstance(value, str) for value in roots.values()), "physical roots must map names to absolute paths")
        roots = {name: Path(path) for name, path in roots.items()}
        if args.mode == "export":
            if args.plan is None or args.repository is None or args.store is None or args.bundle is not None:
                parser.error("export requires --plan, --repository, --store")
            plan = core.contracts.parse_json_bytes(core.read(args.plan.absolute()), location="derived plan")
            print(export(plan, roots, args.repository, args.store))
        else:
            if args.bundle is None or any(value is not None for value in (args.plan, args.repository, args.store)):
                parser.error("verify requires --bundle")
            print(core.canonical(verify(args.bundle, roots)).decode())
    except (core.DerivationError, core.contracts.VerificationError, OSError, ValueError, KeyError) as exc:
        print(f"Derived catalog export failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
