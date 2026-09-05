#!/usr/bin/env python3
"""Verify and materialize a sealed offline-pack/v2 or v3 replay workspace.

This tool is intentionally narrower than a runner: it verifies authority
documents, copies the exact reducer/input closure into a fresh workspace, and
emits a canonical :mod:`build_context` document.  It never executes reducer
code and never reads Brain configuration from the process environment.
"""
from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import os
import secrets
import shutil
import stat
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


HERE = Path(__file__).resolve().parent
BRAIN = HERE.parent
if str(BRAIN) not in sys.path:
    sys.path.insert(0, str(BRAIN))

import authority_contracts as contracts  # noqa: E402
import build_context  # noqa: E402


class ReplayPreparationError(ValueError):
    """The verified pack cannot be safely materialized as a replay."""


@dataclass(frozen=True, slots=True)
class PreparedReplay:
    workspace: Path
    context_path: Path
    environment_path: Path
    environment_id: str
    environment_sha256: str
    reducer_git_commit: str
    configuration_sha256: str
    generation_id: str
    offline_pack_id: str
    source_set_root: str
    reducer_inventory_id: str
    reducer_files: tuple[tuple[str, int, str], ...]

    def to_document(self) -> dict[str, Any]:
        return {
            "context": str(self.context_path),
            "environment_id": self.environment_id,
            "environment_path": str(self.environment_path),
            "environment_sha256": self.environment_sha256,
            "configuration_sha256": self.configuration_sha256,
            "generation_id": self.generation_id,
            "offline_pack_id": self.offline_pack_id,
            "ok": True,
            "reducer_git_commit": self.reducer_git_commit,
            "reducer_inventory_id": self.reducer_inventory_id,
            "source_set_root": self.source_set_root,
            "workspace": str(self.workspace),
        }


@dataclass(frozen=True, slots=True)
class _CopyPlan:
    ref: dict[str, Any]
    relative_destination: str
    location: str


@dataclass(frozen=True, slots=True)
class _OwnedDirectory:
    path: Path
    device: int
    inode: int


def _absolute_existing_file(path: str | os.PathLike[str], location: str) -> Path:
    requested = Path(path)
    if requested.is_symlink():
        raise ReplayPreparationError(f"{location}: symlinks are forbidden")
    try:
        resolved = requested.resolve(strict=True)
    except OSError as exc:
        raise ReplayPreparationError(f"{location}: cannot resolve file: {exc}") from exc
    if not resolved.is_file():
        raise ReplayPreparationError(f"{location}: expected a regular file")
    return resolved


def _absolute_existing_directory(path: str | os.PathLike[str], location: str) -> Path:
    try:
        resolved = Path(path).resolve(strict=True)
    except OSError as exc:
        raise ReplayPreparationError(
            f"{location}: cannot resolve directory: {exc}"
        ) from exc
    if not resolved.is_dir():
        raise ReplayPreparationError(f"{location}: expected a directory")
    return resolved


def _fresh_workspace_path(path: str | os.PathLike[str]) -> Path:
    requested = Path(path)
    if not requested.is_absolute():
        requested = Path.cwd() / requested
    if requested.is_symlink():
        raise ReplayPreparationError(f"workspace already exists: {requested}")
    candidate = requested.resolve(strict=False)
    try:
        parent = candidate.parent.resolve(strict=True)
    except OSError as exc:
        raise ReplayPreparationError(
            f"workspace parent must already exist: {exc}"
        ) from exc
    if not parent.is_dir():
        raise ReplayPreparationError("workspace parent is not a directory")
    candidate = parent / candidate.name
    if candidate.exists() or candidate.is_symlink():
        raise ReplayPreparationError(f"workspace already exists: {candidate}")
    return candidate


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _load_verified_json(
    root: Path,
    ref: dict[str, Any],
    location: str,
) -> tuple[dict[str, Any], bytes]:
    with contracts.open_verified_file(root, ref, location) as handle:
        raw = handle.read()
    value = contracts.parse_json_bytes(raw, location=ref["path"])
    if raw != contracts.canonical_json_bytes(value):
        raise ReplayPreparationError(
            f"{location}: document is not canonical-json-v1 bytes"
        )
    if not isinstance(value, dict):
        raise ReplayPreparationError(f"{location}: expected a JSON object")
    return value, raw


def _reject_destination_collisions(paths: Iterable[str], location: str) -> None:
    """Reject portable aliases and ancestry collisions in linear total size."""
    terminal = object()
    trie: dict[object, Any] = {}
    for logical in sorted(paths):
        node = trie
        parts = PurePosixPath(logical).parts
        for part in parts:
            if terminal in node:
                raise ReplayPreparationError(
                    f"{location}: destination ancestry collision at {logical!r}"
                )
            portable_part = unicodedata.normalize("NFC", part).casefold()
            existing = node.get(portable_part)
            if existing is None:
                child: dict[object, Any] = {}
                node[portable_part] = (part, child)
            else:
                existing_part, child = existing
                if existing_part != part:
                    raise ReplayPreparationError(
                        f"{location}: destination component aliases an existing "
                        f"portable name at {logical!r}"
                    )
            node = child
        if terminal in node or node:
            raise ReplayPreparationError(
                f"{location}: portable destination collision at {logical!r}"
            )
        node[terminal] = True


def _ensure_parent(root: Path, relative: str) -> Path:
    parts = PurePosixPath(relative).parts
    current = root
    for part in parts[:-1]:
        current = current / part
        try:
            current.mkdir(mode=0o700)
            current.chmod(0o700)
        except FileExistsError:
            try:
                mode = current.lstat().st_mode
            except OSError as exc:
                raise ReplayPreparationError(
                    f"cannot inspect destination directory {current}: {exc}"
                ) from exc
            if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                raise ReplayPreparationError(
                    f"destination parent is not a real directory: {current}"
                )
    return root.joinpath(*parts)


def _copy_verified_file(root: Path, plan: _CopyPlan, workspace: Path) -> None:
    destination_path = _ensure_parent(workspace, plan.relative_destination)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    digest = hashlib.sha256()
    byte_count = 0
    with contracts.open_regular_file(
        root, plan.ref["path"], plan.location
    ) as source:
        try:
            descriptor = os.open(destination_path, flags, 0o600)
        except OSError as exc:
            raise ReplayPreparationError(
                f"cannot create destination {destination_path}: {exc}"
            ) from exc
        try:
            with os.fdopen(descriptor, "wb") as destination:
                os.fchmod(destination.fileno(), 0o600)
                while chunk := source.read(1024 * 1024):
                    destination.write(chunk)
                    digest.update(chunk)
                    byte_count += len(chunk)
                destination.flush()
                os.fchmod(destination.fileno(), 0o444)
                os.fsync(destination.fileno())
        except Exception:
            try:
                destination_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
    if byte_count != plan.ref["bytes"] or digest.hexdigest() != plan.ref["sha256"]:
        raise ReplayPreparationError(
            f"{plan.location}: source changed while it was being copied"
        )


def _write_exclusive(path: Path, data: bytes, *, final_mode: int = 0o444) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise ReplayPreparationError(f"cannot create {path}: {exc}") from exc
    with os.fdopen(descriptor, "wb") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(data)
        handle.flush()
        os.fchmod(handle.fileno(), final_mode)
        os.fsync(handle.fileno())


def _make_read_only_tree(root: Path) -> None:
    for directory, names, filenames in os.walk(
        root, topdown=False, followlinks=False
    ):
        directory_path = Path(directory)
        for name in filenames:
            path = directory_path / name
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                raise ReplayPreparationError(
                    f"materialized tree contains a non-regular file: {path}"
                )
            if stat.S_IMODE(mode) != 0o444:
                raise ReplayPreparationError(
                    f"materialized file has an unexpected mode: {path}"
                )
        for name in names:
            path = directory_path / name
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                raise ReplayPreparationError(
                    f"materialized tree contains a non-directory: {path}"
                )
            path.chmod(0o555)
    root.chmod(0o555)


def _owned_directory(path: Path) -> _OwnedDirectory:
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise ReplayPreparationError(f"expected an owned directory: {path}")
    return _OwnedDirectory(path, metadata.st_dev, metadata.st_ino)


def _still_owned(directory: _OwnedDirectory) -> bool:
    try:
        metadata = directory.path.lstat()
    except FileNotFoundError:
        return False
    return (
        stat.S_ISDIR(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and metadata.st_dev == directory.device
        and metadata.st_ino == directory.inode
    )


def _remove_created_workspace(ownership: _OwnedDirectory) -> None:
    workspace = ownership.path
    if not workspace.exists() and not workspace.is_symlink():
        return
    if not _still_owned(ownership):
        raise ReplayPreparationError(
            f"refusing to clean a replaced staging directory: {workspace}"
        )
    try:
        workspace.chmod(0o700)
    except OSError:
        pass
    for directory_name, names, filenames in os.walk(
        workspace, topdown=True, followlinks=False
    ):
        directory_path = Path(directory_name)
        try:
            directory_path.chmod(0o700)
        except OSError:
            pass
        for name in names:
            path = directory_path / name
            if not path.is_symlink():
                try:
                    path.chmod(0o700)
                except OSError:
                    pass
        for name in filenames:
            path = directory_path / name
            if not path.is_symlink():
                try:
                    path.chmod(0o600)
                except OSError:
                    pass
    shutil.rmtree(workspace)


def _create_staging_directory(target: Path) -> _OwnedDirectory:
    for _attempt in range(128):
        candidate = target.parent / (
            f".{target.name}.prepare-{secrets.token_hex(12)}"
        )
        try:
            candidate.mkdir(mode=0o700)
        except FileExistsError:
            continue
        try:
            candidate.chmod(0o700)
            return _owned_directory(candidate)
        except BaseException:
            try:
                candidate.rmdir()
            except OSError:
                pass
            raise
    raise ReplayPreparationError("could not allocate a unique staging directory")


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_tree(root: Path) -> None:
    for directory, _names, _filenames in os.walk(
        root, topdown=False, followlinks=False
    ):
        _fsync_directory(Path(directory))


def _publish_no_replace(staging: Path, target: Path) -> None:
    """Atomically publish a directory without replacing an existing path."""
    source_bytes = os.fsencode(staging)
    target_bytes = os.fsencode(target)
    library = ctypes.CDLL(None, use_errno=True)
    result: int
    if sys.platform == "darwin" and hasattr(library, "renamex_np"):
        renamex_np = library.renamex_np
        renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        renamex_np.restype = ctypes.c_int
        result = renamex_np(source_bytes, target_bytes, 0x00000004)
    elif sys.platform.startswith("linux") and hasattr(library, "renameat2"):
        renameat2 = library.renameat2
        renameat2.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameat2.restype = ctypes.c_int
        result = renameat2(-100, source_bytes, -100, target_bytes, 0x00000001)
    elif os.name == "nt":
        try:
            os.rename(staging, target)
        except FileExistsError as exc:
            raise ReplayPreparationError(
                f"workspace appeared during publication: {target}"
            ) from exc
        return
    else:
        raise ReplayPreparationError(
            "this platform lacks atomic no-replace directory publication"
        )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise ReplayPreparationError(
            f"workspace appeared during publication: {target}"
        )
    raise OSError(error_number, os.strerror(error_number), str(target))


def _binding_projection(binding: dict[str, Any]) -> dict[str, Any]:
    return {
        "input_id": binding["input_id"],
        "members": [
            {
                "object": member["object"],
                "path": member["path"],
                "source_manifest_id": member["source_manifest_id"],
            }
            for member in binding["members"]
        ],
        "source_manifest_ids": binding["source_manifest_ids"],
        "state": binding["state"],
    }


def prepare_replay_v2(
    manifest_path: str | os.PathLike[str],
    workspace: str | os.PathLike[str],
    *,
    authority_git_commit: str,
    authority_root: str,
    semantic_epoch: str,
    prior_state_root: str | None = None,
    pack_root: str | os.PathLike[str] | None = None,
    expected_pack_schema: str | None = None,
) -> PreparedReplay:
    """Verify ``manifest_path`` and copy its exact replay closure to ``workspace``."""
    manifest = _absolute_existing_file(manifest_path, "manifest")
    root = _absolute_existing_directory(
        pack_root if pack_root is not None else manifest.parent,
        "pack root",
    )
    target = _fresh_workspace_path(workspace)
    if _paths_overlap(root, target):
        raise ReplayPreparationError(
            "workspace and offline-pack root must be disjoint by ancestry"
        )

    pack_value, _ = contracts.load_canonical_json(manifest)
    pack = contracts.validate_offline_pack(pack_value)
    supported_pack_schemas = {
        contracts.PACK_SCHEMA_V2,
        contracts.PACK_SCHEMA_V3,
    }
    if pack["schema"] not in supported_pack_schemas:
        raise ReplayPreparationError(
            "prepare_replay_v2 requires offline-pack/v2 or offline-pack/v3"
        )
    if expected_pack_schema is not None:
        if expected_pack_schema not in supported_pack_schemas:
            raise ReplayPreparationError(
                f"unsupported expected pack schema {expected_pack_schema!r}"
            )
        if pack["schema"] != expected_pack_schema:
            raise ReplayPreparationError(
                "offline-pack schema changed before replay preparation"
            )
    contracts.verify_offline_pack_files(pack, root, manifest_path=manifest)

    environment_ref = pack["environment"]
    environment_document, environment_bytes = _load_verified_json(
        root, environment_ref, "$.environment"
    )
    contracts.validate_execution_environment(
        environment_document, location="$.environment.document"
    )
    environment_id = environment_document["environment_id"]

    inventory, _ = _load_verified_json(root, pack["inventory"], "$.inventory")
    contracts.validate_reducer_input_inventory(inventory)
    if inventory["inventory_id"] != pack["inventory"]["inventory_id"]:
        raise ReplayPreparationError("$.inventory: inventory ID cross-check failed")

    source_manifests: dict[str, dict[str, Any]] = {}
    source_objects: dict[tuple[str, str], dict[str, Any]] = {}
    packed_objects = {item["path"]: item for item in pack["objects"]}
    for index, ref in enumerate(pack["source_manifests"]):
        location = f"$.source_manifests[{index}]"
        source_manifest, _ = _load_verified_json(root, ref, location)
        contracts.validate_source_manifest(source_manifest)
        manifest_id = source_manifest["source_manifest_id"]
        expected_source_schema = (
            contracts.SOURCE_SCHEMA_V3
            if pack["schema"] == contracts.PACK_SCHEMA_V3
            else contracts.SOURCE_SCHEMA_V2
        )
        if source_manifest["schema"] != expected_source_schema:
            raise ReplayPreparationError(
                f"{location}: expected {expected_source_schema}"
            )
        if manifest_id != ref["source_manifest_id"]:
            raise ReplayPreparationError(f"{location}: source manifest ID mismatch")
        source_manifests[manifest_id] = source_manifest
        for source_object in source_manifest["objects"]:
            packed = packed_objects.get(source_object["path"])
            if packed is None or any(
                packed[field] != source_object[field]
                for field in ("path", "sha256", "bytes", "media_type")
            ):
                raise ReplayPreparationError(
                    f"{location}: source object disagrees with pack.objects"
                )
            source_objects[(manifest_id, source_object["name"])] = source_object

    configuration_ref = pack["configuration"]
    if configuration_ref["media_type"] != "application/json":
        raise ReplayPreparationError(
            "$.configuration.media_type: expected 'application/json'"
        )
    configuration_document, configuration_bytes = _load_verified_json(
        root, configuration_ref, "$.configuration"
    )
    configuration = build_context.ReducerConfiguration.from_document(
        configuration_document
    )
    if configuration_bytes != build_context.canonical_json_bytes(
        configuration.to_document()
    ):
        raise ReplayPreparationError(
            "$.configuration: reducer configuration is not its exact canonical form"
        )

    declarations = {item["id"]: item for item in inventory["inputs"]}
    pack_bindings = {item["input_id"]: item for item in pack["input_bindings"]}
    if set(declarations) != set(pack_bindings):
        raise ReplayPreparationError(
            "input binding IDs do not exactly equal inventory input IDs"
        )

    roots = {
        "code": target / "code",
        "input": target / "input",
        "output": target / "output",
        "scratch": target / "scratch",
    }
    runtime_bindings: list[dict[str, Any]] = []
    input_copy_plans: list[_CopyPlan] = []
    input_destinations: list[str] = []
    for input_id in sorted(declarations):
        declaration = declarations[input_id]
        packed_binding = pack_bindings[input_id]
        runtime_members: list[dict[str, Any]] = []
        for member_index, member in enumerate(packed_binding["members"]):
            source_key = (member["source_manifest_id"], member["object"])
            source_object = source_objects.get(source_key)
            source_manifest = source_manifests.get(member["source_manifest_id"])
            location = f"$.input_bindings[{input_id!r}].members[{member_index}]"
            if source_object is None or source_manifest is None:
                raise ReplayPreparationError(f"{location}: unknown source object")
            if "normalized" not in source_object["roles"]:
                raise ReplayPreparationError(f"{location}: object is not normalized")
            logical_destination = f"{declaration['root']}/{member['path']}"
            input_destinations.append(logical_destination)
            destination = roots["input"].joinpath(
                *PurePosixPath(logical_destination).parts
            )
            input_copy_plans.append(
                _CopyPlan(
                    source_object,
                    f"input/{logical_destination}",
                    location,
                )
            )
            runtime_members.append(
                {
                    "bytes": source_object["bytes"],
                    "materialized_path": str(destination),
                    "media_type": source_object["media_type"],
                    "object": member["object"],
                    "path": member["path"],
                    "pin": source_manifest["pin"],
                    "sha256": source_object["sha256"],
                    "source_manifest_id": member["source_manifest_id"],
                }
            )
        runtime_binding = {
            "cardinality": declaration["cardinality"],
            "class": declaration["class"],
            "input_id": input_id,
            "members": runtime_members,
            "requirement": declaration["requirement"],
            "root": declaration["root"],
            "source_manifest_ids": packed_binding["source_manifest_ids"],
            "state": packed_binding["state"],
            (
                "path"
                if declaration["cardinality"] == "one"
                else "path_pattern"
            ): declaration[
                "path"
                if declaration["cardinality"] == "one"
                else "path_pattern"
            ],
        }
        if _binding_projection(runtime_binding) != packed_binding:
            raise ReplayPreparationError(
                f"input binding {input_id!r} does not exactly reproduce the pack binding"
            )
        runtime_bindings.append(runtime_binding)

    reducer_copy_plans: list[_CopyPlan] = []
    reducer_destinations: list[str] = []
    for index, reducer_ref in enumerate(pack["reducer"]["files"]):
        logical_path = reducer_ref["logical_path"]
        reducer_destinations.append(logical_path)
        reducer_copy_plans.append(
            _CopyPlan(
                reducer_ref,
                f"code/{logical_path}",
                f"$.reducer.files[{index}]",
            )
        )
    _reject_destination_collisions(input_destinations, "input materialization")
    _reject_destination_collisions(reducer_destinations, "reducer materialization")
    environment_copy_plan = _CopyPlan(
        environment_ref,
        "execution-environment.json",
        "$.environment",
    )

    replay_document = {
        "authority": {
            "authority_root": authority_root,
            "git_commit": authority_git_commit,
        },
        "offline_pack_id": pack["offline_pack_id"],
        "prior_state_root": prior_state_root,
        "reducer": {
            "configuration_sha256": configuration_ref["sha256"],
            "environment_sha256": pack["environment"]["sha256"],
            "git_commit": pack["reducer"]["git_commit"],
        },
        "reducer_inventory_id": inventory["inventory_id"],
        "semantic_epoch": semantic_epoch,
        "source_set_root": pack["source_set_root"],
    }
    context_document: dict[str, Any] = {
        "bindings": runtime_bindings,
        "configuration": configuration.to_document(),
        "generation_id": "sha256:" + "0" * 64,
        "replay": replay_document,
        "roots": {name: str(path) for name, path in roots.items()},
        "schema": build_context.BUILD_CONTEXT_SCHEMA,
        "stages": inventory["stages"],
    }
    context_document["generation_id"] = build_context.generation_identity(
        context_document
    )
    context = build_context.BuildContext.from_document(context_document)
    materialized_document = context.to_document()
    if materialized_document["stages"] != inventory["stages"]:
        raise ReplayPreparationError("runtime stages do not exactly equal inventory stages")
    if [
        _binding_projection(binding) for binding in materialized_document["bindings"]
    ] != pack["input_bindings"]:
        raise ReplayPreparationError(
            "runtime bindings do not exactly equal offline-pack bindings"
        )

    staging_ownership: _OwnedDirectory | None = None
    published = False
    try:
        staging_ownership = _create_staging_directory(target)
        staging = staging_ownership.path
        staging_roots = {
            name: staging / name for name in ("code", "input", "output", "scratch")
        }
        for path in staging_roots.values():
            path.mkdir(mode=0o700, exist_ok=False)
            path.chmod(0o700)
        for root_entry in inventory["roots"]:
            input_root = staging_roots["input"] / root_entry["id"]
            input_root.mkdir(mode=0o700, exist_ok=False)
            input_root.chmod(0o700)

        for plan in input_copy_plans:
            _copy_verified_file(root, plan, staging)
        for plan in reducer_copy_plans:
            _copy_verified_file(root, plan, staging)
        _copy_verified_file(root, environment_copy_plan, staging)

        staged_environment_path = staging / "execution-environment.json"
        environment_metadata = staged_environment_path.lstat()
        if (
            not stat.S_ISREG(environment_metadata.st_mode)
            or stat.S_ISLNK(environment_metadata.st_mode)
            or stat.S_IMODE(environment_metadata.st_mode) != 0o444
            or environment_metadata.st_nlink != 1
        ):
            raise ReplayPreparationError(
                "materialized execution environment must be a private mode-0o444 regular file"
            )
        if staged_environment_path.read_bytes() != environment_bytes:
            raise ReplayPreparationError(
                "$.environment: source changed between validation and materialization"
            )

        context_path = staging / "build-context.json"
        _write_exclusive(
            context_path,
            build_context.canonical_json_bytes(materialized_document),
        )
        loaded = build_context.BuildContext.load(context_path)
        if loaded.generation_id != context.generation_id:
            raise ReplayPreparationError("emitted build context failed identity check")

        _make_read_only_tree(staging_roots["input"])
        _make_read_only_tree(staging_roots["code"])
        _fsync_tree(staging)
        result = PreparedReplay(
            workspace=target,
            context_path=target / "build-context.json",
            environment_path=target / "execution-environment.json",
            environment_id=environment_id,
            environment_sha256=environment_ref["sha256"],
            reducer_git_commit=pack["reducer"]["git_commit"],
            configuration_sha256=configuration_ref["sha256"],
            generation_id=context.generation_id,
            offline_pack_id=pack["offline_pack_id"],
            source_set_root=pack["source_set_root"],
            reducer_inventory_id=inventory["inventory_id"],
            reducer_files=tuple(
                (
                    reducer_ref["logical_path"],
                    reducer_ref["bytes"],
                    reducer_ref["sha256"],
                )
                for reducer_ref in pack["reducer"]["files"]
            ),
        )
        _publish_no_replace(staging, target)
        published = True
        try:
            _fsync_directory(target.parent)
        except OSError as exc:
            raise ReplayPreparationError(
                f"workspace was published at {target}, but its parent directory "
                f"could not be synchronized: {exc}"
            ) from exc
    except BaseException as exc:
        if staging_ownership is not None and not published:
            try:
                _remove_created_workspace(staging_ownership)
            except Exception as cleanup_error:
                if hasattr(exc, "add_note"):
                    exc.add_note(f"staging cleanup also failed: {cleanup_error}")
        raise

    return result


class _JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ReplayPreparationError(f"arguments: {message}")


def _parser() -> argparse.ArgumentParser:
    parser = _JsonArgumentParser(
        description=(
            "Verify and materialize an offline-pack/v2 or offline-pack/v3 "
            "replay workspace."
        )
    )
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument(
        "--root",
        type=Path,
        help="offline-pack root (default: manifest directory)",
    )
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--authority-git-commit", required=True)
    parser.add_argument("--authority-root", required=True)
    parser.add_argument("--semantic-epoch", required=True)
    parser.add_argument("--prior-state-root")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        result = prepare_replay_v2(
            args.manifest,
            args.workspace,
            pack_root=args.root,
            authority_git_commit=args.authority_git_commit,
            authority_root=args.authority_root,
            semantic_epoch=args.semantic_epoch,
            prior_state_root=args.prior_state_root,
        )
    except (
        ReplayPreparationError,
        contracts.VerificationError,
        build_context.BuildContextError,
        OSError,
    ) as exc:
        error = {
            "error": {"message": str(exc), "type": type(exc).__name__},
            "ok": False,
        }
        notes = getattr(exc, "__notes__", None)
        if notes:
            error["error"]["notes"] = list(notes)
        print(build_context.canonical_json_bytes(error).decode("utf-8"), file=sys.stderr)
        return 1
    print(
        build_context.canonical_json_bytes(result.to_document()).decode("utf-8")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
