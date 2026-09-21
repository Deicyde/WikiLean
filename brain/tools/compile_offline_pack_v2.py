#!/usr/bin/env python3
"""Compile an immutable, self-verifying ``offline-pack/v2`` or ``v3`` directory.

The compiler is deliberately post-acquisition and network-free.  A canonical
source plan names every physical source object and every intended present or
absent reducer input.  Absolute host paths are supplied separately as root
bindings, so neither the plan nor the resulting pack identity depends on where
the inputs happen to be mounted.

For source-plan/v3, the compiler verifies and seals acquisition receipts,
normalization lineage, and request-parameter preimages before publication.
Source-plan/v1 retains its byte-identical offline-pack/v2 behavior.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import ctypes
import errno
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Callable, Iterator, Mapping, Sequence


HERE = Path(__file__).resolve().parent
BRAIN = HERE.parent
if str(BRAIN) not in sys.path:
    sys.path.insert(0, str(BRAIN))

import authority_contracts as contracts  # noqa: E402
import build_context  # noqa: E402


SOURCE_PLAN_SCHEMA = "wikilean.offline-pack-source-plan/v1"
SOURCE_PLAN_SCHEMA_V3 = contracts.OFFLINE_PACK_SOURCE_PLAN_SCHEMA_V3
ZERO_HASH = "sha256:" + "0" * 64
NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
MEDIA_TYPE_RE = re.compile(r"^[^/\s]+/[^/\s]+$")
MAX_SAFE_INTEGER = 9_007_199_254_740_991


class PackCompilationError(ValueError):
    """A source plan or source tree cannot produce the requested sealed pack."""


class _DestinationExists(PackCompilationError):
    pass


@dataclass(frozen=True, slots=True)
class CompiledPack:
    root: Path
    manifest_path: Path
    offline_pack_id: str
    source_set_root: str
    source_manifests: int
    source_objects: int
    files: int
    bytes: int
    reused: bool

    def to_document(self) -> dict[str, Any]:
        return {
            "bytes": self.bytes,
            "files": self.files,
            "manifest": str(self.manifest_path),
            "offline_pack_id": self.offline_pack_id,
            "ok": True,
            "reused": self.reused,
            "root": str(self.root),
            "source_manifests": self.source_manifests,
            "source_objects": self.source_objects,
            "source_set_root": self.source_set_root,
        }


@dataclass(frozen=True, slots=True)
class _DirectoryIdentity:
    device: int
    inode: int
    owner: int


def _fail(location: str, message: str) -> None:
    raise PackCompilationError(f"{location}: {message}")


def _object(
    value: Any,
    location: str,
    required: set[str],
    optional: set[str] = frozenset(),
) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(location, "expected an object")
    keys = set(value)
    missing = sorted(required - keys)
    unknown = sorted(keys - required - optional)
    if missing:
        _fail(location, "missing keys: " + ", ".join(missing))
    if unknown:
        _fail(location, "unknown keys: " + ", ".join(unknown))
    return value


def _array(value: Any, location: str, *, nonempty: bool = False) -> list[Any]:
    if not isinstance(value, list):
        _fail(location, "expected an array")
    if nonempty and not value:
        _fail(location, "must not be empty")
    return value


def _string(value: Any, location: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str):
        _fail(location, "expected a string")
    if nonempty and not value:
        _fail(location, "must not be empty")
    return value


def _integer(value: Any, location: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(location, "expected an integer")
    if not 0 <= value <= MAX_SAFE_INTEGER:
        _fail(location, f"must be between 0 and {MAX_SAFE_INTEGER}")
    return value


def _pattern(
    value: Any,
    location: str,
    pattern: re.Pattern[str],
    description: str,
) -> str:
    text = _string(value, location)
    if pattern.fullmatch(text) is None:
        _fail(location, f"expected {description}")
    return text


def _name(value: Any, location: str) -> str:
    return _pattern(value, location, NAME_RE, "a lowercase stable name")


def _digest(value: Any, location: str) -> str:
    return _pattern(value, location, DIGEST_RE, "64 lowercase SHA-256 hex characters")


def _hash(value: Any, location: str) -> str:
    return _pattern(value, location, HASH_RE, "sha256:<64 lowercase hex>")


def _git_commit(value: Any, location: str) -> str:
    return _pattern(value, location, GIT_COMMIT_RE, "a full lowercase Git commit")


def _media_type(value: Any, location: str) -> str:
    return _pattern(value, location, MEDIA_TYPE_RE, "a media type")


def _literal_path(value: Any, location: str) -> str:
    try:
        return contracts.validate_literal_relative_path(value, location)
    except contracts.VerificationError as exc:
        raise PackCompilationError(str(exc)) from exc


def _root_file(value: Any, location: str) -> dict[str, Any]:
    obj = _object(value, location, {"root", "path", "sha256", "bytes"})
    return {
        "root": _name(obj["root"], f"{location}.root"),
        "path": _literal_path(obj["path"], f"{location}.path"),
        "sha256": _digest(obj["sha256"], f"{location}.sha256"),
        "bytes": _integer(obj["bytes"], f"{location}.bytes"),
    }


def _tool(value: Any, location: str) -> dict[str, str]:
    obj = _object(value, location, {"name", "version", "sha256"})
    return {
        "name": _string(obj["name"], f"{location}.name"),
        "version": _string(obj["version"], f"{location}.version"),
        "sha256": _digest(obj["sha256"], f"{location}.sha256"),
    }


def _pin(value: Any, location: str) -> dict[str, str]:
    obj = _object(value, location, {"type", "value"}, {"tree"})
    pin_type = _string(obj["type"], f"{location}.type")
    if pin_type not in {
        "git_commit",
        "content_sha256",
        "dataset_revision",
        "http_etag",
        "database_snapshot",
    }:
        _fail(f"{location}.type", "unknown pin type")
    pin_value = _string(obj["value"], f"{location}.value")
    if len(pin_value) > 512:
        _fail(f"{location}.value", "must contain at most 512 characters")
    if pin_type == "git_commit":
        _git_commit(pin_value, f"{location}.value")
    if pin_type == "content_sha256":
        _digest(pin_value, f"{location}.value")
    result = {"type": pin_type, "value": pin_value}
    if "tree" in obj:
        if pin_type != "git_commit":
            _fail(f"{location}.tree", "tree is permitted only for a git_commit pin")
        result["tree"] = _git_commit(obj["tree"], f"{location}.tree")
    return result


def _license(value: Any, location: str) -> dict[str, Any]:
    obj = _object(
        value,
        location,
        {"expression", "redistribution"},
        {"attribution", "notice"},
    )
    redistribution = _string(
        obj["redistribution"], f"{location}.redistribution"
    )
    if redistribution not in {"allowed", "restricted", "link-only", "unknown"}:
        _fail(f"{location}.redistribution", "unknown redistribution policy")
    result: dict[str, Any] = {
        "expression": _string(obj["expression"], f"{location}.expression"),
        "redistribution": redistribution,
    }
    for key in ("attribution", "notice"):
        if key in obj:
            if obj[key] is not None:
                _string(obj[key], f"{location}.{key}", nonempty=False)
            result[key] = obj[key]
    return result


def validate_source_plan(value: Any) -> dict[str, Any]:
    """Validate the canonical, relocation-independent pack source plan."""
    if isinstance(value, dict) and value.get("schema") == SOURCE_PLAN_SCHEMA_V3:
        # Lazy import avoids a module-initialization cycle: the v3 contract
        # validator reuses this function against a synthetic v1 plan.
        import source_plan_contracts

        try:
            return source_plan_contracts.validate_source_plan_v3(value)
        except contracts.VerificationError as exc:
            raise PackCompilationError(str(exc)) from exc
    obj = _object(
        value,
        "$",
        {
            "schema",
            "inventory_id",
            "input_bindings",
            "sources",
            "reducer",
            "configuration",
            "environment",
            "schemas",
        },
    )
    if obj["schema"] != SOURCE_PLAN_SCHEMA:
        _fail("$.schema", f"expected {SOURCE_PLAN_SCHEMA!r}")
    _hash(obj["inventory_id"], "$.inventory_id")

    sources = _array(obj["sources"], "$.sources", nonempty=True)
    source_names: list[str] = []
    source_objects: dict[tuple[str, str], dict[str, Any]] = {}
    normalized_objects: set[tuple[str, str]] = set()
    for index, raw_source in enumerate(sources):
        location = f"$.sources[{index}]"
        source = _object(
            raw_source,
            location,
            {
                "source",
                "source_kind",
                "pin",
                "objects",
                "license",
                "acquisition",
                "normalization",
            },
            {"previous_source_manifest_id", "review", "audit"},
        )
        source_name = _name(source["source"], f"{location}.source")
        source_names.append(source_name)
        source_kind = _string(source["source_kind"], f"{location}.source_kind")
        if source_kind not in {
            "acquired_dataset",
            "curated_git_tree",
            "sealed_snapshot",
        }:
            _fail(f"{location}.source_kind", "unknown source kind")
        pin = _pin(source["pin"], f"{location}.pin")
        if source_kind == "curated_git_tree" and (
            pin["type"] != "git_commit" or "tree" not in pin
        ):
            _fail(
                f"{location}.pin",
                "curated_git_tree requires a git_commit pin and tree",
            )
        _license(source["license"], f"{location}.license")
        _tool(source["acquisition"], f"{location}.acquisition")

        objects = _array(source["objects"], f"{location}.objects", nonempty=True)
        object_names: list[str] = []
        object_digests: list[str] = []
        roles_by_name: dict[str, list[str]] = {}
        for object_index, raw_object in enumerate(objects):
            object_location = f"{location}.objects[{object_index}]"
            source_object = _object(
                raw_object,
                object_location,
                {
                    "name",
                    "roles",
                    "root",
                    "path",
                    "sha256",
                    "bytes",
                    "media_type",
                    "redistribution",
                },
            )
            object_name = _name(source_object["name"], f"{object_location}.name")
            object_names.append(object_name)
            roles = _array(
                source_object["roles"], f"{object_location}.roles", nonempty=True
            )
            for role_index, raw_role in enumerate(roles):
                role = _string(
                    raw_role,
                    f"{object_location}.roles[{role_index}]",
                )
                if role not in {"raw", "normalized", "receipt"}:
                    _fail(
                        f"{object_location}.roles[{role_index}]",
                        "unknown source object role",
                    )
            if roles != sorted(set(roles)):
                _fail(f"{object_location}.roles", "entries must be unique and sorted")
            _name(source_object["root"], f"{object_location}.root")
            _literal_path(source_object["path"], f"{object_location}.path")
            object_digests.append(
                _digest(source_object["sha256"], f"{object_location}.sha256")
            )
            _integer(source_object["bytes"], f"{object_location}.bytes")
            _media_type(source_object["media_type"], f"{object_location}.media_type")
            redistribution = _string(
                source_object["redistribution"],
                f"{object_location}.redistribution",
            )
            if redistribution not in {
                "allowed",
                "restricted",
                "link-only",
                "unknown",
            }:
                _fail(
                    f"{object_location}.redistribution",
                    "unknown redistribution policy",
                )
            source_objects[(source_name, object_name)] = source_object
            roles_by_name[object_name] = roles
            if "normalized" in roles:
                normalized_objects.add((source_name, object_name))
        if object_names != sorted(set(object_names)):
            _fail(f"{location}.objects", "entries must have unique names and be sorted")
        if len(object_digests) != len(set(object_digests)):
            _fail(
                f"{location}.objects",
                "equal expected content must be represented by one object with combined roles",
            )

        normalization = _object(
            source["normalization"],
            f"{location}.normalization",
            {"schema", "tool", "inputs", "outputs"},
        )
        _string(normalization["schema"], f"{location}.normalization.schema")
        _tool(normalization["tool"], f"{location}.normalization.tool")
        normalization_inputs = _array(
            normalization["inputs"],
            f"{location}.normalization.inputs",
            nonempty=True,
        )
        normalization_outputs = _array(
            normalization["outputs"],
            f"{location}.normalization.outputs",
            nonempty=True,
        )
        for field, values in (
            ("inputs", normalization_inputs),
            ("outputs", normalization_outputs),
        ):
            for item_index, item in enumerate(values):
                _name(item, f"{location}.normalization.{field}[{item_index}]")
            if values != sorted(set(values)):
                _fail(
                    f"{location}.normalization.{field}",
                    "entries must be unique and sorted",
                )
        raw_names = sorted(
            name for name, roles in roles_by_name.items() if "raw" in roles
        )
        normalized_names = sorted(
            name for name, roles in roles_by_name.items() if "normalized" in roles
        )
        if pin["type"] == "content_sha256":
            if len(raw_names) != 1:
                _fail(
                    f"{location}.pin",
                    "content_sha256 requires exactly one raw source object",
                )
            raw_object = source_objects[(source_name, raw_names[0])]
            if pin["value"] != raw_object["sha256"]:
                _fail(
                    f"{location}.pin.value",
                    "must equal the sole raw source object's sha256",
                )
        if normalization_inputs != raw_names:
            _fail(
                f"{location}.normalization.inputs",
                "must name every raw object exactly once",
            )
        if normalization_outputs != normalized_names:
            _fail(
                f"{location}.normalization.outputs",
                "must name every normalized object exactly once",
            )
        if (
            "previous_source_manifest_id" in source
            and source["previous_source_manifest_id"] is not None
        ):
            _hash(
                source["previous_source_manifest_id"],
                f"{location}.previous_source_manifest_id",
            )
        if "review" in source:
            review = _object(
                source["review"],
                f"{location}.review",
                {"summary", "expected_semantic_effects"},
            )
            _string(
                review["summary"],
                f"{location}.review.summary",
                nonempty=False,
            )
            effects = _array(
                review["expected_semantic_effects"],
                f"{location}.review.expected_semantic_effects",
            )
            for effect_index, effect in enumerate(effects):
                _string(
                    effect,
                    f"{location}.review.expected_semantic_effects[{effect_index}]",
                    nonempty=False,
                )
        if "audit" in source:
            audit = _object(
                source["audit"],
                f"{location}.audit",
                set(),
                {"acquired_at", "upstream_uri"},
            )
            for key, value in audit.items():
                _string(value, f"{location}.audit.{key}")

    if source_names != sorted(set(source_names)):
        _fail("$.sources", "entries must have unique source names and be sorted")

    bindings = _array(obj["input_bindings"], "$.input_bindings", nonempty=True)
    binding_ids: list[str] = []
    for index, raw_binding in enumerate(bindings):
        location = f"$.input_bindings[{index}]"
        binding = _object(
            raw_binding,
            location,
            {"input_id", "sources", "state", "members"},
        )
        binding_ids.append(_name(binding["input_id"], f"{location}.input_id"))
        raw_binding_sources = _array(
            binding["sources"],
            f"{location}.sources",
            nonempty=True,
        )
        binding_sources = [
            _name(value, f"{location}.sources[{source_index}]")
            for source_index, value in enumerate(raw_binding_sources)
        ]
        if binding_sources != sorted(set(binding_sources)):
            _fail(f"{location}.sources", "entries must be unique and sorted")
        unknown_sources = sorted(set(binding_sources) - set(source_names))
        if unknown_sources:
            _fail(
                f"{location}.sources",
                "references unknown sources: " + ", ".join(unknown_sources),
            )
        state = _string(binding["state"], f"{location}.state")
        if state not in {"present", "absent"}:
            _fail(f"{location}.state", "expected present or absent")
        members = _array(binding["members"], f"{location}.members")
        member_paths: list[str] = []
        member_sources: list[str] = []
        for member_index, raw_member in enumerate(members):
            member_location = f"{location}.members[{member_index}]"
            member = _object(
                raw_member, member_location, {"path", "source", "object"}
            )
            member_paths.append(
                _literal_path(member["path"], f"{member_location}.path")
            )
            source_key = (
                _name(member["source"], f"{member_location}.source"),
                _name(member["object"], f"{member_location}.object"),
            )
            member_sources.append(source_key[0])
            if source_key[0] not in binding_sources:
                _fail(
                    f"{member_location}.source",
                    "must belong to the binding sources",
                )
            if source_key not in source_objects:
                _fail(member_location, "references an unknown source object")
            if source_key not in normalized_objects:
                _fail(member_location, "bindings must reference normalized objects")
        if member_paths != sorted(set(member_paths)):
            _fail(f"{location}.members", "entries must have unique paths and be sorted")
        if state == "present" and not members:
            _fail(f"{location}.members", "present bindings must contain members")
        if state == "absent" and members:
            _fail(f"{location}.members", "absent bindings must contain no members")
        if state == "present" and binding_sources != sorted(set(member_sources)):
            _fail(
                f"{location}.sources",
                "present binding sources must exactly equal the member source set",
            )
    if binding_ids != sorted(set(binding_ids)):
        _fail("$.input_bindings", "entries must have unique IDs and be sorted")

    reducer = _object(obj["reducer"], "$.reducer", {"root", "git_commit", "entrypoint"})
    _name(reducer["root"], "$.reducer.root")
    _git_commit(reducer["git_commit"], "$.reducer.git_commit")
    _literal_path(reducer["entrypoint"], "$.reducer.entrypoint")
    _root_file(obj["configuration"], "$.configuration")
    _root_file(obj["environment"], "$.environment")

    schemas = _array(obj["schemas"], "$.schemas", nonempty=True)
    schema_paths: list[str] = []
    for index, raw_schema in enumerate(schemas):
        location = f"$.schemas[{index}]"
        schema = _object(
            raw_schema,
            location,
            {"root", "path", "sha256", "bytes", "pack_path", "media_type"},
        )
        _name(schema["root"], f"{location}.root")
        _literal_path(schema["path"], f"{location}.path")
        _digest(schema["sha256"], f"{location}.sha256")
        _integer(schema["bytes"], f"{location}.bytes")
        pack_path = _literal_path(schema["pack_path"], f"{location}.pack_path")
        if not pack_path.startswith("schemas/"):
            _fail(f"{location}.pack_path", "must reside beneath schemas/")
        _media_type(schema["media_type"], f"{location}.media_type")
        schema_paths.append(pack_path)
    if schema_paths != sorted(set(schema_paths)):
        _fail("$.schemas", "entries must have unique pack paths and be sorted")
    _reject_path_collisions(schema_paths)
    return obj


def load_source_plan(path: Path) -> tuple[dict[str, Any], bytes]:
    try:
        value, raw = contracts.load_canonical_json(path)
    except (OSError, contracts.VerificationError) as exc:
        raise PackCompilationError(f"source plan: {exc}") from exc
    return validate_source_plan(value), raw


def _real_directory(path: str | os.PathLike[str], location: str) -> Path:
    requested = Path(path)
    if requested.is_symlink():
        _fail(location, "symlinks are forbidden")
    try:
        resolved = requested.resolve(strict=True)
    except OSError as exc:
        raise PackCompilationError(f"{location}: cannot resolve directory: {exc}") from exc
    if not resolved.is_dir():
        _fail(location, "expected a directory")
    return resolved


def _real_file(path: str | os.PathLike[str], location: str) -> Path:
    requested = Path(path)
    if requested.is_symlink():
        _fail(location, "symlinks are forbidden")
    try:
        resolved = requested.resolve(strict=True)
    except OSError as exc:
        raise PackCompilationError(f"{location}: cannot resolve file: {exc}") from exc
    if not resolved.is_file():
        _fail(location, "expected a regular file")
    return resolved


def _directory_identity(path: Path, location: str) -> _DirectoryIdentity:
    try:
        value = path.lstat()
    except OSError as exc:
        raise PackCompilationError(f"{location}: cannot inspect directory: {exc}") from exc
    if stat.S_ISLNK(value.st_mode) or not stat.S_ISDIR(value.st_mode):
        _fail(location, "expected a real directory")
    return _DirectoryIdentity(value.st_dev, value.st_ino, value.st_uid)


def _owned_directory_identity(path: Path, location: str) -> _DirectoryIdentity:
    identity = _directory_identity(path, location)
    if not hasattr(os, "getuid"):
        _fail(location, "platform lacks required directory ownership checks")
    owner = os.getuid()
    if identity.owner != owner:
        _fail(location, f"must be owned by current user {owner}")
    return identity


def _verify_directory_identity(
    path: Path,
    expected: _DirectoryIdentity,
    location: str,
) -> None:
    actual = _directory_identity(path, location)
    if actual != expected:
        _fail(location, "directory inode or ownership changed during compilation")


def _validate_private_directory(
    path: Path,
    location: str,
) -> _DirectoryIdentity:
    identity = _owned_directory_identity(path, location)
    mode = stat.S_IMODE(path.lstat().st_mode)
    if mode != 0o700:
        _fail(location, f"expected mode 0700, found {mode:04o}")
    return identity


def _validate_private_store(path: Path) -> _DirectoryIdentity:
    return _validate_private_directory(path, "output store")


def _identity_from_stat(value: os.stat_result, location: str) -> _DirectoryIdentity:
    if stat.S_ISLNK(value.st_mode) or not stat.S_ISDIR(value.st_mode):
        _fail(location, "expected a real directory")
    return _DirectoryIdentity(value.st_dev, value.st_ino, value.st_uid)


def _open_private_store(
    path: Path,
    expected: _DirectoryIdentity,
) -> int:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise PackCompilationError(f"output store: cannot open directory: {exc}") from exc
    try:
        value = os.fstat(descriptor)
        actual = _identity_from_stat(value, "output store")
        if actual != expected:
            _fail("output store", "directory inode changed while it was being opened")
        if not hasattr(os, "getuid") or actual.owner != os.getuid():
            _fail("output store", "must be owned by the current user")
        mode = stat.S_IMODE(value.st_mode)
        if mode != 0o700:
            _fail("output store", f"expected mode 0700, found {mode:04o}")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _verify_store_descriptor(
    descriptor: int,
    expected: _DirectoryIdentity,
) -> None:
    try:
        value = os.fstat(descriptor)
    except OSError as exc:
        raise PackCompilationError(f"output store: cannot inspect descriptor: {exc}") from exc
    actual = _identity_from_stat(value, "output store")
    if actual != expected:
        _fail("output store", "open directory inode or ownership changed")
    mode = stat.S_IMODE(value.st_mode)
    if mode != 0o700:
        _fail("output store", f"expected mode 0700, found {mode:04o}")


def _directory_identity_at(
    parent_descriptor: int,
    name: str,
    location: str,
    *,
    missing_ok: bool = False,
) -> _DirectoryIdentity | None:
    try:
        value = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise PackCompilationError(f"{location}: directory is absent") from None
    except OSError as exc:
        raise PackCompilationError(f"{location}: cannot inspect directory: {exc}") from exc
    return _identity_from_stat(value, location)


def _verify_directory_identity_at(
    parent_descriptor: int,
    name: str,
    expected: _DirectoryIdentity,
    location: str,
) -> None:
    actual = _directory_identity_at(parent_descriptor, name, location)
    if actual != expected:
        _fail(location, "directory inode or ownership changed")


def _resolve_output_store(
    path: str | os.PathLike[str],
) -> tuple[Path, bool]:
    requested = Path(path)
    if not requested.is_absolute():
        requested = Path.cwd() / requested
    if requested.exists() or requested.is_symlink():
        store = _real_directory(requested, "output store")
        _validate_private_store(store)
        return store, True
    try:
        parent = requested.parent.resolve(strict=True)
    except OSError as exc:
        raise PackCompilationError(
            f"output store: parent must already exist: {exc}"
        ) from exc
    if parent.is_symlink() or not parent.is_dir():
        _fail("output store", "parent must be a real directory")
    return parent / requested.name, False


def _create_output_store(target: Path) -> tuple[Path, _DirectoryIdentity]:
    try:
        target.mkdir(mode=0o700)
        target.chmod(0o700)
        _fsync_directory(target.parent)
    except FileExistsError:
        store = _real_directory(target, "output store")
        return store, _validate_private_store(store)
    except OSError as exc:
        raise PackCompilationError(f"output store: cannot create {target}: {exc}") from exc
    store = _real_directory(target, "output store")
    return store, _validate_private_store(store)


def _create_private_staging(
    store_descriptor: int,
    store_identity: _DirectoryIdentity,
    name: str,
) -> _DirectoryIdentity:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    staging_descriptor = -1
    created = False
    created_identity: _DirectoryIdentity | None = None
    try:
        store_stat = os.fstat(store_descriptor)
        actual_store = _identity_from_stat(store_stat, "output store")
        if actual_store != store_identity:
            _fail("output store", "directory inode changed before staging creation")
        os.mkdir(name, mode=0o700, dir_fd=store_descriptor)
        created = True
        created_identity = _directory_identity_at(
            store_descriptor,
            name,
            "staging",
        )
        staging_descriptor = os.open(name, flags, dir_fd=store_descriptor)
        staging_stat = os.fstat(staging_descriptor)
        if not stat.S_ISDIR(staging_stat.st_mode):
            _fail("staging", "new staging entry is not a directory")
        if not hasattr(os, "getuid") or staging_stat.st_uid != os.getuid():
            _fail("staging", "new staging directory is not owned by the current user")
        opened_identity = _identity_from_stat(staging_stat, "staging")
        if opened_identity != created_identity:
            _fail("staging", "directory inode changed while it was being opened")
        os.fchmod(staging_descriptor, 0o700)
        final_stat = os.fstat(staging_descriptor)
        if stat.S_IMODE(final_stat.st_mode) != 0o700:
            _fail("staging", "new staging directory does not have mode 0700")
        return _DirectoryIdentity(
            final_stat.st_dev,
            final_stat.st_ino,
            final_stat.st_uid,
        )
    except BaseException as original:
        if created:
            try:
                if created_identity is None:
                    raise PackCompilationError(
                        "staging setup failed before its inode could be captured; "
                        "refusing unsafe cleanup"
                    )
                _verify_directory_identity_at(
                    store_descriptor,
                    name,
                    created_identity,
                    "staging setup cleanup",
                )
                os.rmdir(name, dir_fd=store_descriptor)
                os.fsync(store_descriptor)
            except OSError as cleanup_error:
                raise PackCompilationError(
                    "staging setup failed and its empty directory could not be removed: "
                    f"{cleanup_error}"
                ) from original
        raise
    finally:
        if staging_descriptor >= 0:
            os.close(staging_descriptor)


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _stat_signature(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _read_stable(root: Path, relative: str, location: str) -> bytes:
    try:
        with contracts.open_regular_file(root, relative, location) as handle:
            before = os.fstat(handle.fileno())
            data = handle.read()
            after = os.fstat(handle.fileno())
    except contracts.VerificationError as exc:
        raise PackCompilationError(str(exc)) from exc
    if _stat_signature(before) != _stat_signature(after) or len(data) != before.st_size:
        _fail(location, "source changed while it was being read")
    return data


def _verify_planned_bytes(raw: bytes, ref: Mapping[str, Any], location: str) -> None:
    actual = (hashlib.sha256(raw).hexdigest(), len(raw))
    expected = (ref["sha256"], ref["bytes"])
    if actual != expected:
        _fail(
            location,
            "bytes do not match the approved source plan "
            f"(expected={expected[0]}/{expected[1]}, actual={actual[0]}/{actual[1]})",
        )


def _fingerprint_regular(root: Path, relative: str, location: str) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    try:
        with contracts.open_regular_file(root, relative, location) as handle:
            before = os.fstat(handle.fileno())
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
            after = os.fstat(handle.fileno())
    except contracts.VerificationError as exc:
        raise PackCompilationError(str(exc)) from exc
    if _stat_signature(before) != _stat_signature(after) or size != before.st_size:
        _fail(location, "source changed while it was being hashed")
    return digest.hexdigest(), size


def _ensure_parent(root: Path, relative: str) -> Path:
    current = root
    parts = PurePosixPath(relative).parts
    for part in parts[:-1]:
        current = current / part
        try:
            current.mkdir(mode=0o700)
            current.chmod(0o700)
        except FileExistsError:
            mode = current.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                _fail("destination", f"parent is not a real directory: {current}")
    return root.joinpath(*parts)


def _write_exclusive(path: Path, data: bytes, *, mode: int = 0o444) -> None:
    parent_mode = path.parent.lstat().st_mode
    if stat.S_ISLNK(parent_mode) or not stat.S_ISDIR(parent_mode):
        _fail("destination", f"parent is not a real directory: {path.parent}")
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            os.fchmod(handle.fileno(), 0o600)
            handle.write(data)
            handle.flush()
            os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _copy_stream_to_temp(
    source: BinaryIO,
    candidate: Path,
    *,
    after_copy: Callable[[], None] | None = None,
) -> tuple[Path, str, int]:
    temporary = candidate / f".object-{secrets.token_hex(12)}"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    digest = hashlib.sha256()
    size = 0
    try:
        with os.fdopen(descriptor, "wb") as destination:
            descriptor = -1
            while chunk := source.read(1024 * 1024):
                destination.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            if after_copy is not None:
                after_copy()
            destination.flush()
            os.fchmod(destination.fileno(), 0o444)
            os.fsync(destination.fileno())
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return temporary, digest.hexdigest(), size


def _install_object(
    candidate: Path,
    temporary: Path,
    digest: str,
    size: int,
) -> Path:
    relative = f"objects/sha256/{digest}"
    target = _ensure_parent(candidate, relative)
    try:
        os.link(temporary, target, follow_symlinks=False)
    except FileExistsError:
        if target.is_symlink() or not target.is_file():
            temporary.unlink(missing_ok=True)
            _fail("objects", f"content-addressed destination is not a file: {relative}")
        actual_digest, actual_size = contracts.digest_file(target)
        if (actual_digest, actual_size) != (digest, size):
            temporary.unlink(missing_ok=True)
            _fail("objects", f"content-addressed collision at {relative}")
        temporary.unlink()
    else:
        temporary.unlink()
    return target


def _copy_source_object(
    root: Path,
    relative: str,
    candidate: Path,
    location: str,
    *,
    expected_size: int,
    after_copy: Callable[[str], None] | None,
) -> tuple[str, int]:
    try:
        with contracts.open_regular_file(root, relative, location) as source:
            before = os.fstat(source.fileno())
            if before.st_size != expected_size:
                _fail(
                    location,
                    "source size does not match the approved source plan before copy "
                    f"(expected={expected_size}, actual={before.st_size})",
                )
            temporary, digest, size = _copy_stream_to_temp(
                source,
                candidate,
                after_copy=(lambda: after_copy(location)) if after_copy else None,
            )
            after = os.fstat(source.fileno())
    except contracts.VerificationError as exc:
        raise PackCompilationError(str(exc)) from exc
    if _stat_signature(before) != _stat_signature(after) or size != before.st_size:
        temporary.unlink(missing_ok=True)
        _fail(location, "source changed while it was being copied")
    _install_object(candidate, temporary, digest, size)
    return digest, size


def _copy_planned_file(
    root: Path,
    ref: Mapping[str, Any],
    candidate: Path,
    destination: str,
    location: str,
    *,
    after_copy: Callable[[str], None] | None,
) -> dict[str, Any]:
    """Copy one digest-bound plan file to an exact non-CAS pack path."""
    try:
        with contracts.open_regular_file(root, ref["path"], location) as source:
            before = os.fstat(source.fileno())
            if before.st_size != ref["bytes"]:
                _fail(
                    location,
                    "source size does not match the approved source plan before copy "
                    f"(expected={ref['bytes']}, actual={before.st_size})",
                )
            temporary, digest, size = _copy_stream_to_temp(
                source,
                candidate,
                after_copy=(lambda: after_copy(location)) if after_copy else None,
            )
            after = os.fstat(source.fileno())
    except contracts.VerificationError as exc:
        raise PackCompilationError(str(exc)) from exc
    if _stat_signature(before) != _stat_signature(after) or size != before.st_size:
        temporary.unlink(missing_ok=True)
        _fail(location, "source changed while it was being copied")
    if (digest, size) != (ref["sha256"], ref["bytes"]):
        temporary.unlink(missing_ok=True)
        _fail(
            location,
            "copied bytes do not match the approved source plan "
            f"(expected={ref['sha256']}/{ref['bytes']}, actual={digest}/{size})",
        )
    target = _ensure_parent(candidate, destination)
    try:
        os.link(temporary, target, follow_symlinks=False)
    except FileExistsError:
        temporary.unlink(missing_ok=True)
        _fail(location, f"pack destination already exists: {destination}")
    else:
        temporary.unlink()
    return _file_ref(destination, digest, size, ref["media_type"])


def _git_environment() -> dict[str, str]:
    return {
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": os.environ.get("PATH", ""),
    }


def _git_output(
    git: Path,
    repository: Path,
    arguments: Sequence[str],
    location: str,
) -> bytes:
    process = subprocess.run(
        [str(git), "-C", str(repository), *arguments],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_git_environment(),
        check=False,
    )
    if process.returncode != 0:
        detail = process.stderr.decode("utf-8", "replace").strip()[-500:]
        _fail(location, f"Git command failed ({process.returncode}): {detail}")
    return process.stdout


def _git_optional_output(
    git: Path,
    repository: Path,
    arguments: Sequence[str],
    location: str,
) -> bytes:
    process = subprocess.run(
        [str(git), "-C", str(repository), *arguments],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_git_environment(),
        check=False,
    )
    if process.returncode == 1:
        return b""
    if process.returncode != 0:
        detail = process.stderr.decode("utf-8", "replace").strip()[-500:]
        _fail(location, f"Git command failed ({process.returncode}): {detail}")
    return process.stdout


@dataclass(frozen=True, slots=True)
class _GitEntry:
    mode: str
    object_type: str
    oid: str


@dataclass(frozen=True, slots=True)
class _GitSnapshot:
    git: Path
    repository: Path
    commit: str
    tree: str
    entries: Mapping[str, _GitEntry]

    def regular_blob(self, relative: str, location: str) -> _GitEntry:
        entry = self.entries.get(relative)
        if entry is None:
            _fail(location, f"path is absent from pinned Git commit: {relative}")
        if entry.object_type != "blob" or entry.mode not in {"100644", "100755"}:
            _fail(location, f"Git path is not a regular blob: {relative}")
        return entry

    def enumerate_input(
        self,
        declaration: dict[str, Any],
        location: str,
    ) -> tuple[str, ...]:
        if declaration["cardinality"] == "one":
            relative = declaration["path"]
            entry = self.entries.get(relative)
            if entry is None:
                return ()
            self.regular_blob(relative, location)
            return (relative,)
        pattern = declaration["path_pattern"]
        matched: list[str] = []
        for relative in sorted(self.entries):
            if not contracts._matches_relative_pattern(relative, pattern):
                continue
            if unicodedata.normalize("NFC", relative) != relative:
                _fail(location, f"matched Git path is not Unicode NFC: {relative!r}")
            self.regular_blob(relative, location)
            matched.append(relative)
        return tuple(matched)

    def batch(self) -> _GitBatch:
        return _GitBatch(self)


class _GitBatch:
    def __init__(self, snapshot: _GitSnapshot) -> None:
        self.snapshot = snapshot
        self.process: subprocess.Popen[bytes] | None = None

    def __enter__(self) -> _GitBatch:
        try:
            self.process = subprocess.Popen(
                [
                    str(self.snapshot.git),
                    "-C",
                    str(self.snapshot.repository),
                    "cat-file",
                    "--batch",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=_git_environment(),
            )
        except OSError as exc:
            raise PackCompilationError(f"Git batch reader: cannot start: {exc}") from exc
        return self

    def __exit__(self, exc_type: object, _exc: object, _tb: object) -> None:
        process = self.process
        if process is None:
            return
        failure: PackCompilationError | None = None
        try:
            if exc_type is None:
                assert process.stdin is not None
                try:
                    process.stdin.close()
                except OSError as error:
                    failure = PackCompilationError(
                        f"Git batch reader: cannot close input: {error}"
                    )
                returncode = process.wait()
                if returncode != 0 and failure is None:
                    failure = PackCompilationError(
                        f"Git batch reader exited with status {returncode}"
                    )
            else:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                process.wait()
        finally:
            if process.stdin is not None:
                with contextlib.suppress(OSError):
                    process.stdin.close()
            if process.stdout is not None:
                with contextlib.suppress(OSError):
                    process.stdout.close()
        if failure is not None:
            raise failure

    def copy_blob(
        self,
        relative: str,
        destination: Path,
        location: str,
        *,
        expected_size: int | None = None,
    ) -> tuple[str, int]:
        process = self.process
        if process is None or process.stdin is None or process.stdout is None:
            _fail(location, "Git batch reader is not open")
        entry = self.snapshot.regular_blob(relative, location)
        try:
            process.stdin.write(entry.oid.encode("ascii") + b"\n")
            process.stdin.flush()
            header = process.stdout.readline(1024)
        except OSError as exc:
            raise PackCompilationError(f"{location}: Git batch request failed: {exc}") from exc
        if not header.endswith(b"\n"):
            _fail(location, "Git batch reader returned an unterminated header")
        try:
            response_oid, object_type, raw_size = header[:-1].decode("ascii").split(" ")
            declared_size = int(raw_size)
        except (UnicodeDecodeError, ValueError) as exc:
            raise PackCompilationError(
                f"{location}: malformed Git batch header {header[:200]!r}"
            ) from exc
        if response_oid != entry.oid or object_type != "blob" or declared_size < 0:
            _fail(location, "Git batch response does not match the indexed blob")
        if expected_size is not None and declared_size != expected_size:
            _fail(
                location,
                "Git blob size does not match the approved source plan before copy "
                f"(expected={expected_size}, actual={declared_size})",
            )

        parent_mode = destination.parent.lstat().st_mode
        if stat.S_ISLNK(parent_mode) or not stat.S_ISDIR(parent_mode):
            _fail("destination", f"parent is not a real directory: {destination.parent}")
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        digest = hashlib.sha256()
        remaining = declared_size
        try:
            with os.fdopen(descriptor, "wb") as target:
                descriptor = -1
                while remaining:
                    chunk = process.stdout.read(min(1024 * 1024, remaining))
                    if not chunk:
                        _fail(location, "Git batch reader ended inside blob payload")
                    target.write(chunk)
                    digest.update(chunk)
                    remaining -= len(chunk)
                if process.stdout.read(1) != b"\n":
                    _fail(location, "Git batch blob payload lacks its terminator")
                target.flush()
                os.fchmod(target.fileno(), 0o444)
                os.fsync(target.fileno())
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        return digest.hexdigest(), declared_size


def _load_git_snapshot(
    git: Path,
    repository: Path,
    commit: str,
    location: str,
) -> _GitSnapshot:
    partial_clone = _git_optional_output(
        git,
        repository,
        [
            "config",
            "--local",
            "--get-regexp",
            r"^remote\..*\.(promisor|partialclonefilter)$",
        ],
        location,
    )
    extension = _git_optional_output(
        git,
        repository,
        ["config", "--local", "--get", "extensions.partialClone"],
        location,
    )
    if partial_clone.strip() or extension.strip():
        _fail(location, "partial-clone repositories are forbidden for offline compilation")
    top = _git_output(git, repository, ["rev-parse", "--show-toplevel"], location)
    try:
        top_path = Path(top.decode("utf-8").strip()).resolve(strict=True)
    except (UnicodeDecodeError, OSError) as exc:
        raise PackCompilationError(f"{location}: invalid Git worktree root") from exc
    if top_path != repository:
        _fail(location, f"root is inside Git worktree {top_path}, not its top level")
    resolved_commit = _git_output(
        git, repository, ["rev-parse", "--verify", f"{commit}^{{commit}}"], location
    ).decode("ascii", "strict").strip()
    if resolved_commit != commit:
        _fail(location, f"Git resolved commit to {resolved_commit!r}")
    tree = _git_output(
        git, repository, ["rev-parse", "--verify", f"{commit}^{{tree}}"], location
    ).decode("ascii", "strict").strip()
    tree = _git_commit(tree, f"{location}.tree")
    raw = _git_output(
        git,
        repository,
        ["ls-tree", "-r", "-z", "--full-tree", commit],
        location,
    )
    records = [record for record in raw.split(b"\0") if record]
    entries: dict[str, _GitEntry] = {}
    for record in records:
        try:
            header, encoded_path = record.split(b"\t", 1)
            mode, object_type, oid = header.decode("ascii").split(" ")
            relative = encoded_path.decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise PackCompilationError(
                f"{location}: malformed recursive git ls-tree output"
            ) from exc
        if not re.fullmatch(r"[0-9a-f]{40}", oid):
            _fail(location, f"invalid object ID for Git path {relative!r}")
        if relative in entries:
            _fail(location, f"duplicate Git tree path {relative!r}")
        entries[relative] = _GitEntry(mode, object_type, oid)
    return _GitSnapshot(git, repository, commit, tree, entries)


def _copy_git_object(
    batch: _GitBatch,
    relative: str,
    candidate: Path,
    location: str,
    *,
    expected_size: int,
) -> tuple[str, int]:
    temporary = candidate / f".object-{secrets.token_hex(12)}"
    digest, size = batch.copy_blob(
        relative,
        temporary,
        location,
        expected_size=expected_size,
    )
    _install_object(candidate, temporary, digest, size)
    return digest, size


def _glob_prefix(pattern: str) -> str:
    prefix: list[str] = []
    for part in PurePosixPath(pattern).parts:
        if any(character in part for character in "*?[]{}"):
            break
        prefix.append(part)
    return "/".join(prefix)


def _lstat_beneath(
    root: Path,
    relative: str,
    location: str,
) -> os.stat_result | None:
    current = root
    parts = PurePosixPath(relative).parts
    for index, part in enumerate(parts):
        current = current / part
        try:
            result = current.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise PackCompilationError(
                f"{location}: cannot inspect {relative}: {exc}"
            ) from exc
        if stat.S_ISLNK(result.st_mode):
            _fail(location, f"source path contains a symlink: {current.relative_to(root)}")
        if index < len(parts) - 1 and not stat.S_ISDIR(result.st_mode):
            _fail(
                location,
                f"source path parent is not a directory: {current.relative_to(root)}",
            )
    return result


def _walk_strict(
    root: Path,
    *,
    topdown: bool,
    location: str,
) -> Iterator[tuple[str, list[str], list[str]]]:
    def onerror(error: OSError) -> None:
        raise PackCompilationError(f"{location}: directory traversal failed: {error}")

    yield from os.walk(
        root,
        topdown=topdown,
        followlinks=False,
        onerror=onerror,
    )


def _enumerate_pattern(root: Path, pattern: str, location: str) -> tuple[str, ...]:
    prefix = _glob_prefix(pattern)
    start = root.joinpath(*PurePosixPath(prefix).parts) if prefix else root
    if prefix:
        prefix_stat = _lstat_beneath(root, prefix, location)
        if prefix_stat is None:
            return ()
        if not stat.S_ISDIR(prefix_stat.st_mode):
            _fail(location, f"glob prefix is not a directory: {prefix}")
    matches: list[str] = []
    for directory, names, filenames in _walk_strict(
        start,
        topdown=True,
        location=location,
    ):
        names.sort()
        filenames.sort()
        directory_path = Path(directory)
        for name in names:
            child = directory_path / name
            mode = child.lstat().st_mode
            if stat.S_ISLNK(mode):
                _fail(location, f"source tree contains a symlink: {child.relative_to(root)}")
            if not stat.S_ISDIR(mode):
                _fail(location, f"source tree contains a non-directory: {child.relative_to(root)}")
        for name in filenames:
            child = directory_path / name
            relative = child.relative_to(root).as_posix()
            if not contracts._matches_relative_pattern(relative, pattern):
                continue
            mode = child.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                _fail(location, f"matched source is not a regular file: {relative}")
            matches.append(relative)
    return tuple(sorted(matches))


def _enumerate_input(root: Path, declaration: dict[str, Any], location: str) -> tuple[str, ...]:
    if declaration["cardinality"] == "one":
        relative = declaration["path"]
        source_stat = _lstat_beneath(root, relative, location)
        if source_stat is None:
            return ()
        if not stat.S_ISREG(source_stat.st_mode):
            _fail(location, f"declared source is not a regular file: {relative}")
        return (relative,)
    return _enumerate_pattern(root, declaration["path_pattern"], location)


def _file_ref(path: str, digest: str, size: int, media_type: str) -> dict[str, Any]:
    return {
        "bytes": size,
        "media_type": media_type,
        "path": path,
        "sha256": digest,
    }


def _write_document(candidate: Path, relative: str, value: Any) -> dict[str, Any]:
    raw = contracts.canonical_json_bytes(value)
    return _write_document_bytes(candidate, relative, raw, "application/json")


def _write_document_bytes(
    candidate: Path,
    relative: str,
    raw: bytes,
    media_type: str,
) -> dict[str, Any]:
    target = _ensure_parent(candidate, relative)
    _write_exclusive(target, raw)
    return _file_ref(
        relative,
        hashlib.sha256(raw).hexdigest(),
        len(raw),
        media_type,
    )


def _reject_path_collisions(
    paths: Sequence[str],
    location: str = "pack paths",
) -> None:
    """Mirror replay preparation's portable alias and ancestry rules."""
    terminal = object()
    trie: dict[object, Any] = {}
    for logical in sorted(paths):
        node = trie
        for part in PurePosixPath(logical).parts:
            if terminal in node:
                _fail(location, f"destination ancestry collision at {logical!r}")
            portable_part = unicodedata.normalize("NFC", part).casefold()
            existing = node.get(portable_part)
            if existing is None:
                child: dict[object, Any] = {}
                node[portable_part] = (part, child)
            else:
                existing_part, child = existing
                if existing_part != part:
                    _fail(
                        location,
                        "destination component aliases an existing portable "
                        f"name at {logical!r}",
                    )
            node = child
        if terminal in node or node:
            _fail(location, f"portable destination collision at {logical!r}")
        node[terminal] = True


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_tree(root: Path) -> None:
    for directory, _names, _files in _walk_strict(
        root,
        topdown=False,
        location="pack fsync",
    ):
        _fsync_directory(Path(directory))


def _make_read_only(root: Path) -> None:
    for directory, names, filenames in _walk_strict(
        root,
        topdown=False,
        location="pack sealing",
    ):
        directory_path = Path(directory)
        for name in filenames:
            path = directory_path / name
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                _fail("pack", f"contains a non-regular file: {path.relative_to(root)}")
            path.chmod(0o444)
        for name in names:
            path = directory_path / name
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                _fail("pack", f"contains a non-directory: {path.relative_to(root)}")
            path.chmod(0o555)
    root.chmod(0o555)


def _verify_read_only_tree(root: Path) -> None:
    root_mode = root.lstat().st_mode
    if not stat.S_ISDIR(root_mode) or root_mode & 0o222:
        _fail("existing pack", "root must be a read-only directory")
    for directory, names, filenames in _walk_strict(
        root,
        topdown=True,
        location="existing pack",
    ):
        directory_path = Path(directory)
        for name in [*names, *filenames]:
            path = directory_path / name
            mode = path.lstat().st_mode
            relative = path.relative_to(root).as_posix()
            if stat.S_ISLNK(mode):
                _fail("existing pack", f"contains a symlink: {relative}")
            if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                _fail("existing pack", f"contains a non-regular entry: {relative}")
            if mode & 0o222:
                _fail("existing pack", f"contains a writable entry: {relative}")


def _remove_directory_contents(descriptor: int, location: str) -> None:
    try:
        names = sorted(os.listdir(descriptor))
    except OSError as exc:
        raise PackCompilationError(f"{location}: cannot enumerate directory: {exc}") from exc
    for name in names:
        child_location = f"{location}/{name}"
        try:
            before = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        except OSError as exc:
            raise PackCompilationError(f"{child_location}: cannot inspect entry: {exc}") from exc
        if not hasattr(os, "getuid") or before.st_uid != os.getuid():
            _fail(child_location, "refusing to remove an entry not owned by the current user")
        if stat.S_ISDIR(before.st_mode):
            flags = (
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            try:
                child_descriptor = os.open(name, flags, dir_fd=descriptor)
            except OSError as exc:
                raise PackCompilationError(
                    f"{child_location}: cannot open directory for cleanup: {exc}"
                ) from exc
            try:
                opened = os.fstat(child_descriptor)
                expected = _identity_from_stat(opened, child_location)
                observed = _identity_from_stat(before, child_location)
                if opened.st_mode != before.st_mode or expected != observed:
                    _fail(child_location, "entry changed while it was being opened")
                os.fchmod(child_descriptor, 0o700)
                _remove_directory_contents(child_descriptor, child_location)
                _verify_directory_identity_at(
                    descriptor,
                    name,
                    expected,
                    child_location,
                )
                os.rmdir(name, dir_fd=descriptor)
            finally:
                os.close(child_descriptor)
        elif stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
            try:
                after = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            except OSError as exc:
                raise PackCompilationError(
                    f"{child_location}: cannot re-inspect entry: {exc}"
                ) from exc
            if (after.st_dev, after.st_ino, after.st_uid, stat.S_IFMT(after.st_mode)) != (
                before.st_dev,
                before.st_ino,
                before.st_uid,
                stat.S_IFMT(before.st_mode),
            ):
                _fail(child_location, "entry changed during cleanup")
            os.unlink(name, dir_fd=descriptor)
        else:
            _fail(child_location, "refusing to remove a non-regular entry")


def _remove_tree_at(
    parent_descriptor: int,
    name: str,
    expected: _DirectoryIdentity,
) -> None:
    if not hasattr(os, "getuid") or expected.owner != os.getuid():
        _fail("cleanup", "refusing to remove staging not owned by the current user")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise PackCompilationError(f"cleanup: cannot open staging directory: {exc}") from exc
    try:
        opened = _identity_from_stat(os.fstat(descriptor), "cleanup")
        if opened != expected:
            _fail("cleanup", "directory inode or ownership changed")
        os.fchmod(descriptor, 0o700)
        _remove_directory_contents(descriptor, "cleanup")
        _verify_directory_identity_at(parent_descriptor, name, expected, "cleanup")
        os.rmdir(name, dir_fd=parent_descriptor)
        os.fsync(parent_descriptor)
    finally:
        os.close(descriptor)


def _remove_tree(root: Path, expected: _DirectoryIdentity) -> None:
    parent = _real_directory(root.parent, "cleanup parent")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(parent, flags)
    try:
        _remove_tree_at(descriptor, root.name, expected)
    finally:
        os.close(descriptor)


def _publish_no_replace(
    store_descriptor: int,
    staging_name: str,
    target_name: str,
) -> None:
    if (
        not staging_name
        or not target_name
        or "/" in staging_name
        or "/" in target_name
        or staging_name in {".", ".."}
        or target_name in {".", ".."}
    ):
        _fail("publication", "source and target must be single path components")
    source_bytes = os.fsencode(staging_name)
    target_bytes = os.fsencode(target_name)
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin" and hasattr(library, "renameatx_np"):
        rename = library.renameatx_np
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        result = rename(
            store_descriptor,
            source_bytes,
            store_descriptor,
            target_bytes,
            0x00000004,
        )
    elif sys.platform.startswith("linux") and hasattr(library, "renameat2"):
        rename = library.renameat2
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        result = rename(
            store_descriptor,
            source_bytes,
            store_descriptor,
            target_bytes,
            0x00000001,
        )
    else:
        _fail(
            "publication",
            "platform lacks descriptor-relative atomic no-replace directory rename",
        )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise _DestinationExists(f"pack destination already exists: {target_name}")
    raise OSError(error_number, os.strerror(error_number), target_name)


def _tree_paths(root: Path) -> set[str]:
    result: set[str] = set()
    for directory, names, filenames in _walk_strict(
        root,
        topdown=True,
        location="pack",
    ):
        names.sort()
        filenames.sort()
        directory_path = Path(directory)
        for name in names:
            path = directory_path / name
            relative = path.relative_to(root).as_posix()
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                _fail("pack", f"contains a non-directory: {relative}")
        for name in filenames:
            path = directory_path / name
            relative = path.relative_to(root).as_posix()
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                _fail("pack", f"contains a non-regular file: {relative}")
            result.add(relative)
    return result


def _verify_existing_pack(
    root: Path,
    expected_id: str,
    expected_fingerprints: Mapping[str, tuple[str, int]],
    *,
    expected_schema: str = contracts.PACK_SCHEMA_V2,
) -> dict[str, int]:
    if root.is_symlink() or not root.is_dir():
        _fail("existing pack", f"destination is not a real directory: {root}")
    manifest_path = root / "offline-pack.json"
    try:
        document, _ = contracts.load_canonical_json(manifest_path)
        pack = contracts.validate_offline_pack(document)
        if pack.get("schema") != expected_schema:
            expected_label = (
                "offline-pack/v2"
                if expected_schema == contracts.PACK_SCHEMA_V2
                else "offline-pack/v3"
            )
            _fail("existing pack", f"destination is not {expected_label}")
        if pack["offline_pack_id"] != expected_id:
            _fail("existing pack", "manifest identity does not match destination")
        counts = contracts.verify_offline_pack_files(
            pack, root, manifest_path=manifest_path
        )
    except (OSError, contracts.VerificationError) as exc:
        raise PackCompilationError(f"existing pack verification failed: {exc}") from exc
    if _tree_paths(root) != set(expected_fingerprints):
        _fail("existing pack", "same-ID directory does not have the candidate file set")
    expected_manifest = expected_fingerprints.get("offline-pack.json")
    if expected_manifest is None or contracts.digest_file(manifest_path) != expected_manifest:
        _fail("existing pack", "same-ID manifest is not byte-identical to candidate")
    _verify_read_only_tree(root)
    return counts


def _verify_existing_pack_at(
    store: Path,
    store_descriptor: int,
    store_identity: _DirectoryIdentity,
    name: str,
    expected_id: str,
    expected_fingerprints: Mapping[str, tuple[str, int]],
    *,
    expected_schema: str = contracts.PACK_SCHEMA_V2,
) -> tuple[dict[str, int], _DirectoryIdentity]:
    _verify_store_descriptor(store_descriptor, store_identity)
    _verify_directory_identity(store, store_identity, "output store")
    identity = _directory_identity_at(store_descriptor, name, "existing pack")
    assert identity is not None
    if not hasattr(os, "getuid") or identity.owner != os.getuid():
        _fail("existing pack", "must be owned by the current user")
    root = store / name
    if expected_schema == contracts.PACK_SCHEMA_V2:
        counts = _verify_existing_pack(
            root,
            expected_id,
            expected_fingerprints,
        )
    else:
        counts = _verify_existing_pack(
            root,
            expected_id,
            expected_fingerprints,
            expected_schema=expected_schema,
        )
    _verify_store_descriptor(store_descriptor, store_identity)
    _verify_directory_identity(store, store_identity, "output store")
    _verify_directory_identity_at(store_descriptor, name, identity, "existing pack")
    _verify_directory_identity(root, identity, "existing pack")
    return counts, identity


def _resolve_git_executable(value: str | os.PathLike[str]) -> Path:
    candidate = os.fspath(value)
    located = shutil.which(candidate) if not os.path.isabs(candidate) else candidate
    if not located:
        _fail("git", f"executable not found: {candidate}")
    return _real_file(located, "git")


def compile_offline_pack_v2(
    source_plan_path: str | os.PathLike[str],
    inventory_path: str | os.PathLike[str],
    output_store: str | os.PathLike[str],
    *,
    roots: Mapping[str, str | os.PathLike[str]],
    git_executable: str | os.PathLike[str] = "git",
    after_copy: Callable[[str], None] | None = None,
    before_seal: Callable[[], None] | None = None,
) -> CompiledPack:
    """Compile and atomically publish one verified offline pack.

    ``after_copy`` and ``before_seal`` are deterministic fault-injection seams
    for tests.  Production callers should leave them unset.
    """
    plan_path = _real_file(source_plan_path, "source plan")
    inventory_file = _real_file(inventory_path, "inventory")
    plan, _ = load_source_plan(plan_path)
    is_v3 = plan["schema"] == SOURCE_PLAN_SCHEMA_V3
    source_plan_contracts = None
    expected_v3_manifests: dict[str, dict[str, Any]] = {}
    if is_v3:
        import source_plan_contracts as source_plan_contracts_module

        source_plan_contracts = source_plan_contracts_module
    try:
        inventory, _ = contracts.load_canonical_json(inventory_file)
        contracts.validate_reducer_input_inventory(inventory)
    except (OSError, contracts.VerificationError) as exc:
        raise PackCompilationError(f"inventory: {exc}") from exc
    if inventory["inventory_id"] != plan["inventory_id"]:
        _fail("$.inventory_id", "does not match the verified reducer inventory")

    required_roots = {entry["id"] for entry in inventory["roots"]}
    required_roots.add(plan["reducer"]["root"])
    required_roots.add(plan["configuration"]["root"])
    required_roots.add(plan["environment"]["root"])
    required_roots.update(item["root"] for item in plan["schemas"])
    required_roots.update(
        item["root"]
        for source in plan["sources"]
        for item in source["objects"]
    )
    if is_v3:
        required_roots.update(
            ref["root"]
            for source in plan["sources"]
            if "evidence" in source
            for ref in (
                *source["evidence"]["acquisition_receipts"],
                source["evidence"]["normalization_lineage"],
                *source["evidence"]["request_parameter_preimages"],
            )
        )
    if set(roots) != required_roots:
        _fail(
            "roots",
            "root bindings must exactly match the plan and inventory "
            f"(missing={sorted(required_roots - set(roots))}, "
            f"unknown={sorted(set(roots) - required_roots)})",
        )
    resolved_roots = {
        name: _real_directory(path, f"roots.{name}") for name, path in roots.items()
    }
    root_identities = {
        name: _directory_identity(root, f"roots.{name}")
        for name, root in resolved_roots.items()
    }
    evidence_source_refs: dict[
        tuple[str, str], tuple[dict[str, Any], str]
    ] = {}
    if is_v3:
        assert source_plan_contracts is not None
        try:
            source_plan_contracts.verify_source_plan_v3_evidence(
                plan,
                resolved_roots,
            )
            expected_v3_manifests = {
                manifest["source"]: manifest
                for manifest in source_plan_contracts.source_manifests_from_plan_v3(
                    plan
                ).values()
            }
        except contracts.VerificationError as exc:
            raise PackCompilationError(f"source plan evidence: {exc}") from exc
        for source_index, source in enumerate(plan["sources"]):
            if "evidence" not in source:
                continue
            evidence = source["evidence"]
            groups = (
                ("acquisition_receipt", evidence["acquisition_receipts"]),
                ("normalization_lineage", [evidence["normalization_lineage"]]),
                (
                    "request_parameter_preimage",
                    evidence["request_parameter_preimages"],
                ),
            )
            for kind, refs in groups:
                for evidence_index, ref in enumerate(refs):
                    key = (kind, (
                        ref.get("acquisition_receipt_id")
                        or ref.get("normalization_lineage_id")
                        or ref["parameters_sha256"]
                    ))
                    location = (
                        f"$.sources[{source_index}].evidence.{kind}"
                        f"[{evidence_index}]"
                    )
                    evidence_source_refs.setdefault(key, (ref, location))
    store_path, store_exists = _resolve_output_store(output_store)
    for name, root in resolved_roots.items():
        if _paths_overlap(store_path, root):
            _fail("output store", f"overlaps source root {name!r}")

    git = _resolve_git_executable(git_executable)
    git_snapshots: dict[tuple[Path, str], _GitSnapshot] = {}

    def snapshot_for(root: Path, commit: str, location: str) -> _GitSnapshot:
        key = (root, commit)
        snapshot = git_snapshots.get(key)
        if snapshot is None:
            snapshot = _load_git_snapshot(git, root, commit, location)
            git_snapshots[key] = snapshot
        return snapshot

    reducer = plan["reducer"]
    reducer_repo = resolved_roots[reducer["root"]]
    reducer_snapshot = snapshot_for(
        reducer_repo,
        reducer["git_commit"],
        "$.reducer.git_commit",
    )
    if reducer["entrypoint"] not in inventory["scope"]:
        _fail("$.reducer.entrypoint", "must name one file in inventory.scope")

    declarations = {item["id"]: item for item in inventory["inputs"]}
    planned_bindings = {item["input_id"]: item for item in plan["input_bindings"]}
    if set(declarations) != set(planned_bindings):
        _fail(
            "$.input_bindings",
            "must name every inventory input exactly once "
            f"(missing={sorted(set(declarations) - set(planned_bindings))}, "
            f"unknown={sorted(set(planned_bindings) - set(declarations))})",
        )

    source_by_name = {source["source"]: source for source in plan["sources"]}
    source_objects = {
        (source["source"], item["name"]): item
        for source in plan["sources"]
        for item in source["objects"]
    }
    curated_snapshots: dict[str, _GitSnapshot] = {}
    for source_index, source in enumerate(plan["sources"]):
        if source["source_kind"] != "curated_git_tree":
            continue
        pin = source["pin"]
        object_roots = {item["root"] for item in source["objects"]}
        if len(object_roots) != 1:
            _fail(
                f"$.sources[{source_index}].objects",
                "curated_git_tree objects must share exactly one Git root",
            )
        root_name = next(iter(object_roots))
        snapshot = snapshot_for(
            resolved_roots[root_name],
            pin["value"],
            f"$.sources[{source_index}].pin",
        )
        if snapshot.tree != pin["tree"]:
            _fail(
                f"$.sources[{source_index}].pin.tree",
                f"expected {pin['tree']}, found {snapshot.tree}",
            )
        curated_snapshots[source["source"]] = snapshot

    # Presence is explicit and checked before any output staging begins.
    mutable_input_paths: dict[str, tuple[str, ...]] = {}
    logical_paths_by_root: dict[str, list[str]] = {}
    for input_id in sorted(declarations):
        declaration = declarations[input_id]
        binding = planned_bindings[input_id]
        location = f"$.input_bindings[{input_id!r}]"
        binding_sources = [source_by_name[name] for name in binding["sources"]]
        if declaration["class"] == "curated_git_input":
            if len(binding_sources) != 1:
                _fail(location, "curated Git inputs require exactly one binding source")
            source = binding_sources[0]
            if source["source_kind"] != "curated_git_tree":
                _fail(location, "curated Git inputs require a curated_git_tree source")
            source_roots = {item["root"] for item in source["objects"]}
            if source_roots != {declaration["root"]}:
                _fail(
                    location,
                    "curated binding source must use its inventory declaration root",
                )
            snapshot = curated_snapshots[source["source"]]
            actual_paths = snapshot.enumerate_input(declaration, location)
        else:
            actual_paths = _enumerate_input(
                resolved_roots[declaration["root"]],
                declaration,
                location,
            )
            mutable_input_paths[input_id] = actual_paths
        planned_paths = tuple(member["path"] for member in binding["members"])
        if actual_paths != planned_paths:
            _fail(
                location,
                "declared member set does not equal the source root "
                f"(planned={list(planned_paths)}, actual={list(actual_paths)})",
            )
        if declaration["requirement"] == "required" and binding["state"] != "present":
            _fail(f"$.input_bindings[{input_id!r}].state", "required input must be present")
        expected_state = "present" if actual_paths else "absent"
        if binding["state"] != expected_state:
            _fail(
                f"$.input_bindings[{input_id!r}].state",
                f"expected {expected_state!r} for the enumerated member set",
            )
        if declaration["cardinality"] == "one" and actual_paths and len(actual_paths) != 1:
            _fail(location, "cardinality one requires one member")
        logical_paths_by_root.setdefault(declaration["root"], []).extend(actual_paths)
        for member_index, member in enumerate(binding["members"]):
            member_location = f"{location}.members[{member_index}]"
            source_object = source_objects[(member["source"], member["object"])]
            if declaration["class"] == "curated_git_input":
                source = binding_sources[0]
                snapshot = curated_snapshots[source["source"]]
                logical_entry = snapshot.regular_blob(member["path"], member_location)
                object_entry = snapshot.regular_blob(
                    source_object["path"], member_location
                )
                if logical_entry.oid != object_entry.oid:
                    _fail(
                        member_location,
                        "logical Git input does not equal the named pinned object",
                    )
    for root_name, paths in logical_paths_by_root.items():
        _reject_path_collisions(paths, f"logical inputs for root {root_name!r}")

    # Validate the small reducer closure before any potentially large source
    # object is copied.  The exact bytes validated here are the bytes packed.
    configuration_raw = _read_stable(
        resolved_roots[plan["configuration"]["root"]],
        plan["configuration"]["path"],
        "$.configuration",
    )
    _verify_planned_bytes(configuration_raw, plan["configuration"], "$.configuration")
    try:
        configuration_document = contracts.parse_json_bytes(
            configuration_raw, location=plan["configuration"]["path"]
        )
        configuration = build_context.ReducerConfiguration.from_document(
            configuration_document
        )
    except (contracts.VerificationError, build_context.BuildContextError) as exc:
        raise PackCompilationError(f"$.configuration: {exc}") from exc
    if configuration_raw != build_context.canonical_json_bytes(
        configuration.to_document()
    ):
        _fail("$.configuration", "must be exact canonical reducer configuration bytes")

    environment_raw = _read_stable(
        resolved_roots[plan["environment"]["root"]],
        plan["environment"]["path"],
        "$.environment",
    )
    _verify_planned_bytes(environment_raw, plan["environment"], "$.environment")
    try:
        environment_document = contracts.parse_json_bytes(
            environment_raw, location=plan["environment"]["path"]
        )
        contracts.validate_execution_environment(environment_document)
    except contracts.VerificationError as exc:
        raise PackCompilationError(f"$.environment: {exc}") from exc
    if environment_raw != contracts.canonical_json_bytes(environment_document):
        _fail("$.environment", "must be canonical-json-v1 bytes")
    if environment_document["runner"]["git_commit"] != reducer["git_commit"]:
        _fail("$.environment.runner.git_commit", "must equal the reducer Git commit")

    schema_bytes = [
        _read_stable(
            resolved_roots[schema["root"]],
            schema["path"],
            f"$.schemas[{index}]",
        )
        for index, schema in enumerate(plan["schemas"])
    ]
    for index, (schema, raw) in enumerate(zip(plan["schemas"], schema_bytes)):
        _verify_planned_bytes(raw, schema, f"$.schemas[{index}]")

    if store_exists:
        store = store_path
        store_identity = _validate_private_store(store)
    else:
        store, store_identity = _create_output_store(store_path)
    staging = store / f".offline-pack-{secrets.token_hex(12)}"
    staging_identity: _DirectoryIdentity | None = None
    store_descriptor = -1
    published = False
    try:
        store_descriptor = _open_private_store(store, store_identity)
        staging_identity = _create_private_staging(
            store_descriptor,
            store_identity,
            staging.name,
        )
        assert staging_identity is not None
        inventory_ref = _write_document(
            staging,
            "inventory/reducer-inputs-v2.json",
            inventory,
        )
        inventory_ref["inventory_id"] = inventory["inventory_id"]

        acquisition_receipt_refs: dict[str, dict[str, Any]] = {}
        normalization_lineage_refs: dict[str, dict[str, Any]] = {}
        request_preimage_refs: dict[str, dict[str, Any]] = {}
        evidence_fingerprints: dict[
            tuple[str, str], tuple[str, int]
        ] = {}
        if is_v3:
            for (kind, identity), (ref, location) in sorted(
                evidence_source_refs.items()
            ):
                if kind == "acquisition_receipt":
                    destination = (
                        "evidence/acquisition-receipts/"
                        + identity.removeprefix("sha256:")
                        + ".json"
                    )
                elif kind == "normalization_lineage":
                    destination = (
                        "evidence/normalization-lineages/"
                        + identity.removeprefix("sha256:")
                        + ".json"
                    )
                else:
                    destination = f"evidence/request-parameters/sha256/{identity}"
                copied = _copy_planned_file(
                    resolved_roots[ref["root"]],
                    ref,
                    staging,
                    destination,
                    location,
                    after_copy=after_copy,
                )
                evidence_fingerprints[(ref["root"], ref["path"])] = (
                    ref["sha256"],
                    ref["bytes"],
                )
                if kind == "acquisition_receipt":
                    copied["acquisition_receipt_id"] = identity
                    acquisition_receipt_refs[identity] = copied
                elif kind == "normalization_lineage":
                    copied["normalization_lineage_id"] = identity
                    normalization_lineage_refs[identity] = copied
                else:
                    copied["parameters_sha256"] = identity
                    request_preimage_refs[identity] = copied

        object_refs: dict[str, dict[str, Any]] = {}
        materialized_objects: dict[tuple[str, str], dict[str, Any]] = {}
        source_manifests: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for source_index, source in enumerate(plan["sources"]):
            finalized_objects: list[dict[str, Any]] = []
            batch_context = (
                curated_snapshots[source["source"]].batch()
                if source["source_kind"] == "curated_git_tree"
                else contextlib.nullcontext(None)
            )
            with batch_context as batch:
                for object_index, item in enumerate(source["objects"]):
                    location = f"$.sources[{source_index}].objects[{object_index}]"
                    physical_root = resolved_roots[item["root"]]
                    if source["source_kind"] == "curated_git_tree":
                        assert isinstance(batch, _GitBatch)
                        digest, size = _copy_git_object(
                            batch,
                            item["path"],
                            staging,
                            location,
                            expected_size=item["bytes"],
                        )
                    else:
                        digest, size = _copy_source_object(
                            physical_root,
                            item["path"],
                            staging,
                            location,
                            expected_size=item["bytes"],
                            after_copy=after_copy,
                        )
                    if (digest, size) != (item["sha256"], item["bytes"]):
                        _fail(
                            location,
                            "copied bytes do not match the approved source plan "
                            f"(expected={item['sha256']}/{item['bytes']}, "
                            f"actual={digest}/{size})",
                        )
                    object_path = f"objects/sha256/{digest}"
                    existing = object_refs.get(object_path)
                    pack_ref = _file_ref(object_path, digest, size, item["media_type"])
                    if existing is not None and existing != pack_ref:
                        _fail(
                            location,
                            "equal content is declared with incompatible size or media type",
                        )
                    object_refs[object_path] = pack_ref
                    finalized = {
                        "bytes": size,
                        "media_type": item["media_type"],
                        "name": item["name"],
                        "path": object_path,
                        "redistribution": item["redistribution"],
                        "roles": item["roles"],
                        "sha256": digest,
                    }
                    finalized_objects.append(finalized)
                    materialized_objects[(source["source"], item["name"])] = finalized

            manifest: dict[str, Any] = {
                "acquisition": copy.deepcopy(source["acquisition"]),
                "license": copy.deepcopy(source["license"]),
                "normalization": copy.deepcopy(source["normalization"]),
                "objects": finalized_objects,
                "pin": copy.deepcopy(source["pin"]),
                "schema": (
                    contracts.SOURCE_SCHEMA_V3
                    if is_v3
                    else contracts.SOURCE_SCHEMA_V2
                ),
                "source": source["source"],
                "source_kind": source["source_kind"],
                "source_manifest_id": ZERO_HASH,
            }
            for optional_key in (
                "previous_source_manifest_id",
                "review",
                "audit",
            ):
                if optional_key in source:
                    manifest[optional_key] = copy.deepcopy(source[optional_key])
            if is_v3 and "evidence" in source:
                manifest["evidence"] = copy.deepcopy(
                    expected_v3_manifests[source["source"]]["evidence"]
                )
            manifest["source_manifest_id"] = contracts.source_manifest_identity(manifest)
            try:
                contracts.validate_source_manifest(manifest)
            except contracts.VerificationError as exc:
                raise PackCompilationError(
                    f"$.sources[{source_index}]: generated source manifest failed "
                    f"validation: {exc}"
                ) from exc
            if is_v3 and manifest != expected_v3_manifests[source["source"]]:
                _fail(
                    f"$.sources[{source_index}]",
                    "materialized source manifest differs from the validated v3 plan projection",
                )
            manifest_path = (
                "manifests/"
                + manifest["source_manifest_id"].removeprefix("sha256:")
                + ".json"
            )
            manifest_ref = _write_document(staging, manifest_path, manifest)
            manifest_ref["source_manifest_id"] = manifest["source_manifest_id"]
            source_manifests.append((manifest, manifest_ref))

        source_manifests.sort(key=lambda item: item[0]["source_manifest_id"])
        manifest_ids = {
            manifest["source"]: manifest["source_manifest_id"]
            for manifest, _ref in source_manifests
        }

        input_bindings: list[dict[str, Any]] = []
        logical_fingerprints: dict[tuple[str, str], tuple[str, int]] = {}
        for input_id in sorted(declarations):
            declaration = declarations[input_id]
            planned = planned_bindings[input_id]
            members: list[dict[str, str]] = []
            for member_index, member in enumerate(planned["members"]):
                location = f"$.input_bindings[{input_id!r}].members[{member_index}]"
                source_object = materialized_objects[(member["source"], member["object"])]
                logical_key = (declaration["root"], member["path"])
                if (
                    declaration["class"] != "curated_git_input"
                    and logical_key not in logical_fingerprints
                ):
                    logical_fingerprints[logical_key] = _fingerprint_regular(
                        resolved_roots[declaration["root"]],
                        member["path"],
                        location,
                    )
                if (
                    declaration["class"] != "curated_git_input"
                    and logical_fingerprints[logical_key]
                    != (source_object["sha256"], source_object["bytes"])
                ):
                    _fail(location, "logical input bytes do not equal the named source object")
                members.append(
                    {
                        "object": member["object"],
                        "path": member["path"],
                        "source_manifest_id": manifest_ids[member["source"]],
                    }
                )
            input_bindings.append(
                {
                    "input_id": input_id,
                    "members": members,
                    "source_manifest_ids": sorted(
                        manifest_ids[source_name]
                        for source_name in planned["sources"]
                    ),
                    "state": planned["state"],
                }
            )

        reducer_files: list[dict[str, Any]] = []
        with reducer_snapshot.batch() as reducer_batch:
            for index, logical_path in enumerate(inventory["scope"]):
                pack_path = f"reducer/{logical_path}"
                target = _ensure_parent(staging, pack_path)
                digest, size = reducer_batch.copy_blob(
                    logical_path,
                    target,
                    f"$.reducer.files[{index}]",
                )
                reducer_files.append(
                    {
                        "logical_path": logical_path,
                        **_file_ref(
                            pack_path,
                            digest,
                            size,
                            (
                                "text/x-python"
                                if logical_path.endswith(".py")
                                else "application/octet-stream"
                            ),
                        ),
                    }
                )

        configuration_ref = _write_document_bytes(
            staging,
            "configuration/reducer.json",
            configuration_raw,
            "application/json",
        )
        environment_ref = _write_document_bytes(
            staging,
            "environment/execution-environment.json",
            environment_raw,
            "application/json",
        )

        schema_refs: list[dict[str, Any]] = []
        for index, (schema, raw) in enumerate(zip(plan["schemas"], schema_bytes)):
            ref = _write_document_bytes(
                staging,
                schema["pack_path"],
                raw,
                schema["media_type"],
            )
            schema_refs.append(ref)

        packed_evidence_refs = [
            *acquisition_receipt_refs.values(),
            *normalization_lineage_refs.values(),
            *request_preimage_refs.values(),
        ]
        all_pack_paths = [
            "inventory/reducer-inputs-v2.json",
            "configuration/reducer.json",
            "environment/execution-environment.json",
            "offline-pack.json",
            *(ref["path"] for ref in object_refs.values()),
            *(ref["path"] for _manifest, ref in source_manifests),
            *(ref["path"] for ref in reducer_files),
            *(ref["path"] for ref in schema_refs),
            *(ref["path"] for ref in packed_evidence_refs),
        ]
        _reject_path_collisions(all_pack_paths)

        pack: dict[str, Any] = {
            "configuration": configuration_ref,
            "environment": environment_ref,
            "input_bindings": input_bindings,
            "inventory": inventory_ref,
            "objects": [object_refs[path] for path in sorted(object_refs)],
            "offline_pack_id": ZERO_HASH,
            "reducer": {
                "entrypoint": reducer["entrypoint"],
                "files": reducer_files,
                "git_commit": reducer["git_commit"],
            },
            "schema": contracts.PACK_SCHEMA_V3 if is_v3 else contracts.PACK_SCHEMA_V2,
            "schemas": schema_refs,
            "source_manifests": [ref for _manifest, ref in source_manifests],
            "source_set_root": (
                contracts.source_set_root_v3
                if is_v3
                else contracts.source_set_root_v2
            )(
                inventory["inventory_id"],
                [manifest["source_manifest_id"] for manifest, _ref in source_manifests],
                input_bindings,
            ),
        }
        if is_v3:
            pack["evidence"] = {
                "acquisition_receipts": [
                    acquisition_receipt_refs[key]
                    for key in sorted(acquisition_receipt_refs)
                ],
                "normalization_lineages": [
                    normalization_lineage_refs[key]
                    for key in sorted(normalization_lineage_refs)
                ],
                "request_parameter_preimages": [
                    request_preimage_refs[key]
                    for key in sorted(request_preimage_refs)
                ],
            }
        pack["offline_pack_id"] = contracts.offline_pack_identity(pack)
        manifest_ref = _write_document(staging, "offline-pack.json", pack)
        try:
            contracts.validate_offline_pack(pack)
        except contracts.VerificationError as exc:
            raise PackCompilationError(
                f"candidate pack manifest validation failed: {exc}"
            ) from exc

        fingerprint_refs = [
            inventory_ref,
            environment_ref,
            *(ref for _manifest, ref in source_manifests),
            *object_refs.values(),
            *reducer_files,
            configuration_ref,
            *schema_refs,
            *packed_evidence_refs,
            manifest_ref,
        ]
        fingerprints = {
            ref["path"]: (ref["sha256"], ref["bytes"])
            for ref in fingerprint_refs
        }
        if len(fingerprints) != len(fingerprint_refs):
            _fail("pack paths", "duplicate file reference after candidate validation")
        if _tree_paths(staging) != set(fingerprints):
            _fail("candidate pack", "verified file set changed before publication")
        total_bytes = sum(size for _digest_value, size in fingerprints.values())

        if before_seal is not None:
            before_seal()
        for input_id, expected_paths in mutable_input_paths.items():
            declaration = declarations[input_id]
            actual_paths = _enumerate_input(
                resolved_roots[declaration["root"]],
                declaration,
                f"$.input_bindings[{input_id!r}]",
            )
            if actual_paths != expected_paths:
                _fail(
                    f"$.input_bindings[{input_id!r}]",
                    "mutable input member set changed during compilation "
                    f"(before={list(expected_paths)}, after={list(actual_paths)})",
                )
        if is_v3:
            for (root_name, relative), expected in sorted(
                evidence_fingerprints.items()
            ):
                actual = _fingerprint_regular(
                    resolved_roots[root_name],
                    relative,
                    f"evidence source {root_name}:{relative}",
                )
                if actual != expected:
                    _fail(
                        f"evidence source {root_name}:{relative}",
                        "changed after validation/copy and before pack sealing",
                    )
        for name, root in resolved_roots.items():
            _verify_directory_identity(root, root_identities[name], f"roots.{name}")
        if _validate_private_store(store) != store_identity:
            _fail("output store", "directory inode changed during compilation")
        _verify_store_descriptor(store_descriptor, store_identity)
        _verify_directory_identity_at(
            store_descriptor,
            staging.name,
            staging_identity,
            "staging",
        )
        _verify_directory_identity(staging, staging_identity, "staging")

        _make_read_only(staging)
        _fsync_tree(staging)
        if _validate_private_store(store) != store_identity:
            _fail("output store", "directory inode changed before publication")
        _verify_store_descriptor(store_descriptor, store_identity)
        _verify_directory_identity_at(
            store_descriptor,
            staging.name,
            staging_identity,
            "staging",
        )
        _verify_directory_identity(staging, staging_identity, "staging")
        if is_v3:
            counts = _verify_existing_pack(
                staging,
                pack["offline_pack_id"],
                fingerprints,
                expected_schema=contracts.PACK_SCHEMA_V3,
            )
        else:
            counts = _verify_existing_pack(
                staging,
                pack["offline_pack_id"],
                fingerprints,
            )
        _verify_store_descriptor(store_descriptor, store_identity)
        _verify_directory_identity(store, store_identity, "output store")
        _verify_directory_identity_at(
            store_descriptor,
            staging.name,
            staging_identity,
            "staging",
        )
        _verify_directory_identity(staging, staging_identity, "staging")

        target_name = pack["offline_pack_id"].removeprefix("sha256:")
        target = store / target_name
        existing_identity = _directory_identity_at(
            store_descriptor,
            target_name,
            "existing pack",
            missing_ok=True,
        )
        if existing_identity is not None:
            if is_v3:
                existing_counts, existing_identity = _verify_existing_pack_at(
                    store,
                    store_descriptor,
                    store_identity,
                    target_name,
                    pack["offline_pack_id"],
                    fingerprints,
                    expected_schema=contracts.PACK_SCHEMA_V3,
                )
            else:
                existing_counts, existing_identity = _verify_existing_pack_at(
                    store,
                    store_descriptor,
                    store_identity,
                    target_name,
                    pack["offline_pack_id"],
                    fingerprints,
                )
            _remove_tree_at(store_descriptor, staging.name, staging_identity)
            _verify_store_descriptor(store_descriptor, store_identity)
            _verify_directory_identity(store, store_identity, "output store")
            _verify_directory_identity_at(
                store_descriptor,
                target_name,
                existing_identity,
                "existing pack",
            )
            _verify_directory_identity(target, existing_identity, "existing pack")
            return CompiledPack(
                root=target,
                manifest_path=target / "offline-pack.json",
                offline_pack_id=pack["offline_pack_id"],
                source_set_root=pack["source_set_root"],
                source_manifests=existing_counts["source_manifests"],
                source_objects=existing_counts["source_objects"],
                files=len(fingerprints),
                bytes=total_bytes,
                reused=True,
            )

        try:
            _publish_no_replace(store_descriptor, staging.name, target_name)
        except _DestinationExists:
            if is_v3:
                existing_counts, existing_identity = _verify_existing_pack_at(
                    store,
                    store_descriptor,
                    store_identity,
                    target_name,
                    pack["offline_pack_id"],
                    fingerprints,
                    expected_schema=contracts.PACK_SCHEMA_V3,
                )
            else:
                existing_counts, existing_identity = _verify_existing_pack_at(
                    store,
                    store_descriptor,
                    store_identity,
                    target_name,
                    pack["offline_pack_id"],
                    fingerprints,
                )
            _remove_tree_at(store_descriptor, staging.name, staging_identity)
            _verify_store_descriptor(store_descriptor, store_identity)
            _verify_directory_identity(store, store_identity, "output store")
            _verify_directory_identity_at(
                store_descriptor,
                target_name,
                existing_identity,
                "existing pack",
            )
            _verify_directory_identity(target, existing_identity, "existing pack")
            return CompiledPack(
                root=target,
                manifest_path=target / "offline-pack.json",
                offline_pack_id=pack["offline_pack_id"],
                source_set_root=pack["source_set_root"],
                source_manifests=existing_counts["source_manifests"],
                source_objects=existing_counts["source_objects"],
                files=len(fingerprints),
                bytes=total_bytes,
                reused=True,
            )
        published = True
        _verify_directory_identity_at(
            store_descriptor,
            target_name,
            staging_identity,
            "published pack",
        )
        os.fsync(store_descriptor)
        _verify_store_descriptor(store_descriptor, store_identity)
        _verify_directory_identity(store, store_identity, "output store")
        _verify_directory_identity(target, staging_identity, "published pack")
        return CompiledPack(
            root=target,
            manifest_path=target / "offline-pack.json",
            offline_pack_id=pack["offline_pack_id"],
            source_set_root=pack["source_set_root"],
            source_manifests=counts["source_manifests"],
            source_objects=counts["source_objects"],
            files=len(fingerprints),
            bytes=total_bytes,
            reused=False,
        )
    except BaseException:
        if not published and staging_identity is not None and store_descriptor >= 0:
            _remove_tree_at(store_descriptor, staging.name, staging_identity)
        raise
    finally:
        if store_descriptor >= 0:
            os.close(store_descriptor)


def _parse_roots(values: Sequence[str]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for index, value in enumerate(values):
        if "=" not in value:
            _fail(f"--root[{index}]", "expected NAME=PATH")
        name, raw_path = value.split("=", 1)
        _name(name, f"--root[{index}]")
        if not raw_path:
            _fail(f"--root[{index}]", "path must not be empty")
        if name in result:
            _fail(f"--root[{index}]", f"duplicate root {name!r}")
        result[name] = Path(raw_path)
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--inventory", required=True, type=Path)
    parser.add_argument("--output-store", required=True, type=Path)
    parser.add_argument(
        "--root",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="bind one relocation-independent plan root (repeatable)",
    )
    parser.add_argument("--git", default="git", help="Git executable")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        result = compile_offline_pack_v2(
            args.plan,
            args.inventory,
            args.output_store,
            roots=_parse_roots(args.root),
            git_executable=args.git,
        )
    except (
        OSError,
        PackCompilationError,
        contracts.VerificationError,
        build_context.BuildContextError,
    ) as exc:
        print(
            contracts.canonical_json_bytes(
                {"error": {"message": str(exc), "type": type(exc).__name__}, "ok": False}
            ).decode("utf-8"),
            file=sys.stderr,
        )
        return 1
    print(contracts.canonical_json_bytes(result.to_document()).decode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
