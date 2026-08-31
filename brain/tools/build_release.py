#!/usr/bin/env python3
"""Freeze already-built Brain outputs into a verified content-addressed release."""
from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from authority_contracts import (
    BUILD_ATTESTATION_SCHEMA,
    COMPATIBILITY_SEMANTIC_PATHS,
    DIGEST_RE,
    GIT_COMMIT_RE,
    RELEASE_PROFILE,
    RELEASE_SCHEMA,
    REQUIRED_RELEASE_PATHS,
    STATIC_CELLS_PREFIX,
    VALIDATION_ATTESTATION_SCHEMA,
    VerificationError,
    _artifact_logical_root_handle,
    _jsonl_meta_handle,
    attestation_identity,
    canonical_json_bytes,
    compatibility_semantic_state_root,
    digest_file,
    legacy_declared_input_root,
    parse_artifact_json_bytes,
    parse_json_bytes,
    release_identity,
    validate_release_manifest,
    validate_relative_path,
    verify_release_files,
)

BUILDER_NAME = "wikilean-release-builder"
BUILDER_VERSION = "1"
VALIDATOR_NAME = "wikilean-release-validator"
VALIDATOR_VERSION = "1"
DEFAULT_INPUT_INVENTORY = "brain/authority/reducer-inputs-v1.json"


@dataclass(frozen=True)
class BuildConfig:
    repo_root: Path
    output_store: Path
    semantic_epoch: str
    schedule: str
    reducer_version: str
    authority_git_commit: str
    reducer_git_commit: str
    configuration_sha256: str
    environment_sha256: str
    input_inventory: str = DEFAULT_INPUT_INVENTORY
    compatible_overlay_generation_ids: tuple[str, ...] = ()
    recorded_at: str | None = None


def _error(message: str) -> VerificationError:
    return VerificationError(f"release builder: {message}")


def _validate_digest(value: str, name: str) -> None:
    if not DIGEST_RE.fullmatch(value):
        raise _error(f"{name} must be 64 lowercase SHA-256 hex digits")


def _validate_commit(value: str, name: str) -> None:
    if not GIT_COMMIT_RE.fullmatch(value):
        raise _error(f"{name} must be a full 40-character lowercase Git commit")


def _open_source(root: Path, relative: str) -> tuple[int, os.stat_result]:
    validate_relative_path(relative, "source path")
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptors: list[int] = []
    try:
        descriptors.append(os.open(root, directory_flags))
        for part in PurePosixPath(relative).parts[:-1]:
            descriptors.append(os.open(part, directory_flags, dir_fd=descriptors[-1]))
        fd = os.open(PurePosixPath(relative).name, file_flags, dir_fd=descriptors[-1])
        source_stat = os.fstat(fd)
        if not stat.S_ISREG(source_stat.st_mode):
            os.close(fd)
            raise _error(f"source is not a regular file: {relative}")
        return fd, source_stat
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise _error(f"missing source file or directory: {relative}") from exc
    except OSError as exc:
        raise _error(f"cannot safely open source {relative}: {exc.strerror or exc}") from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _read_source(root: Path, relative: str) -> bytes:
    fd, before = _open_source(root, relative)
    try:
        with os.fdopen(fd, "rb") as source:
            data = source.read()
            after = os.fstat(source.fileno())
    finally:
        # fdopen owns fd unless construction itself failed.
        pass
    if _mutation_signature(before) != _mutation_signature(after) or len(data) != before.st_size:
        raise _error(f"source changed while reading: {relative}")
    return data


def _digest_source(root: Path, relative: str) -> tuple[str, int]:
    """Hash a pinned source descriptor without materializing its contents."""
    fd, before = _open_source(root, relative)
    digest = hashlib.sha256()
    size = 0
    with os.fdopen(fd, "rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
        after = os.fstat(source.fileno())
    if _mutation_signature(before) != _mutation_signature(after) or size != before.st_size:
        raise _error(f"source changed while hashing: {relative}")
    return digest.hexdigest(), size


def _read_jsonl_metadata(root: Path, relative: str) -> dict[str, Any]:
    """Read only the first JSONL metadata row from a pinned source."""
    fd, before = _open_source(root, relative)
    with os.fdopen(fd, "rb") as source:
        metadata = _jsonl_meta_handle(source, relative)
        after = os.fstat(source.fileno())
    if _mutation_signature(before) != _mutation_signature(after):
        raise _error(f"source changed while reading metadata: {relative}")
    return metadata


def _mutation_signature(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _copy_source_file(
    root: Path,
    relative: str,
    destination: Path,
    *,
    after_copy: Callable[[str], None] | None = None,
) -> tuple[str, int]:
    fd, before = _open_source(root, relative)
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    try:
        with os.fdopen(fd, "rb") as source, destination.open("xb") as target:
            while chunk := source.read(1024 * 1024):
                target.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            target.flush()
            os.fsync(target.fileno())
            if after_copy is not None:
                after_copy(relative)
            after = os.fstat(source.fileno())
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    if _mutation_signature(before) != _mutation_signature(after) or size != before.st_size:
        destination.unlink(missing_ok=True)
        raise _error(f"source changed while freezing: {relative}")
    return digest.hexdigest(), size


def _inventory_paths(repo_root: Path, inventory_relative: str) -> tuple[str, list[dict[str, Any]]]:
    inventory_bytes = _read_source(repo_root, inventory_relative)
    inventory_sha256 = hashlib.sha256(inventory_bytes).hexdigest()
    inventory = parse_json_bytes(inventory_bytes, location=inventory_relative)
    if not isinstance(inventory, dict) or inventory.get("schema") != "wikilean.reducer-input-inventory/v1":
        raise _error("input inventory has an unknown schema/version")
    raw_inputs = inventory.get("inputs")
    if not isinstance(raw_inputs, list):
        raise _error("input inventory inputs must be an array")

    result: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_inputs):
        if not isinstance(raw, dict):
            raise _error(f"input inventory entry {index} must be an object")
        declarations = [name for name in ("path", "path_pattern") if name in raw]
        if len(declarations) != 1 or not isinstance(raw[declarations[0]], str):
            # Named ambient-state entries are documented prohibitions, not reducer files.
            if "name" in raw and raw.get("class") == "forbidden_ambient_state":
                continue
            raise _error(f"input inventory entry {index} must declare exactly one path or path_pattern")
        declaration = declarations[0]
        declared_path = validate_relative_path(raw[declaration], f"input inventory entry {index}")
        if declaration == "path":
            paths = [declared_path] if (repo_root / declared_path).exists() or (repo_root / declared_path).is_symlink() else []
        else:
            paths = sorted(
                path.relative_to(repo_root).as_posix()
                for path in repo_root.glob(declared_path)
                if path.is_file() or path.is_symlink()
            )
        if not paths:
            result.append({"declaration": declaration, "path": declared_path, "present": False})
            continue
        for relative in paths:
            digest, size = _digest_source(repo_root, relative)
            result.append({
                "declaration": declaration,
                "path": relative,
                "present": True,
                "sha256": digest,
                "bytes": size,
            })
    return inventory_sha256, result


def _static_closure(repo_root: Path) -> set[str]:
    manifest_relative = f"{STATIC_CELLS_PREFIX}manifest.json"
    manifest_bytes = _read_source(repo_root, manifest_relative)
    manifest = parse_artifact_json_bytes(manifest_bytes, location=manifest_relative)
    if not isinstance(manifest, dict):
        raise _error("static cell manifest must be an object")
    shards = manifest.get("shards")
    traces = manifest.get("traces")
    trace_files = traces.get("files") if isinstance(traces, dict) else None
    if not isinstance(shards, dict) or not isinstance(trace_files, dict):
        raise _error("static cell manifest must declare shard and trace file maps")
    dynamic = {f"{STATIC_CELLS_PREFIX}{key}.json" for key in shards}
    dynamic.update(f"{STATIC_CELLS_PREFIX}traces/{key}.json" for key in trace_files)
    expected_static = {path for path in REQUIRED_RELEASE_PATHS if path.startswith(STATIC_CELLS_PREFIX)} | dynamic

    cells_root = repo_root / "site/assets/brain/cells"
    actual: set[str] = set()
    for directory, names, files in os.walk(cells_root, followlinks=False):
        directory_path = Path(directory)
        for name in names:
            path = directory_path / name
            if path.is_symlink():
                raise _error(f"static cell directory contains a symlink: {path.relative_to(repo_root).as_posix()}")
        for name in files:
            path = directory_path / name
            relative = path.relative_to(repo_root).as_posix()
            if path.is_symlink():
                raise _error(f"static cell directory contains a symlink: {relative}")
            if not path.is_file():
                raise _error(f"static cell directory contains a non-regular entry: {relative}")
            actual.add(relative)
    if actual != expected_static:
        missing = sorted(expected_static - actual)
        unreferenced = sorted(actual - expected_static)
        raise _error(f"static manifest closure mismatch (missing={missing[:5]}, unreferenced={unreferenced[:5]})")
    return dynamic


def _preflight_generations(repo_root: Path) -> str:
    metadata = {
        path: _read_jsonl_metadata(repo_root, path)
        for path in (
            "brain/data/nodes.jsonl",
            "brain/data/edges.jsonl",
            "brain/data/edges_links.jsonl",
            "brain/data/cells.jsonl",
            "brain/data/synapses.jsonl",
        )
    }
    base_paths = ("brain/data/nodes.jsonl", "brain/data/edges.jsonl", "brain/data/edges_links.jsonl")
    generations = {metadata[path].get("generated_at") for path in base_paths}
    snapshots = {metadata[path].get("snapshot_id") for path in base_paths}
    if len(generations) != 1 or None in generations:
        raise _error("organ graph outputs have mixed or missing generated_at values")
    if len(snapshots) != 1 or None in snapshots:
        raise _error("organ graph outputs have mixed or missing snapshot_id values")
    snapshot_id = next(iter(snapshots))
    if not isinstance(snapshot_id, str) or not DIGEST_RE.fullmatch(snapshot_id):
        raise _error("organ graph snapshot_id must be 64 lowercase SHA-256 hex digits")
    base_generation = next(iter(generations))
    for path in ("brain/data/cells.jsonl", "brain/data/synapses.jsonl"):
        if metadata[path].get("base_generated_at") != base_generation:
            raise _error(f"{path} does not name the organ graph generated_at")
        if metadata[path].get("base_snapshot_id") != snapshot_id:
            raise _error(f"{path} does not name the organ graph snapshot_id")
    return snapshot_id


def _media_and_format(relative: str) -> tuple[str, str]:
    if relative.endswith(".jsonl"):
        return "application/x-ndjson", "jsonl-rowset"
    if relative.endswith(".json"):
        return "application/json", "json"
    if relative.endswith(".html"):
        return "text/html", "opaque"
    if relative.endswith(".sqlite3"):
        return "application/vnd.sqlite3", "opaque"
    return "application/octet-stream", "opaque"


def _logical_name(relative: str) -> str:
    name = relative.replace("/", ".")
    if len(name) <= 128:
        return name
    return f"artifact.{hashlib.sha256(relative.encode('utf-8')).hexdigest()}"


def _write_canonical(path: Path, value: Any) -> tuple[str, int]:
    data = canonical_json_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    return hashlib.sha256(data).hexdigest(), len(data)


def _fsync_directory_tree(root: Path) -> None:
    """Persist every newly-created directory entry before publishing the tree."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directories = [path for path in root.rglob("*") if path.is_dir()]
    directories.append(root)
    for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
        descriptor = os.open(directory, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _tree_fingerprints(root: Path) -> dict[str, tuple[str, int]]:
    """Describe a release tree with bounded-memory file hashes."""
    result: dict[str, tuple[str, int]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise _error(f"finalized release contains a symlink: {relative}")
        if path.is_file():
            result[relative] = digest_file(path)
        elif not path.is_dir():
            raise _error(f"finalized release contains a non-regular entry: {relative}")
    return result


def _verify_finalized(root: Path, expected_release_id: str) -> None:
    manifest_path = root / "release.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest = parse_json_bytes(manifest_bytes, location=str(manifest_path))
    if manifest_bytes != canonical_json_bytes(manifest):
        raise _error(f"existing manifest is not canonical: {manifest_path}")
    validated = validate_release_manifest(manifest)
    if validated["release_id"] != expected_release_id:
        raise _error(f"existing release identity does not match directory: {root}")
    verify_release_files(validated, root)


def build_release(
    config: BuildConfig,
    *,
    after_copy: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    repo_root = config.repo_root.resolve(strict=True)
    if config.output_store.is_symlink():
        raise _error("output store must not be a symlink")
    output_store = config.output_store.resolve() if config.output_store.exists() else config.output_store.absolute()
    _validate_commit(config.authority_git_commit, "authority_git_commit")
    _validate_commit(config.reducer_git_commit, "reducer_git_commit")
    _validate_digest(config.configuration_sha256, "configuration_sha256")
    _validate_digest(config.environment_sha256, "environment_sha256")
    validate_relative_path(config.input_inventory, "input_inventory")
    overlays = tuple(sorted(set(config.compatible_overlay_generation_ids)))
    if overlays != config.compatible_overlay_generation_ids:
        raise _error("compatible overlay generation IDs must be unique and sorted")

    snapshot_id = _preflight_generations(repo_root)
    dynamic_paths = _static_closure(repo_root)
    source_paths = sorted(set(REQUIRED_RELEASE_PATHS) | dynamic_paths)
    inventory_sha256, declared_inputs = _inventory_paths(repo_root, config.input_inventory)
    source_set_root = legacy_declared_input_root(inventory_sha256, declared_inputs)

    output_store.mkdir(parents=True, exist_ok=True)
    if output_store.is_symlink() or not output_store.is_dir():
        raise _error("output store must be a real directory")
    candidate = Path(tempfile.mkdtemp(prefix=".brain-release-", dir=output_store))
    try:
        artifacts: list[dict[str, Any]] = []
        for relative in source_paths:
            destination = candidate / relative
            digest, size = _copy_source_file(
                repo_root,
                relative,
                destination,
                after_copy=after_copy,
            )
            media_type, logical_format = _media_and_format(relative)
            with destination.open("rb") as handle:
                logical_root = _artifact_logical_root_handle(
                    handle, logical_format, relative
                )
            artifacts.append({
                "logical_name": _logical_name(relative),
                "path": relative,
                "media_type": media_type,
                "sha256": digest,
                "bytes": size,
                "logical_format": logical_format,
                "logical_root": logical_root,
            })

        final_inventory_sha256, final_declared_inputs = _inventory_paths(
            repo_root, config.input_inventory
        )
        if (
            final_inventory_sha256 != inventory_sha256
            or final_declared_inputs != declared_inputs
        ):
            raise _error("declared reducer inputs changed while freezing the release")

        by_path = {artifact["path"]: artifact for artifact in artifacts}
        semantic_root = compatibility_semantic_state_root(
            config.semantic_epoch,
            snapshot_id,
            {
                path: by_path[path]["logical_root"]
                for path in COMPATIBILITY_SEMANTIC_PATHS
            },
        )
        release: dict[str, Any] = {
            "schema": RELEASE_SCHEMA,
            "profile": RELEASE_PROFILE,
            "release_id": "sha256:" + "0" * 64,
            "authority": {
                "git_commit": config.authority_git_commit,
                "semantic_state_root": semantic_root,
                "through_changeset": None,
            },
            "source_set_root": source_set_root,
            "semantic_epoch": config.semantic_epoch,
            "reducer": {
                "schedule": config.schedule,
                "version": config.reducer_version,
                "git_commit": config.reducer_git_commit,
                "configuration_sha256": config.configuration_sha256,
                "environment_sha256": config.environment_sha256,
            },
            "artifacts": artifacts,
            "attestations": [],
            "compatible_overlay_generation_ids": list(overlays),
        }
        if config.recorded_at is not None:
            release["created_at"] = config.recorded_at
        release["release_id"] = release_identity(release)

        attested_artifacts = sorted(
            (
                {key: artifact[key] for key in ("logical_name", "sha256", "bytes", "logical_root")}
                for artifact in artifacts
            ),
            key=lambda value: value["logical_name"],
        )
        build: dict[str, Any] = {
            "schema": BUILD_ATTESTATION_SCHEMA,
            "attestation_id": "sha256:" + "0" * 64,
            "release_id": release["release_id"],
            "builder": {
                "name": BUILDER_NAME,
                "version": BUILDER_VERSION,
                "git_commit": config.reducer_git_commit,
                "configuration_sha256": config.configuration_sha256,
                "environment_sha256": config.environment_sha256,
                "network": "disabled",
            },
            "input_roots": {
                "authority": semantic_root,
                "source_set": source_set_root,
                "prior_state": None,
            },
            "output_root": semantic_root,
            "artifacts": attested_artifacts,
            "metrics": {
                "artifact_count": len(artifacts),
                "artifact_bytes": sum(artifact["bytes"] for artifact in artifacts),
                "declared_input_count": len(declared_inputs),
            },
        }
        validation: dict[str, Any] = {
            "schema": VALIDATION_ATTESTATION_SCHEMA,
            "attestation_id": "sha256:" + "0" * 64,
            "release_id": release["release_id"],
            "validator": {
                "name": VALIDATOR_NAME,
                "version": VALIDATOR_VERSION,
                "git_commit": config.authority_git_commit,
                "environment_sha256": config.environment_sha256,
                "network": "disabled",
            },
            "checks": [
                {"name": "artifact-closure", "status": "pass"},
                {"name": "artifact-digests-and-logical-roots", "status": "pass"},
                {"name": "generation-and-snapshot-consistency", "status": "pass"},
                {"name": "sqlite-and-static-projection-parity", "status": "pass"},
            ],
            "result": "pass",
        }
        if config.recorded_at is not None:
            build["recorded_at"] = config.recorded_at
            validation["recorded_at"] = config.recorded_at
        build["attestation_id"] = attestation_identity(build)
        validation["attestation_id"] = attestation_identity(validation)
        build_digest, build_size = _write_canonical(candidate / "attestations/build.json", build)
        validation_digest, validation_size = _write_canonical(
            candidate / "attestations/validation.json", validation
        )
        release["attestations"] = [
            {"kind": "build", "path": "attestations/build.json", "sha256": build_digest, "bytes": build_size},
            {"kind": "validation", "path": "attestations/validation.json", "sha256": validation_digest, "bytes": validation_size},
        ]
        _write_canonical(candidate / "release.json", release)

        validated = validate_release_manifest(release)
        verify_release_files(validated, candidate)
        _fsync_directory_tree(candidate)
        release_hex = release["release_id"].removeprefix("sha256:")
        final = output_store / release_hex
        reused = False
        if final.exists() or final.is_symlink():
            if final.is_symlink() or not final.is_dir():
                raise _error(f"release destination exists and is not a real directory: {final}")
            _verify_finalized(final, release["release_id"])
            if _tree_fingerprints(final) != _tree_fingerprints(candidate):
                raise _error(f"release destination exists with different bytes: {final}")
            reused = True
        else:
            try:
                os.rename(candidate, final)
            except OSError as exc:
                if exc.errno not in {errno.EEXIST, errno.ENOTEMPTY} or not final.is_dir():
                    raise
                _verify_finalized(final, release["release_id"])
                if _tree_fingerprints(final) != _tree_fingerprints(candidate):
                    raise _error(f"concurrent release destination has different bytes: {final}") from exc
                reused = True
            if not reused:
                directory_fd = os.open(output_store, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        root = final.resolve(strict=True)
        return {
            "artifact_count": len(artifacts),
            "byte_count": sum(artifact["bytes"] for artifact in artifacts),
            "manifest": str(root / "release.json"),
            "release": release_hex,
            "release_id": release["release_id"],
            "reused": reused,
            "root": str(root),
        }
    finally:
        if candidate.exists():
            shutil.rmtree(candidate)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--output-store", type=Path, required=True)
    parser.add_argument("--semantic-epoch", required=True)
    parser.add_argument("--schedule", required=True)
    parser.add_argument("--reducer-version", required=True)
    parser.add_argument("--authority-git-commit", required=True)
    parser.add_argument("--reducer-git-commit", required=True)
    parser.add_argument("--configuration-sha256", required=True)
    parser.add_argument("--environment-sha256", required=True)
    parser.add_argument("--input-inventory", default=DEFAULT_INPUT_INVENTORY)
    parser.add_argument(
        "--compatible-overlay-generation-id",
        action="append",
        default=[],
        dest="compatible_overlay_generation_ids",
    )
    parser.add_argument("--recorded-at")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = build_release(BuildConfig(
            repo_root=args.repo_root,
            output_store=args.output_store,
            semantic_epoch=args.semantic_epoch,
            schedule=args.schedule,
            reducer_version=args.reducer_version,
            authority_git_commit=args.authority_git_commit,
            reducer_git_commit=args.reducer_git_commit,
            configuration_sha256=args.configuration_sha256,
            environment_sha256=args.environment_sha256,
            input_inventory=args.input_inventory,
            compatible_overlay_generation_ids=tuple(args.compatible_overlay_generation_ids),
            recorded_at=args.recorded_at,
        ))
    except (OSError, VerificationError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
