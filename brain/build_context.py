#!/usr/bin/env python3
"""Strict runtime context for a sealed, path-independent Brain replay.

The context is deliberately a *runtime* document.  It is assembled only after
an offline pack and reducer inventory have been verified; it does not replace
either authority contract.  Reducers consume its exact input bindings and write
only its declared stage outputs.

No function in this module creates, copies, chmods, or otherwise mutates a file.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import stat
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any


BUILD_CONTEXT_SCHEMA = "wikilean.brain-build-context/v1"
GENERATION_DOMAIN = "wikilean.brain-generation.v1"
REDUCER_CONFIGURATION_SCHEMA = "wikilean.brain-reducer-config/v1"
MAX_SAFE_INTEGER = 9_007_199_254_740_991

HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
EPOCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
MEDIA_TYPE_RE = re.compile(r"^[^/\s]+/[^/\s]+$")

INPUT_ROOT_IDS = frozenset({"repo", "external", "mathlib", "decl_oracle"})
PIN_TYPES = frozenset(
    {"git_commit", "content_sha256", "dataset_revision", "http_etag", "database_snapshot"}
)
INPUT_CLASSES = frozenset({"curated_git_input", "immutable_source_object"})
CELL_ATTACH_KINDS = frozenset(
    {"generalization", "special_case", "invocation", "related"}
)


class BuildContextError(ValueError):
    """A runtime build-context document violates its closed-world contract."""


def _fail(location: str, message: str) -> None:
    raise BuildContextError(f"{location}: {message}")


def _check_unicode(value: str, location: str) -> str:
    if "\x00" in value:
        _fail(location, "NUL is forbidden")
    if unicodedata.normalize("NFC", value) != value:
        _fail(location, "text must be Unicode NFC")
    return value


def _string(value: Any, location: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str):
        _fail(location, "expected string")
    _check_unicode(value, location)
    if nonempty and not value:
        _fail(location, "must not be empty")
    return value


def _name(value: Any, location: str) -> str:
    text = _string(value, location)
    if not NAME_RE.fullmatch(text):
        _fail(location, "expected a lowercase stable ID")
    return text


def _hash(value: Any, location: str) -> str:
    text = _string(value, location)
    if not HASH_RE.fullmatch(text):
        _fail(location, "expected sha256:<64 lowercase hex>")
    return text


def _digest(value: Any, location: str) -> str:
    text = _string(value, location)
    if not DIGEST_RE.fullmatch(text):
        _fail(location, "expected 64 lowercase hex characters")
    return text


def _git_commit(value: Any, location: str) -> str:
    text = _string(value, location)
    if not GIT_COMMIT_RE.fullmatch(text):
        _fail(location, "expected a full lowercase Git commit")
    return text


def _object(
    value: Any,
    location: str,
    required: set[str],
    optional: set[str] = frozenset(),
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail(location, "expected object")
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
        _fail(location, "expected array")
    if nonempty and not value:
        _fail(location, "must not be empty")
    return value


def _integer(value: Any, location: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(location, "expected integer")
    if not 0 <= value <= MAX_SAFE_INTEGER:
        _fail(location, "integer is outside the portable nonnegative range")
    return value


def _relative_path(value: Any, location: str, *, literal: bool = True) -> str:
    text = _string(value, location)
    if text.startswith("/") or "\\" in text:
        _fail(location, "expected a normalized POSIX relative path")
    path = PurePosixPath(text)
    if path.as_posix() != text or any(part in {"", ".", ".."} for part in path.parts):
        _fail(location, "expected a normalized POSIX relative path")
    if literal and any(character in text for character in "*?[]{}"):
        _fail(location, "literal paths must not contain glob metacharacters")
    if not literal and ("{" in text or "}" in text):
        _fail(location, "brace expansion is not supported")
    return text


def _absolute_root(value: Any, location: str) -> Path:
    if isinstance(value, os.PathLike):
        text = _check_unicode(os.fspath(value), location)
    else:
        text = _string(value, location)
    path = Path(text)
    if not path.is_absolute():
        _fail(location, "expected an absolute path")
    if any(part in {".", ".."} for part in path.parts) or path.as_posix() != text:
        _fail(location, "absolute roots must be normalized")
    return path.resolve(strict=False)


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _relative_paths_overlap(left: str, right: str) -> bool:
    left_parts = PurePosixPath(left).parts
    right_parts = PurePosixPath(right).parts
    common = min(len(left_parts), len(right_parts))
    return left_parts[:common] == right_parts[:common]


def _matches_relative_pattern(path: str, pattern: str) -> bool:
    """Match POSIX glob segments with ``**`` crossing directories only."""
    path_parts = PurePosixPath(path).parts
    pattern_parts = PurePosixPath(pattern).parts
    previous = [False] * (len(path_parts) + 1)
    previous[0] = True
    for part in pattern_parts:
        current = [False] * (len(path_parts) + 1)
        if part == "**":
            current[0] = previous[0]
            for path_index in range(1, len(path_parts) + 1):
                current[path_index] = previous[path_index] or current[path_index - 1]
        else:
            for path_index, path_part in enumerate(path_parts, start=1):
                current[path_index] = previous[path_index - 1] and fnmatch.fnmatchcase(
                    path_part, part
                )
        previous = current
    return previous[-1]


def _canonical_json_value(value: Any, location: str = "$") -> Any:
    if value is None or isinstance(value, (str, bool)):
        if isinstance(value, str):
            _check_unicode(value, location)
        return value
    if isinstance(value, int):
        if not -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER:
            _fail(location, "integer exceeds the portable range")
        return value
    if isinstance(value, list):
        return [
            _canonical_json_value(item, f"{location}[{index}]")
            for index, item in enumerate(value)
        ]
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                _fail(location, "object keys must be strings")
            _check_unicode(key, f"{location}.<key>")
            result[key] = _canonical_json_value(item, f"{location}.{key}")
        return result
    _fail(location, f"unsupported canonical JSON type {type(value).__name__}")


def canonical_json_bytes(value: Any) -> bytes:
    normalized = _canonical_json_value(value)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _domain_hash(domain: str, value: Any) -> str:
    prefix = f"wikilean\0{domain}\0canonical-json-v1\0".encode("ascii")
    return "sha256:" + hashlib.sha256(prefix + canonical_json_bytes(value)).hexdigest()


def _freeze_json(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class Replay:
    offline_pack_id: str
    source_set_root: str
    reducer_inventory_id: str
    authority_git_commit: str
    authority_root: str
    prior_state_root: str | None
    semantic_epoch: str
    reducer_git_commit: str
    configuration_sha256: str
    environment_sha256: str

    def __post_init__(self) -> None:
        _hash(self.offline_pack_id, "$.replay.offline_pack_id")
        _hash(self.source_set_root, "$.replay.source_set_root")
        _hash(self.reducer_inventory_id, "$.replay.reducer_inventory_id")
        _git_commit(self.authority_git_commit, "$.replay.authority.git_commit")
        _hash(self.authority_root, "$.replay.authority.authority_root")
        if self.prior_state_root is not None:
            _hash(self.prior_state_root, "$.replay.prior_state_root")
        if not EPOCH_RE.fullmatch(self.semantic_epoch):
            _fail("$.replay.semantic_epoch", "invalid semantic epoch")
        _git_commit(self.reducer_git_commit, "$.replay.reducer.git_commit")
        _digest(self.configuration_sha256, "$.replay.reducer.configuration_sha256")
        _digest(self.environment_sha256, "$.replay.reducer.environment_sha256")

    @classmethod
    def from_document(cls, value: Any, location: str = "$.replay") -> Replay:
        obj = _object(
            value,
            location,
            {
                "offline_pack_id",
                "source_set_root",
                "reducer_inventory_id",
                "authority",
                "prior_state_root",
                "semantic_epoch",
                "reducer",
            },
        )
        authority = _object(
            obj["authority"],
            f"{location}.authority",
            {"git_commit", "authority_root"},
        )
        reducer = _object(
            obj["reducer"],
            f"{location}.reducer",
            {"git_commit", "configuration_sha256", "environment_sha256"},
        )
        prior = obj["prior_state_root"]
        if prior is not None:
            prior = _hash(prior, f"{location}.prior_state_root")
        return cls(
            offline_pack_id=_hash(obj["offline_pack_id"], f"{location}.offline_pack_id"),
            source_set_root=_hash(obj["source_set_root"], f"{location}.source_set_root"),
            reducer_inventory_id=_hash(
                obj["reducer_inventory_id"], f"{location}.reducer_inventory_id"
            ),
            authority_git_commit=_git_commit(
                authority["git_commit"], f"{location}.authority.git_commit"
            ),
            authority_root=_hash(
                authority["authority_root"],
                f"{location}.authority.authority_root",
            ),
            prior_state_root=prior,
            semantic_epoch=_string(obj["semantic_epoch"], f"{location}.semantic_epoch"),
            reducer_git_commit=_git_commit(
                reducer["git_commit"], f"{location}.reducer.git_commit"
            ),
            configuration_sha256=_digest(
                reducer["configuration_sha256"],
                f"{location}.reducer.configuration_sha256",
            ),
            environment_sha256=_digest(
                reducer["environment_sha256"],
                f"{location}.reducer.environment_sha256",
            ),
        )

    def to_document(self) -> dict[str, Any]:
        return {
            "authority": {
                "authority_root": self.authority_root,
                "git_commit": self.authority_git_commit,
            },
            "offline_pack_id": self.offline_pack_id,
            "prior_state_root": self.prior_state_root,
            "reducer": {
                "configuration_sha256": self.configuration_sha256,
                "environment_sha256": self.environment_sha256,
                "git_commit": self.reducer_git_commit,
            },
            "reducer_inventory_id": self.reducer_inventory_id,
            "semantic_epoch": self.semantic_epoch,
            "source_set_root": self.source_set_root,
        }


@dataclass(frozen=True, slots=True)
class BuildRoots:
    code: Path
    input: Path
    output: Path
    scratch: Path

    def __post_init__(self) -> None:
        normalized = []
        for name in ("code", "input", "output", "scratch"):
            path = _absolute_root(getattr(self, name), f"$.roots.{name}")
            object.__setattr__(self, name, path)
            normalized.append((name, path))
        for index, (left_name, left) in enumerate(normalized):
            for right_name, right in normalized[index + 1 :]:
                if _paths_overlap(left, right):
                    _fail(
                        "$.roots",
                        f"{left_name} and {right_name} overlap by ancestry",
                    )

    @classmethod
    def from_document(cls, value: Any, location: str = "$.roots") -> BuildRoots:
        obj = _object(value, location, {"code", "input", "output", "scratch"})
        return cls(
            code=_absolute_root(obj["code"], f"{location}.code"),
            input=_absolute_root(obj["input"], f"{location}.input"),
            output=_absolute_root(obj["output"], f"{location}.output"),
            scratch=_absolute_root(obj["scratch"], f"{location}.scratch"),
        )

    def to_document(self) -> dict[str, str]:
        return {
            "code": str(self.code),
            "input": str(self.input),
            "output": str(self.output),
            "scratch": str(self.scratch),
        }


@dataclass(frozen=True, slots=True)
class ReducerConfiguration:
    external_node_cap: int
    cell_attach_kinds: tuple[str, ...]
    layout_enabled: bool
    layout_iterations: int
    schema: str = REDUCER_CONFIGURATION_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(self, "cell_attach_kinds", tuple(self.cell_attach_kinds))
        if self.schema != REDUCER_CONFIGURATION_SCHEMA:
            _fail("$.configuration.schema", f"expected {REDUCER_CONFIGURATION_SCHEMA!r}")
        _integer(self.external_node_cap, "$.configuration.external_node_cap")
        if list(self.cell_attach_kinds) != sorted(set(self.cell_attach_kinds)):
            _fail(
                "$.configuration.cell_attach_kinds",
                "entries must be unique and sorted",
            )
        unknown = sorted(set(self.cell_attach_kinds) - CELL_ATTACH_KINDS)
        if unknown:
            _fail(
                "$.configuration.cell_attach_kinds",
                "unknown match kinds: " + ", ".join(unknown),
            )
        if not isinstance(self.layout_enabled, bool):
            _fail("$.configuration.layout.enabled", "expected boolean")
        _integer(self.layout_iterations, "$.configuration.layout.iterations")

    @classmethod
    def from_document(
        cls, value: Any, location: str = "$.configuration"
    ) -> ReducerConfiguration:
        obj = _object(
            value,
            location,
            {"schema", "external_node_cap", "cell_attach_kinds", "layout"},
        )
        if obj["schema"] != REDUCER_CONFIGURATION_SCHEMA:
            _fail(f"{location}.schema", f"expected {REDUCER_CONFIGURATION_SCHEMA!r}")
        raw_kinds = _array(obj["cell_attach_kinds"], f"{location}.cell_attach_kinds")
        kinds = tuple(
            _string(kind, f"{location}.cell_attach_kinds[{index}]")
            for index, kind in enumerate(raw_kinds)
        )
        layout = _object(
            obj["layout"], f"{location}.layout", {"enabled", "iterations"}
        )
        if not isinstance(layout["enabled"], bool):
            _fail(f"{location}.layout.enabled", "expected boolean")
        return cls(
            schema=obj["schema"],
            external_node_cap=_integer(
                obj["external_node_cap"], f"{location}.external_node_cap"
            ),
            cell_attach_kinds=kinds,
            layout_enabled=layout["enabled"],
            layout_iterations=_integer(
                layout["iterations"], f"{location}.layout.iterations"
            ),
        )

    def to_document(self) -> dict[str, Any]:
        return {
            "cell_attach_kinds": list(self.cell_attach_kinds),
            "external_node_cap": self.external_node_cap,
            "layout": {
                "enabled": self.layout_enabled,
                "iterations": self.layout_iterations,
            },
            "schema": self.schema,
        }


@dataclass(frozen=True, slots=True)
class SourcePin:
    type: str
    value: str
    tree: str | None = None

    def __post_init__(self) -> None:
        if self.type not in PIN_TYPES:
            _fail("$.bindings[].members[].pin.type", "unknown source pin type")
        _string(self.value, "$.bindings[].members[].pin.value")
        if len(self.value) > 512:
            _fail("$.bindings[].members[].pin.value", "must be at most 512 characters")
        if self.type == "git_commit":
            _git_commit(self.value, "$.bindings[].members[].pin.value")
        if self.type == "content_sha256":
            _digest(self.value, "$.bindings[].members[].pin.value")
        if self.tree is not None:
            if self.type != "git_commit":
                _fail("$.bindings[].members[].pin.tree", "tree requires a git_commit pin")
            _git_commit(self.tree, "$.bindings[].members[].pin.tree")

    @classmethod
    def from_document(cls, value: Any, location: str) -> SourcePin:
        obj = _object(value, location, {"type", "value"}, {"tree"})
        return cls(
            type=_string(obj["type"], f"{location}.type"),
            value=_string(obj["value"], f"{location}.value"),
            tree=(
                _git_commit(obj["tree"], f"{location}.tree")
                if "tree" in obj
                else None
            ),
        )

    def to_document(self) -> dict[str, str]:
        result = {"type": self.type, "value": self.value}
        if self.tree is not None:
            result["tree"] = self.tree
        return result


@dataclass(frozen=True, slots=True)
class InputMember:
    logical_path: str
    source_manifest_id: str
    object_name: str
    sha256: str
    byte_length: int
    media_type: str
    pin: SourcePin
    materialized_path: Path | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.pin, SourcePin):
            _fail("$.bindings[].members[].pin", "expected SourcePin")
        object.__setattr__(
            self,
            "logical_path",
            _relative_path(self.logical_path, "$.bindings[].members[].path"),
        )
        _hash(self.source_manifest_id, "$.bindings[].members[].source_manifest_id")
        _name(self.object_name, "$.bindings[].members[].object")
        _digest(self.sha256, "$.bindings[].members[].sha256")
        _integer(self.byte_length, "$.bindings[].members[].bytes")
        if not MEDIA_TYPE_RE.fullmatch(self.media_type):
            _fail("$.bindings[].members[].media_type", "invalid media type")
        if self.materialized_path is not None:
            object.__setattr__(
                self,
                "materialized_path",
                _absolute_root(
                    self.materialized_path,
                    "$.bindings[].members[].materialized_path",
                ),
            )

    @classmethod
    def from_document(cls, value: Any, location: str) -> InputMember:
        obj = _object(
            value,
            location,
            {"path", "source_manifest_id", "object", "sha256", "bytes", "media_type", "pin"},
            {"materialized_path"},
        )
        media_type = _string(obj["media_type"], f"{location}.media_type")
        if not MEDIA_TYPE_RE.fullmatch(media_type):
            _fail(f"{location}.media_type", "invalid media type")
        return cls(
            logical_path=_relative_path(obj["path"], f"{location}.path"),
            source_manifest_id=_hash(
                obj["source_manifest_id"], f"{location}.source_manifest_id"
            ),
            object_name=_name(obj["object"], f"{location}.object"),
            sha256=_digest(obj["sha256"], f"{location}.sha256"),
            byte_length=_integer(obj["bytes"], f"{location}.bytes"),
            media_type=media_type,
            pin=SourcePin.from_document(obj["pin"], f"{location}.pin"),
            materialized_path=(
                _absolute_root(
                    obj["materialized_path"], f"{location}.materialized_path"
                )
                if "materialized_path" in obj
                else None
            ),
        )

    def to_document(self, *, include_physical: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "bytes": self.byte_length,
            "media_type": self.media_type,
            "object": self.object_name,
            "path": self.logical_path,
            "pin": self.pin.to_document(),
            "sha256": self.sha256,
            "source_manifest_id": self.source_manifest_id,
        }
        if include_physical and self.materialized_path is not None:
            result["materialized_path"] = str(self.materialized_path)
        return result


@dataclass(frozen=True, slots=True)
class InputBinding:
    input_id: str
    root: str
    cardinality: str
    requirement: str
    input_class: str
    state: str
    declared_path: str
    members: tuple[InputMember, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "members", tuple(self.members))
        if not all(isinstance(member, InputMember) for member in self.members):
            _fail("$.bindings[].members", "expected InputMember entries")
        _name(self.input_id, "$.bindings[].input_id")
        if self.root not in INPUT_ROOT_IDS:
            _fail("$.bindings[].root", "unknown input root")
        if self.cardinality not in {"one", "many"}:
            _fail("$.bindings[].cardinality", "expected one or many")
        if self.requirement not in {"required", "optional"}:
            _fail("$.bindings[].requirement", "expected required or optional")
        if self.input_class not in INPUT_CLASSES:
            _fail("$.bindings[].class", "unknown input class")
        if self.state not in {"present", "absent"}:
            _fail("$.bindings[].state", "expected present or absent")
        declared = _relative_path(
            self.declared_path,
            "$.bindings[].path" if self.cardinality == "one" else "$.bindings[].path_pattern",
            literal=self.cardinality == "one",
        )
        object.__setattr__(self, "declared_path", declared)
        if self.requirement == "required" and self.state != "present":
            _fail("$.bindings[].state", "required inputs must be present")
        if self.state == "absent" and self.members:
            _fail("$.bindings[].members", "absent bindings must have no members")
        if self.state == "present" and not self.members:
            _fail("$.bindings[].members", "present bindings must have members")
        if self.cardinality == "one" and self.state == "present" and len(self.members) != 1:
            _fail("$.bindings[].members", "cardinality one requires exactly one member")
        paths = [member.logical_path for member in self.members]
        if paths != sorted(set(paths)):
            _fail("$.bindings[].members", "members must have unique paths sorted by path")
        for member in self.members:
            matches = (
                member.logical_path == self.declared_path
                if self.cardinality == "one"
                else _matches_relative_pattern(member.logical_path, self.declared_path)
            )
            if not matches:
                _fail(
                    "$.bindings[].members[].path",
                    f"{member.logical_path!r} does not match {self.declared_path!r}",
                )
            if self.input_class == "curated_git_input" and (
                member.pin.type != "git_commit" or member.pin.tree is None
            ):
                _fail(
                    "$.bindings[].members[].pin",
                    "curated_git_input members require a git_commit pin with tree",
                )

    @classmethod
    def from_document(cls, value: Any, location: str) -> InputBinding:
        if not isinstance(value, Mapping):
            _fail(location, "expected object")
        selectors = {key for key in ("path", "path_pattern") if key in value}
        if len(selectors) != 1:
            _fail(location, "must contain exactly one of path or path_pattern")
        selector = next(iter(selectors))
        required = {
            "input_id",
            "root",
            "cardinality",
            "requirement",
            "class",
            "state",
            "members",
            selector,
        }
        obj = _object(value, location, required)
        cardinality = _string(obj["cardinality"], f"{location}.cardinality")
        if cardinality == "one" and selector != "path":
            _fail(location, "cardinality one requires path")
        if cardinality == "many" and selector != "path_pattern":
            _fail(location, "cardinality many requires path_pattern")
        raw_members = _array(obj["members"], f"{location}.members")
        members = tuple(
            InputMember.from_document(member, f"{location}.members[{index}]")
            for index, member in enumerate(raw_members)
        )
        return cls(
            input_id=_name(obj["input_id"], f"{location}.input_id"),
            root=_string(obj["root"], f"{location}.root"),
            cardinality=cardinality,
            requirement=_string(obj["requirement"], f"{location}.requirement"),
            input_class=_string(obj["class"], f"{location}.class"),
            state=_string(obj["state"], f"{location}.state"),
            declared_path=_relative_path(
                obj[selector], f"{location}.{selector}", literal=selector == "path"
            ),
            members=members,
        )

    def to_document(self, *, include_physical: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "cardinality": self.cardinality,
            "class": self.input_class,
            "input_id": self.input_id,
            "members": [
                member.to_document(include_physical=include_physical)
                for member in self.members
            ],
            "requirement": self.requirement,
            "root": self.root,
            "state": self.state,
        }
        result["path" if self.cardinality == "one" else "path_pattern"] = self.declared_path
        return result


@dataclass(frozen=True, slots=True)
class StageOutput:
    kind: str
    path: str

    def __post_init__(self) -> None:
        if self.kind not in {"file", "tree"}:
            _fail("$.stages[].outputs[].kind", "expected file or tree")
        object.__setattr__(
            self,
            "path",
            _relative_path(self.path, "$.stages[].outputs[].path"),
        )

    @classmethod
    def from_document(cls, value: Any, location: str) -> StageOutput:
        obj = _object(value, location, {"kind", "path"})
        return cls(
            kind=_string(obj["kind"], f"{location}.kind"),
            path=_relative_path(obj["path"], f"{location}.path"),
        )

    def to_document(self) -> dict[str, str]:
        return {"kind": self.kind, "path": self.path}


@dataclass(frozen=True, slots=True)
class Stage:
    id: str
    program: str
    argv: tuple[str, ...]
    needs: tuple[str, ...]
    outputs: tuple[StageOutput, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "argv", tuple(self.argv))
        object.__setattr__(self, "needs", tuple(self.needs))
        object.__setattr__(self, "outputs", tuple(self.outputs))
        if not all(isinstance(output, StageOutput) for output in self.outputs):
            _fail("$.stages[].outputs", "expected StageOutput entries")
        _name(self.id, "$.stages[].id")
        object.__setattr__(self, "program", _relative_path(self.program, "$.stages[].program"))
        for index, argument in enumerate(self.argv):
            _string(argument, f"$.stages[].argv[{index}]", nonempty=False)
        for index, need in enumerate(self.needs):
            _name(need, f"$.stages[].needs[{index}]")
        if list(self.needs) != sorted(set(self.needs)):
            _fail("$.stages[].needs", "dependencies must be unique and sorted")
        if not self.outputs:
            _fail("$.stages[].outputs", "must not be empty")
        paths = [output.path for output in self.outputs]
        if paths != sorted(set(paths)):
            _fail("$.stages[].outputs", "outputs must have unique paths sorted by path")

    @classmethod
    def from_document(cls, value: Any, location: str) -> Stage:
        obj = _object(value, location, {"id", "program", "argv", "needs", "outputs"})
        argv = tuple(
            _string(argument, f"{location}.argv[{index}]", nonempty=False)
            for index, argument in enumerate(_array(obj["argv"], f"{location}.argv"))
        )
        needs = tuple(
            _name(need, f"{location}.needs[{index}]")
            for index, need in enumerate(_array(obj["needs"], f"{location}.needs"))
        )
        outputs = tuple(
            StageOutput.from_document(output, f"{location}.outputs[{index}]")
            for index, output in enumerate(
                _array(obj["outputs"], f"{location}.outputs", nonempty=True)
            )
        )
        return cls(
            id=_name(obj["id"], f"{location}.id"),
            program=_relative_path(obj["program"], f"{location}.program"),
            argv=argv,
            needs=needs,
            outputs=outputs,
        )

    def to_document(self) -> dict[str, Any]:
        return {
            "argv": list(self.argv),
            "id": self.id,
            "needs": list(self.needs),
            "outputs": [output.to_document() for output in self.outputs],
            "program": self.program,
        }


@dataclass(frozen=True, slots=True)
class BuildContext:
    generation_id: str
    replay: Replay
    roots: BuildRoots
    bindings: tuple[InputBinding, ...]
    stages: tuple[Stage, ...]
    configuration: ReducerConfiguration
    audit: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.replay, Replay):
            _fail("$.replay", "expected Replay")
        if not isinstance(self.roots, BuildRoots):
            _fail("$.roots", "expected BuildRoots")
        object.__setattr__(self, "bindings", tuple(self.bindings))
        object.__setattr__(self, "stages", tuple(self.stages))
        if not all(isinstance(binding, InputBinding) for binding in self.bindings):
            _fail("$.bindings", "expected InputBinding entries")
        if not all(isinstance(stage, Stage) for stage in self.stages):
            _fail("$.stages", "expected Stage entries")
        _hash(self.generation_id, "$.generation_id")
        configuration = (
            self.configuration
            if isinstance(self.configuration, ReducerConfiguration)
            else ReducerConfiguration.from_document(self.configuration)
        )
        object.__setattr__(self, "configuration", configuration)
        if self.audit is not None:
            audit = _canonical_json_value(self.audit, "$.audit")
            if not isinstance(audit, dict):
                _fail("$.audit", "expected object")
            object.__setattr__(self, "audit", _freeze_json(audit))

        config_digest = hashlib.sha256(
            canonical_json_bytes(configuration.to_document())
        ).hexdigest()
        if config_digest != self.replay.configuration_sha256:
            _fail(
                "$.configuration",
                "canonical bytes do not match replay.reducer.configuration_sha256",
            )

        input_ids = [binding.input_id for binding in self.bindings]
        if not input_ids:
            _fail("$.bindings", "must not be empty")
        if input_ids != sorted(set(input_ids)):
            _fail("$.bindings", "bindings must have unique input IDs sorted by input_id")
        logical_members: set[tuple[str, str]] = set()
        for binding in self.bindings:
            root = self.input_root(binding.root)
            for member in binding.members:
                key = (binding.root, member.logical_path)
                if key in logical_members:
                    _fail("$.bindings", f"logical input {key!r} is bound more than once")
                logical_members.add(key)
                expected = self._contained(root, member.logical_path, "$.bindings[].members[].path")
                if member.materialized_path is not None and member.materialized_path != expected:
                    _fail(
                        "$.bindings[].members[].materialized_path",
                        f"expected exact path {expected}",
                    )

        if not self.stages:
            _fail("$.stages", "must not be empty")
        prior_stages: set[str] = set()
        output_owners: list[tuple[str, str, str]] = []
        for stage in self.stages:
            if stage.id in prior_stages:
                _fail("$.stages", f"duplicate stage ID {stage.id!r}")
            unknown = sorted(set(stage.needs) - prior_stages)
            if unknown:
                _fail(
                    "$.stages[].needs",
                    "dependencies must name earlier stages: " + ", ".join(unknown),
                )
            self.code(stage.program)
            for output in stage.outputs:
                for owned_path, owned_stage, owned_kind in output_owners:
                    if _relative_paths_overlap(output.path, owned_path):
                        _fail(
                            "$.stages[].outputs[].path",
                            f"{output.path!r} overlaps {owned_kind} {owned_path!r} "
                            f"owned by stage {owned_stage!r}",
                        )
                output_owners.append((output.path, stage.id, output.kind))
            prior_stages.add(stage.id)

        expected_generation = self.computed_generation_id()
        if self.generation_id != expected_generation:
            _fail("$.generation_id", f"expected {expected_generation}")

    @classmethod
    def from_document(cls, value: Any) -> BuildContext:
        obj = _object(
            value,
            "$",
            {"schema", "generation_id", "replay", "roots", "bindings", "stages", "configuration"},
            {"audit"},
        )
        if obj["schema"] != BUILD_CONTEXT_SCHEMA:
            _fail("$.schema", f"unknown schema/version {obj['schema']!r}")
        replay = Replay.from_document(obj["replay"])
        roots = BuildRoots.from_document(obj["roots"])
        bindings = tuple(
            InputBinding.from_document(binding, f"$.bindings[{index}]")
            for index, binding in enumerate(
                _array(obj["bindings"], "$.bindings", nonempty=True)
            )
        )
        stages = tuple(
            Stage.from_document(stage, f"$.stages[{index}]")
            for index, stage in enumerate(_array(obj["stages"], "$.stages", nonempty=True))
        )
        configuration = ReducerConfiguration.from_document(obj["configuration"])
        audit = (
            _canonical_json_value(obj["audit"], "$.audit") if "audit" in obj else None
        )
        return cls(
            generation_id=_hash(obj["generation_id"], "$.generation_id"),
            replay=replay,
            roots=roots,
            bindings=bindings,
            stages=stages,
            configuration=configuration,
            audit=audit,
        )

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> BuildContext:
        source = Path(path)

        def reject_float(value: str) -> None:
            raise BuildContextError(f"{source}: floating-point JSON is forbidden ({value})")

        def reject_constant(value: str) -> None:
            raise BuildContextError(f"{source}: non-finite JSON is forbidden ({value})")

        def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, item in pairs:
                if key in result:
                    raise BuildContextError(f"{source}: duplicate object key {key!r}")
                result[key] = item
            return result

        try:
            raw = source.read_bytes()
            document = json.loads(
                raw,
                parse_float=reject_float,
                parse_constant=reject_constant,
                object_pairs_hook=reject_duplicates,
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise BuildContextError(f"{source}: could not load build context: {exc}") from exc
        if raw != canonical_json_bytes(document):
            raise BuildContextError(f"{source}: context is not canonical-json-v1 bytes")
        return cls.from_document(document)

    def to_document(
        self,
        *,
        include_physical: bool = True,
        include_audit: bool = True,
    ) -> dict[str, Any]:
        document: dict[str, Any] = {
            "bindings": [
                binding.to_document(include_physical=include_physical)
                for binding in self.bindings
            ],
            "configuration": self.configuration.to_document(),
            "generation_id": self.generation_id,
            "replay": self.replay.to_document(),
            "roots": self.roots.to_document(),
            "schema": BUILD_CONTEXT_SCHEMA,
            "stages": [stage.to_document() for stage in self.stages],
        }
        if include_audit and self.audit is not None:
            document["audit"] = _thaw_json(self.audit)
        return document

    def logical_document(self) -> dict[str, Any]:
        """Return the generation preimage, excluding every physical/audit field."""
        return {
            "bindings": [
                binding.to_document(include_physical=False) for binding in self.bindings
            ],
            "configuration": self.configuration.to_document(),
            "replay": self.replay.to_document(),
            "schema": BUILD_CONTEXT_SCHEMA,
            "stages": [stage.to_document() for stage in self.stages],
        }

    def computed_generation_id(self) -> str:
        return _domain_hash(GENERATION_DOMAIN, self.logical_document())

    def _binding(self, input_id: str) -> InputBinding:
        for binding in self.bindings:
            if binding.input_id == input_id:
                return binding
        raise BuildContextError(f"unknown input binding {input_id!r}")

    def stage(self, stage_id: str) -> Stage:
        for stage in self.stages:
            if stage.id == stage_id:
                return stage
        raise BuildContextError(f"unknown stage {stage_id!r}")

    def input_root(self, root_id: str) -> Path:
        if root_id not in INPUT_ROOT_IDS:
            raise BuildContextError(f"unknown input root {root_id!r}")
        return self._contained(self.roots.input, root_id, f"input root {root_id!r}")

    @staticmethod
    def _contained(root: Path, relative: str, location: str) -> Path:
        logical = _relative_path(relative, location)
        resolved_root = root.resolve(strict=False)
        parts = PurePosixPath(logical).parts
        candidate = resolved_root.joinpath(*parts)
        current = resolved_root
        for index, part in enumerate(parts):
            current = current / part
            try:
                mode = current.lstat().st_mode
            except FileNotFoundError:
                break
            except (NotADirectoryError, OSError) as exc:
                _fail(location, f"cannot inspect path component {current}: {exc}")
            if stat.S_ISLNK(mode):
                _fail(location, f"symlink components are forbidden: {current}")
            if index < len(parts) - 1 and not stat.S_ISDIR(mode):
                _fail(location, f"non-directory path component: {current}")
        return candidate

    def member_records(self, input_id: str) -> tuple[InputMember, ...]:
        return self._binding(input_id).members

    def members(self, input_id: str) -> tuple[Path, ...]:
        binding = self._binding(input_id)
        root = self.input_root(binding.root)
        return tuple(
            self._contained(root, member.logical_path, f"input {input_id!r}")
            for member in binding.members
        )

    def require_one(self, input_id: str) -> Path:
        binding = self._binding(input_id)
        if binding.cardinality != "one":
            raise BuildContextError(f"input {input_id!r} does not have cardinality one")
        paths = self.members(input_id)
        if binding.state != "present" or len(paths) != 1:
            raise BuildContextError(f"input {input_id!r} is absent")
        return paths[0]

    def optional_one(self, input_id: str) -> Path | None:
        binding = self._binding(input_id)
        if binding.cardinality != "one":
            raise BuildContextError(f"input {input_id!r} does not have cardinality one")
        if binding.requirement != "optional":
            raise BuildContextError(f"input {input_id!r} is not optional")
        paths = self.members(input_id)
        if binding.state == "absent":
            return None
        if len(paths) != 1:
            raise BuildContextError(f"input {input_id!r} does not resolve to one member")
        return paths[0]

    def code(self, relative: str) -> Path:
        return self._contained(self.roots.code, relative, "code path")

    def output(self, relative: str) -> Path:
        logical = _relative_path(relative, "output path")
        owned = any(self._stage_owns(stage, logical) for stage in self.stages)
        if not owned:
            raise BuildContextError(f"output path {logical!r} is not owned by any stage")
        return self._contained(self.roots.output, logical, "output path")

    @staticmethod
    def _stage_owns(stage: Stage, logical: str) -> bool:
        path = PurePosixPath(logical)
        return any(
            (output.kind == "file" and logical == output.path)
            or (
                output.kind == "tree"
                and (logical == output.path or PurePosixPath(output.path) in path.parents)
            )
            for output in stage.outputs
        )

    def output_for(self, stage_id: str, relative: str) -> Path:
        stage = self.stage(stage_id)
        logical = _relative_path(relative, f"output path for stage {stage_id!r}")
        if not self._stage_owns(stage, logical):
            raise BuildContextError(
                f"stage {stage_id!r} does not own output path {logical!r}"
            )
        return self._contained(self.roots.output, logical, "output path")

    def scratch_for(self, stage_id: str, relative: str) -> Path:
        self.stage(stage_id)
        logical = _relative_path(relative, f"scratch path for stage {stage_id!r}")
        stage_root = self._contained(self.roots.scratch, stage_id, "stage scratch root")
        return self._contained(stage_root, logical, "stage scratch path")


def generation_identity(context: BuildContext | Mapping[str, Any]) -> str:
    """Hash the logical context while excluding runtime paths and audit data.

    Accepting the pre-validation document lets the pack runner fill the
    self-authenticating ``generation_id`` field before constructing a
    :class:`BuildContext`.
    """
    if isinstance(context, BuildContext):
        logical = context.logical_document()
    else:
        document = _object(
            context,
            "$",
            {
                "schema",
                "replay",
                "roots",
                "bindings",
                "stages",
                "configuration",
            },
            {"audit", "generation_id"},
        )
        bindings = _array(document["bindings"], "$.bindings", nonempty=True)
        logical_bindings: list[Any] = []
        for binding_index, raw_binding in enumerate(bindings):
            binding = dict(
                _object(
                    raw_binding,
                    f"$.bindings[{binding_index}]",
                    set(raw_binding) if isinstance(raw_binding, Mapping) else set(),
                )
            )
            raw_members = _array(
                binding.get("members"),
                f"$.bindings[{binding_index}].members",
            )
            members = []
            for member_index, raw_member in enumerate(raw_members):
                member = dict(
                    _object(
                        raw_member,
                        f"$.bindings[{binding_index}].members[{member_index}]",
                        set(raw_member) if isinstance(raw_member, Mapping) else set(),
                    )
                )
                member.pop("materialized_path", None)
                members.append(member)
            binding["members"] = members
            logical_bindings.append(binding)
        logical = {
            "bindings": logical_bindings,
            "configuration": document["configuration"],
            "replay": document["replay"],
            "schema": document["schema"],
            "stages": document["stages"],
        }
    return _domain_hash(GENERATION_DOMAIN, logical)


def load_build_context(path: str | os.PathLike[str]) -> BuildContext:
    return BuildContext.load(path)


__all__ = [
    "BUILD_CONTEXT_SCHEMA",
    "GENERATION_DOMAIN",
    "BuildContext",
    "BuildContextError",
    "BuildRoots",
    "InputBinding",
    "InputMember",
    "Replay",
    "REDUCER_CONFIGURATION_SCHEMA",
    "ReducerConfiguration",
    "SourcePin",
    "Stage",
    "StageOutput",
    "canonical_json_bytes",
    "generation_identity",
    "load_build_context",
]
